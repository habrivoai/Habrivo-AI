"""Operator-only environment validation. No app import, files written or service I/O.

A passing result validates configuration syntax only, never credentials, model
support, schemas, deliverability, isolation or permission to perform a smoke test.
"""
import json
import os
import re
from customer_security import mail_config, SecurityError
from database import DatabaseConfig
from marketing_studio import ProviderConfig, StudioError


def check_configuration():
    checks = {}
    try:
        config = ProviderConfig.from_env()
        checks['ai_configuration'] = 'valid'
        checks['smoke_ai_budget'] = 'valid' if config.daily_limit <= 3 else 'reduce_daily_limit_to_3_or_less'
    except StudioError:
        checks['ai_configuration'] = 'missing_or_invalid'
        checks['smoke_ai_budget'] = 'not_checked'
    try:
        mail_config()
        checks['mail_configuration'] = 'valid'
    except SecurityError:
        checks['mail_configuration'] = 'missing_or_invalid'
    secret = os.getenv('SECRET_KEY', '')
    checks['session_secret'] = ('valid' if 32 <= len(secret) <= 4096 and
                                not re.search(r'[\x00-\x20\x7f]', secret) else 'missing_or_invalid')
    try:
        config = DatabaseConfig.from_env()
        explicit = os.getenv('DB_BACKEND') in {'sqlite', 'postgres'}
        checks['database_configuration'] = ('valid' if explicit and
            (config.backend != 'sqlite' or bool(os.getenv('DATA_DIR', '').strip())) else 'explicit_backend_and_location_required')
    except (ValueError, OSError, RuntimeError):
        checks['database_configuration'] = 'missing_or_invalid'
    ready = all(value == 'valid' for value in checks.values())
    return {'configuration_valid_for_controlled_test': ready,
            'scope': 'environment_syntax_only_no_service_or_database_access',
            'checks': checks,
            'not_verified': ['model_schema_compatibility', 'provider_credentials_and_pricing',
                             'smtp_credentials_and_inbox_delivery', 'database_schema_and_persistence',
                             'test_environment_isolation', 'https_browser_journey'],
            'trial_lifetime_attempts': 3,
            'committed_attempts_count_on_provider_failure': True,
            'generation_output_token_cap': 900,
            'production_ready': False}


def main():
    report = check_configuration()
    print(json.dumps(report, sort_keys=True))
    return 0 if report['configuration_valid_for_controlled_test'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
