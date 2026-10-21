"""Real HTTP/SQLite account tests, isolated to newly created temporary storage."""
import hashlib
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'beckend'))


def lookup_customer_id(module, email):
    """Test-fixture inspection only; signup intentionally reveals no account ID."""
    db=module.conn()
    try:return db.execute('SELECT id FROM users WHERE email=?',(email,)).fetchone()[0]
    finally:db.close()


def authenticated_test_client(module, customer_id):
    """Synthetic fixture only: issue the same durable session used by real login."""
    db=module.conn()
    try:user=db.execute('SELECT id,password_hash FROM users WHERE id=?',(customer_id,)).fetchone()
    finally:db.close()
    raw=module.customer_security.issue_session(module.conn,module.DATABASE_CONFIG,module.app.secret_key,user)
    client=module.app.test_client()
    with client.session_transaction(base_url='https://localhost') as session:
        session['customer_id']=customer_id
        session['customer_session']=raw
        session.permanent=True
    return client


class CustomerAccountTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = {'APP_ENV':'test', 'SECRET_KEY':'synthetic-account-test-key', 'DATA_DIR':self.temp.name}
        self.env = patch.dict(os.environ, env, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        dotenv = patch('dotenv.load_dotenv')
        dotenv.start()
        self.addCleanup(dotenv.stop)
        network = patch('requests.sessions.Session.request', side_effect=AssertionError('External requests forbidden'))
        network.start()
        self.addCleanup(network.stop)
        path = Path(__file__).resolve().parents[1] / 'beckend' / 'app.py'
        spec = importlib.util.spec_from_file_location('customer_account_test_app', path)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.module.app.config['TESTING'] = True
        self.client = self.module.app.test_client()
        self.password = 'Original-test-password'
        self.new_password = 'Replacement-test-password'
        self.assertEqual(self.post('/api/register', {'name':'Synthetic Customer', 'email':'one@example.invalid', 'password':self.password}).status_code,201)

    def post(self,path,data,client=None):
        return (client or self.client).post(path,json=data,base_url='https://localhost')

    def get(self,path):
        return self.client.get(path,base_url='https://localhost')

    def login(self):
        result=self.post('/api/customer/login',{'email':'one@example.invalid','password':self.password})
        self.assertEqual(result.status_code,200)
        return result.get_json()['user']['id']

    def change(self,**overrides):
        data={'current_password':self.password,'new_password':self.new_password}
        data.update(overrides)
        return self.post('/api/customer/password',data)

    def test_signup_login_profile_change_reauthentication_and_real_logout(self):
        self.login()
        self.assertEqual(self.get('/api/check-auth').get_json()['user']['email'],'one@example.invalid')
        response=self.change()
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.headers['Cache-Control'],'no-store')
        self.assertEqual(self.get('/api/check-auth').status_code,401)
        self.assertEqual(self.post('/api/customer/login',{'email':'one@example.invalid','password':self.password}).status_code,401)
        self.assertEqual(self.post('/api/customer/login',{'email':'one@example.invalid','password':self.new_password}).status_code,200)
        self.assertEqual(self.post('/api/customer/logout',{}).status_code,200)
        self.assertEqual(self.get('/api/check-auth').status_code,401)

    def test_legacy_pbkdf2_password_can_be_changed_without_id_changes(self):
        user_id=self.login()
        old_hash='pbkdf2:sha256:260000$synthetic-salt$'+hashlib.pbkdf2_hmac('sha256',self.password.encode(),b'synthetic-salt',260000).hex()
        db=self.module.conn()
        try:
            with db:db.execute('UPDATE users SET password_hash = ? WHERE id = ?', (old_hash,user_id))
        finally:db.close()
        self.login()  # Direct fixture credential replacement invalidates prior sessions.
        self.assertEqual(self.change().status_code,200)
        response=self.post('/api/customer/login',{'email':'one@example.invalid','password':self.new_password})
        self.assertEqual(response.get_json()['user']['id'],user_id)

    def test_unauthenticated_change_does_not_open_database(self):
        with patch.object(self.module,'conn',side_effect=AssertionError('Must not connect')):
            response=self.change()
        self.assertEqual(response.status_code,401)

    def test_wrong_current_password_changes_nothing_and_session_survives(self):
        self.login()
        response=self.change(current_password='wrong-current-password')
        self.assertEqual(response.status_code,403)
        self.assertEqual(self.get('/api/check-auth').status_code,200)
        self.assertNotIn(self.password,response.get_data(as_text=True))
        self.assertNotIn(self.new_password,response.get_data(as_text=True))
        self.assertEqual(self.post('/api/customer/login',{'email':'one@example.invalid','password':self.password}).status_code,200)

    def test_malformed_requests_and_password_policy(self):
        self.login()
        for data in ([], 'invalid', {}, {'current_password':self.password,'new_password':'short'},
                     {'current_password':self.password,'new_password':'x'*129},
                     {'current_password':False,'new_password':self.new_password},
                     {'current_password':self.password,'new_password':self.password}):
            with self.subTest(kind=type(data).__name__):
                self.assertEqual(self.post('/api/customer/password',data).status_code,400)
        self.assertEqual(self.get('/api/check-auth').status_code,200)

    def test_client_cannot_change_another_customers_password(self):
        response=self.post('/api/register',{'name':'Other Synthetic','email':'two@example.invalid','password':'Other-test-password'})
        other_id=lookup_customer_id(self.module,'two@example.invalid')
        self.login()
        self.assertEqual(self.change(customer_id=other_id,email='two@example.invalid').status_code,200)
        other=self.module.app.test_client()
        result=self.post('/api/customer/login',{'email':'two@example.invalid','password':'Other-test-password'},client=other)
        self.assertEqual(result.status_code,200)
        self.assertEqual(result.get_json()['user']['id'],other_id)

    def test_deleted_customer_and_unavailable_storage_are_safe(self):
        user_id=self.login()
        with patch.object(self.module,'conn',side_effect=RuntimeError('PRIVATE_DATABASE_DETAILS')):
            response=self.change()
        self.assertEqual(response.status_code,503)
        self.assertNotIn('PRIVATE_',response.get_data(as_text=True))
        db=self.module.conn()
        try:
            with db:db.execute('DELETE FROM users WHERE id = ?', (user_id,))
        finally:db.close()
        self.assertEqual(self.change().status_code,401)
        self.assertEqual(self.get('/api/check-auth').status_code,401)

    def test_competing_password_change_is_not_overwritten(self):
        user_id=self.login()
        actual_hash=self.module.generate_password_hash
        competing_password='Competing-test-password'
        competing_hash=actual_hash(competing_password)
        def update_during_hash(password):
            # Deterministically interleave a second writer after verification.
            other=self.module.conn()
            try:
                with other:
                    other.execute('UPDATE users SET password_hash = ? WHERE id = ?',
                                  (competing_hash,user_id))
            finally:other.close()
            return actual_hash(password)
        with patch.object(self.module,'generate_password_hash',side_effect=update_during_hash):
            response=self.change()
        self.assertEqual(response.status_code,409)
        self.assertEqual(self.post('/api/customer/login',{'email':'one@example.invalid','password':self.new_password}).status_code,401)
        self.assertEqual(self.post('/api/customer/login',{'email':'one@example.invalid','password':competing_password}).status_code,200)

    def test_customer_page_and_account_script_are_served(self):
        page=self.get('/customer.html')
        self.assertEqual(page.status_code,200)
        for marker in (b'id="passwordForm"',b'id="accountPanel"',b'/customer-account.js'):
            self.assertIn(marker,page.data)
        script=self.get('/customer-account.js')
        self.assertEqual(script.status_code,200)
        self.assertIn(b'/api/customer/logout',script.data)
        self.assertIn(b'/api/customer/password',script.data)
        page.close()
        script.close()


if __name__=='__main__':unittest.main()
