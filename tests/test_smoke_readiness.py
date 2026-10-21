"""Environment-only checks and mocked-boundary regressions; no real services."""
import contextlib
import io
import json
import os
import unittest
from unittest.mock import patch
import test_customer_account  # Existing backend import path; no app import here.
from test_customer_recovery import MAIL
import customer_security as security
import marketing_studio as studio
import smoke_readiness

ENV = {**MAIL, 'MARKETING_AI_PROVIDER':'openai', 'OPENAI_API_KEY':'PRIVATE_SYNTHETIC_KEY',
       'MARKETING_AI_MODEL':'configured-model', 'MARKETING_AI_DAILY_LIMIT':'3',
       'SECRET_KEY':'synthetic-secret-key-with-at-least-32-characters',
       'APP_ENV':'test', 'DB_BACKEND':'sqlite', 'DATA_DIR':'/synthetic-disposable-directory'}


class SmokeReadinessTests(unittest.TestCase):
    def test_valid_environment_has_no_service_access_or_sensitive_output(self):
        with patch.dict(os.environ, ENV, clear=True), patch('database.connect', side_effect=AssertionError('No DB')), \
             patch.object(studio.requests,'post',side_effect=AssertionError('No AI')), \
             patch.object(security.smtplib,'SMTP_SSL',side_effect=AssertionError('No SMTP')):
            report = smoke_readiness.check_configuration()
        self.assertTrue(report['configuration_valid_for_controlled_test'])
        self.assertFalse(report['production_ready'])
        self.assertIn('model_schema_compatibility', report['not_verified'])
        rendered = json.dumps(report)
        for secret in (ENV['OPENAI_API_KEY'], ENV['SECRET_KEY'], MAIL['SMTP_PASSWORD'], MAIL['PUBLIC_BASE_URL'], ENV['DATA_DIR'], ENV['MARKETING_AI_MODEL']):
            self.assertNotIn(secret, rendered)
        self.assertEqual(report['trial_lifetime_attempts'], 3)
        self.assertTrue(report['committed_attempts_count_on_provider_failure'])

    def test_cli_returns_only_safe_report_and_meaningful_exit_codes(self):
        for env, expected in ((ENV,0), ({},1)):
            with patch.dict(os.environ,env,clear=True), contextlib.redirect_stdout(io.StringIO()) as out:
                result = smoke_readiness.main()
            self.assertEqual(result,expected)
            self.assertEqual(json.loads(out.getvalue())['configuration_valid_for_controlled_test'], expected == 0)

    def test_low_smoke_budget_does_not_change_runtime_default_or_trial_policy(self):
        with patch.dict(os.environ,ENV,clear=True):
            os.environ.pop('MARKETING_AI_DAILY_LIMIT')
            self.assertEqual(studio.ProviderConfig.from_env().daily_limit, 50)
            report = smoke_readiness.check_configuration()
            self.assertEqual(report['checks']['smoke_ai_budget'],'reduce_daily_limit_to_3_or_less')
            self.assertFalse(report['configuration_valid_for_controlled_test'])

    def test_malformed_credential_and_configuration_values_fail_without_echoing(self):
        changes = [('OPENAI_API_KEY','PRIVATE\r\nHeader:value'), ('OPENAI_API_KEY','x'*4097),
                   ('MARKETING_AI_MODEL','unsupported model'), ('MARKETING_AI_PROVIDER','other'),
                   ('MARKETING_AI_DAILY_LIMIT','0'), ('MARKETING_AI_DAILY_LIMIT','not-a-number'),
                   ('SMTP_USERNAME','PRIVATE\nuser'), ('SMTP_PASSWORD','PRIVATE\x00secret'),
                   ('SMTP_PASSWORD',' '), ('PUBLIC_BASE_URL','https://example.invalid\n'),
                   ('PUBLIC_BASE_URL','https://example.invalid:0'), ('PUBLIC_BASE_URL','https://example.invalid/path'),
                   ('PUBLIC_BASE_URL','https://user:PRIVATE@example.invalid'),
                   ('PUBLIC_BASE_URL','http://example.invalid'), ('SECRET_KEY','short'),
                   ('DATABASE_URL','postgresql://PRIVATE:secret@host:bad/db')]
        for name, value in changes:
            with self.subTest(name=name), patch.object(os, 'environ', {**ENV,name:value}):
                report = smoke_readiness.check_configuration()
                self.assertFalse(report['configuration_valid_for_controlled_test'])
                self.assertNotIn('PRIVATE',json.dumps(report))

    def test_explicit_database_location_required_but_never_connected(self):
        for omit in ('DB_BACKEND','DATA_DIR'):
            env = {key:value for key,value in ENV.items() if key != omit}
            with patch.dict(os.environ,env,clear=True):
                self.assertFalse(smoke_readiness.check_configuration()['configuration_valid_for_controlled_test'])
        with patch.dict(os.environ,{**ENV,'DB_BACKEND':'postgres','DATABASE_URL':'postgresql://user:PRIVATE@localhost/disposable'},clear=True):
            self.assertEqual(smoke_readiness.check_configuration()['checks']['database_configuration'],'valid')

    def test_smtp_transport_is_tls_verified_bounded_and_never_enables_debug(self):
        import ssl
        from email.message import EmailMessage
        with patch.dict(os.environ,ENV,clear=True), patch.object(security.smtplib,'SMTP_SSL') as smtp:
            security.send_security_email(EmailMessage(), security.mail_config())
        self.assertEqual(smtp.call_args.args, ('smtp.example.invalid',465))
        self.assertEqual(smtp.call_args.kwargs['timeout'],10)
        self.assertEqual(smtp.call_args.kwargs['context'].verify_mode,ssl.CERT_REQUIRED)
        self.assertTrue(smtp.call_args.kwargs['context'].check_hostname)
        smtp.return_value.__enter__.return_value.set_debuglevel.assert_not_called()

    def test_invalid_mail_config_never_opens_database_or_transport(self):
        with patch.dict(os.environ,{**ENV,'PUBLIC_BASE_URL':'https://example.invalid\t'},clear=True), \
             patch.object(security.smtplib,'SMTP_SSL') as smtp:
            factory = unittest.mock.Mock(side_effect=AssertionError('No DB'))
            with self.assertRaises(security.SecurityError):
                security.deliver_reset_emails(factory,unittest.mock.Mock(),'synthetic-secret')
            factory.assert_not_called(); smtp.assert_not_called()


if __name__ == '__main__':
    unittest.main()
