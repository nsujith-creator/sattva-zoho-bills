"""Owner sign-in, expiry, CSRF and logout; local synthetic data only."""
import gc
import http.client
import json
import re
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import urlencode
from app import Server, Handler, init_db
from auth import OwnerSessions, IDLE_SECONDS, ABSOLUTE_SECONDS, password_hash

PASSWORD='synthetic-owner-password-for-tests-only'


class SessionRules(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.verifier=password_hash(PASSWORD)

    def setUp(self):
        self.now=100.0
        self.auth=OwnerSessions(self.verifier,clock=lambda:self.now)

    def login(self,password=PASSWORD):
        form=self.auth.form('test-peer')
        return self.auth.login(form,form,password,'test-peer')

    def test_nonce_required_and_single_use(self):
        self.assertEqual((None,'form_expired'),self.auth.login('','','x','test-peer'))
        form=self.auth.form('test-peer')
        token,error=self.auth.login(form,form,PASSWORD,'test-peer')
        self.assertIsNone(error);self.assertTrue(self.auth.lookup(token))
        self.assertEqual((None,'form_expired'),self.auth.login(form,form,PASSWORD,'test-peer'))

    def test_login_nonce_cannot_move_between_peers(self):
        form=self.auth.form('one')
        self.assertEqual((None,'form_expired'),self.auth.login(form,form,PASSWORD,'two'))

    def test_idle_expiry_and_logout(self):
        token,_=self.login();self.now+=IDLE_SECONDS
        self.assertIsNone(self.auth.lookup(token))
        token,_=self.login();self.auth.logout(token)
        self.assertIsNone(self.auth.lookup(token))

    def test_absolute_expiry_even_with_activity(self):
        token,_=self.login()
        for _ in range(24):
            self.now+=1000;self.assertTrue(self.auth.lookup(token))
        self.now=100+ABSOLUTE_SECONDS
        self.assertIsNone(self.auth.lookup(token))

    def test_failed_login_limit_and_recovery(self):
        for _ in range(5):self.assertEqual((None,'invalid'),self.login('wrong'))
        self.assertEqual((None,'throttled'),self.login())
        self.now+=901;token,error=self.login()
        self.assertIsNone(error);self.assertTrue(self.auth.lookup(token))

    def test_password_limits_malformed_verifier_and_bounded_state(self):
        with self.assertRaises(ValueError):OwnerSessions('short')
        with self.assertRaises(ValueError):OwnerSessions('pbkdf2_sha256$1$00$00')
        self.assertEqual((None,'invalid'),self.login('x'*1000))
        for _ in range(300):self.auth.form('one')
        self.assertLessEqual(len(self.auth.forms),256)


class OwnerHTTP(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.verifier=password_hash(PASSWORD)

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();db=Path(self.temp.name)/'review.sqlite3';init_db(db)
        self.server=Server(('127.0.0.1',0),Handler,db,'owner',self.verifier,local_http=True)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()

    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join(timeout=3);gc.collect();self.temp.cleanup()

    def request(self,method,path,body=None,headers=None):
        c=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=5)
        c.request(method,path,body,headers or {});r=c.getresponse();result=r.status,dict(r.getheaders()),r.read();c.close();return result

    def login(self,password=PASSWORD,destination='/'):
        code,h,b=self.request('GET','/login?next='+destination)
        nonce=re.search(rb'name="csrf" value="([^"]+)"',b).group(1).decode()
        cookie=h['Set-Cookie'].split(';')[0]
        return self.request('POST','/login',urlencode({'csrf':nonce,'password':password,'next':destination}),{'Cookie':cookie,'Content-Type':'application/x-www-form-urlencoded'})

    def test_deep_link_login_cookie_and_basic_auth_rejected(self):
        path='/review/'+'a'*32
        code,h,_=self.request('GET',path);self.assertEqual(303,code);self.assertIn('/login?next=',h['Location'])
        code,h,_=self.login(destination=path);self.assertEqual(303,code);self.assertEqual(path,h['Location'])
        self.assertIn('HttpOnly',h['Set-Cookie']);self.assertIn('SameSite=Strict',h['Set-Cookie'])
        cookie=h['Set-Cookie'].split(';')[0]
        self.assertEqual(200,self.request('GET','/',headers={'Cookie':cookie})[0])
        self.assertEqual(303,self.request('GET','/',headers={'Authorization':'Basic b3duZXI6cGFzc3dvcmQ='})[0])

    def test_browser_icon_request_does_not_replace_login_nonce(self):
        _,h,page=self.request('GET','/login')
        nonce=re.search(rb'name="csrf" value="([^"]+)"',page).group(1).decode()
        cookie=h['Set-Cookie'].split(';')[0]
        code,icon_headers,_=self.request('GET','/favicon.ico',headers={'Cookie':cookie})
        self.assertEqual(204,code);self.assertNotIn('Set-Cookie',icon_headers)
        code,_,_=self.request('POST','/login',urlencode({'csrf':nonce,'password':PASSWORD}),{'Cookie':cookie,'Content-Type':'application/x-www-form-urlencoded'})
        self.assertEqual(303,code)

    def test_logout_requires_csrf_then_revokes_old_session(self):
        _,h,_=self.login();cookie=h['Set-Cookie'].split(';')[0]
        _,_,page=self.request('GET','/',headers={'Cookie':cookie})
        csrf=re.search(rb'name="csrf-token" content="([^"]+)"',page).group(1).decode()
        headers={'Cookie':cookie,'Content-Type':'application/x-www-form-urlencoded'}
        self.assertEqual(403,self.request('POST','/logout','csrf=wrong',headers)[0])
        code,h,_=self.request('POST','/logout',urlencode({'csrf':csrf}),headers)
        self.assertEqual(303,code);self.assertIn('Max-Age=0',h['Set-Cookie'])
        self.assertEqual(303,self.request('GET','/',headers={'Cookie':cookie})[0])
        self.assertEqual(401,self.request('POST','/api/invoices',b'%PDF-synthetic',{'Cookie':cookie})[0])

    def test_old_session_rotated_and_external_redirect_disallowed(self):
        _,h,_=self.login(destination='//outside.example/steal');self.assertEqual('/',h['Location'])
        first=h['Set-Cookie'].split(';')[0]
        self.assertEqual('/',Handler.safe_next('https://outside.example/'))
        form=self.server.auth.form('127.0.0.1')
        code,h,_=self.request('POST','/login',urlencode({'csrf':form,'password':PASSWORD}),{'Cookie':first+'; sattva_login='+form,'Content-Type':'application/x-www-form-urlencoded'})
        self.assertEqual(303,code)
        self.assertEqual(303,self.request('GET','/',headers={'Cookie':first})[0])

    def test_expired_cookie_cannot_read_original_or_mutate(self):
        _,h,_=self.login();cookie=h['Set-Cookie'].split(';')[0]
        self.server.auth.clock=lambda:10**15
        self.assertEqual(303,self.request('GET','/invoice/'+'a'*32,headers={'Cookie':cookie})[0])
        self.assertEqual(401,self.request('POST','/api/reviews/'+'a'*32,b'{}',{'Cookie':cookie})[0])

    def test_login_csrf_and_excessive_requests(self):
        code,_,_=self.request('POST','/login','password='+PASSWORD,{'Content-Type':'application/x-www-form-urlencoded'})
        self.assertEqual(400,code)
        for _ in range(5):self.assertEqual(401,self.login('wrong')[0])
        code,_,body=self.login();self.assertEqual(429,code);self.assertIn(b'15 minutes',body)

    def test_production_cookie_flags_and_plain_http_refusal(self):
        self.server.local_http=False;self.server.session_cookie='__Host-sattva_session';self.server.login_cookie='__Host-sattva_login'
        self.assertEqual(403,self.request('GET','/login')[0])
        code,h,_=self.request('GET','/login',headers={'X-Forwarded-Proto':'https'})
        self.assertEqual(200,code);self.assertIn('; Secure',h['Set-Cookie']);self.assertIn('Strict-Transport-Security',h)


if __name__=='__main__':unittest.main()
