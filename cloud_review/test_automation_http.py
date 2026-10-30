"""Actual HTTP owner journey with synthetic source and fake external providers."""
import gc
import http.client
import json
import os
import re
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode
import app
import automation as a
from test_automation import PDF, FakeAI, FakeZoho, core_fixture, invoice, source_fixture


class UploadJourney(unittest.TestCase):
    def test_upload_runs_without_manual_entry_and_duplicate_retries_do_not_post(self):
        folder=tempfile.TemporaryDirectory()
        db=Path(folder.name)/'review.sqlite3'
        app.init_db(db)
        ai=FakeAI(invoice())
        zoho=FakeZoho(folder.name)
        # FakeZoho checks the real upload ID as well as its persisted posting authorization.
        original=zoho.call
        def call(endpoint,params=None,payload=None):
            if payload:
                import test_automation
                jobs=[p for p in (Path(folder.name)/'automation').glob('*.json') if len(p.stem)==32]
                with patch.object(test_automation,'DOC',jobs[0].stem):return original(endpoint,params,payload)
            return original(endpoint,params,payload)
        zoho.call=call
        engine=a.Engine(db,core_fixture(),ai,zoho,source_reader=lambda pdf: source_fixture())
        with patch.dict(os.environ,{'APP_AUTOMATION_POLICY':a.POLICY,'APP_AUTOMATION_POSTING':'1'}), \
                patch.object(app,'Engine',return_value=engine), \
                patch('urllib.request.urlopen',side_effect=AssertionError('External network forbidden')):
            server=app.Server(('127.0.0.1',0),app.Handler,db,'owner','synthetic-test-password-123456',local_http=True)
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            def request(method,path,body=None,headers=None):
                connection=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=5)
                connection.request(method,path,body,headers or {})
                response=connection.getresponse();status=response.status
                headers=dict(response.getheaders());data=response.read();connection.close()
                return status,headers,data
            try:
                status,headers,body=request('GET','/login')
                nonce=re.search(r'name="csrf" value="([^"]+)"',body.decode()).group(1)
                status,headers,_=request('POST','/login',urlencode({'csrf':nonce,'password':'synthetic-test-password-123456'}),
                    {'Cookie':headers['Set-Cookie'].split(';')[0],'Content-Type':'application/x-www-form-urlencoded'})
                self.assertEqual(303,status)
                cookie=headers['Set-Cookie'].split(';')[0]
                status,_,body=request('GET','/',headers={'Cookie':cookie})
                csrf=re.search(r'name="csrf-token" content="([^"]+)"',body.decode()).group(1)
                self.assertIn(b'Drop a vendor bill here',body)
                upload_headers={'Cookie':cookie,'X-CSRF-Token':csrf,'Content-Type':'application/pdf','X-Filename':'synthetic.pdf'}
                self.assertEqual(200,request('GET','/healthz')[0])
                # Keep the synthetic unavailable state stable across both HTTP reads;
                # the real worker can recover/clear worker_error between requests.
                with patch.object(engine,'healthy',return_value=False):
                    self.assertEqual(503,request('GET','/healthz')[0])
                    self.assertIn(b'Processing is temporarily unavailable',request('GET','/',headers={'Cookie':cookie})[2])
                oversized={**upload_headers,'Content-Length':str(a.MAX_PDF+1)}
                self.assertEqual(413,request('POST','/api/invoices',PDF,oversized)[0])
                self.assertEqual(0,ai.calls)
                # Crash window: original+intent commit, but enqueue does not run.
                engine.close()
                with patch.object(engine,'enqueue',side_effect=OSError('simulated interruption after upload commit')):
                    status,_,body=request('POST','/api/invoices',PDF,upload_headers)
                self.assertEqual(503,status)
                import sqlite3
                with sqlite3.connect(db) as connection:
                    doc_id=connection.execute('SELECT id FROM invoices').fetchone()[0]
                connection.close()
                self.assertIsNone(engine.read(doc_id))
                engine=a.Engine(db,core_fixture(),ai,zoho,source_reader=lambda pdf: source_fixture())
                server.automation=engine
                engine.recover_uploads()
                self.assertEqual('queued',engine.read(doc_id)['status'])
                engine.start()
                for _ in range(60):
                    status,_,body=request('GET','/review/'+doc_id,headers={'Cookie':cookie})
                    if b'Posted to Zoho.' in body:break
                    time.sleep(.05)
                self.assertEqual('verified',engine.read(doc_id)['status'],str((ai.calls,zoho.posts,engine.read(doc_id))))
                self.assertIn(b'Posted to Zoho.',body)
                self.assertIn(b'Open Zoho bill',body)
                self.assertNotIn(b'Enter the invoice details',body)
                self.assertIn(b'pageshow',body)
                self.assertEqual((2,1,1),(ai.calls,zoho.posts,zoho.attachments))
                # The same secure session and CSRF guard protect retry. A retained
                # created bill resumes read-back/attachment checking without a POST.
                retry_body=urlencode({'csrf':csrf})
                before=engine.read(doc_id)
                status,headers,_=request('POST','/api/retry/'+doc_id,retry_body,{'Content-Type':'application/x-www-form-urlencoded'})
                self.assertEqual((303,'/login?next=%2Freview%2F'+doc_id),(status,headers['Location']))
                self.assertEqual(before,engine.read(doc_id))  # Signed out: sent to sign-in, nothing retried.
                self.assertEqual(403,request('POST','/api/retry/'+doc_id,urlencode({'csrf':'wrong'}),
                    {'Cookie':cookie,'Content-Type':'application/x-www-form-urlencoded'})[0])
                job=engine.read(doc_id);engine.save(job,'retryable')
                self.assertIn(b'Retry processing',request('GET','/review/'+doc_id,headers={'Cookie':cookie})[2])
                self.assertEqual(303,request('POST','/api/retry/'+doc_id,retry_body,
                    {'Cookie':cookie,'Content-Type':'application/x-www-form-urlencoded'})[0])
                for _ in range(60):
                    if engine.read(doc_id)['status']=='verified':break
                    time.sleep(.05)
                self.assertEqual('verified',engine.read(doc_id)['status'])
                self.assertEqual((2,1,1),(ai.calls,zoho.posts,zoho.attachments))
                duplicate_status,_,duplicate_body=request('POST','/api/invoices',PDF,upload_headers)
                self.assertEqual(200,duplicate_status)
                self.assertEqual(doc_id,json.loads(duplicate_body)['id'])
                self.assertEqual(303,request('GET','/review/'+doc_id)[0])
                self.assertEqual((2,1,1),(ai.calls,zoho.posts,zoho.attachments))
                self.assertEqual(303,request('POST','/logout',urlencode({'csrf':csrf}),
                    {'Cookie':cookie,'Content-Type':'application/x-www-form-urlencoded'})[0])
                self.assertEqual(303,request('GET','/review/'+doc_id,headers={'Cookie':cookie})[0])
            finally:
                server.shutdown();server.server_close();thread.join(timeout=3)
        gc.collect();folder.cleanup()


if __name__=='__main__':unittest.main()
