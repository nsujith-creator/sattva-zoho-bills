import contextlib
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import sys
import sqlite3
import tempfile
import threading
import types
import unittest
from unittest.mock import Mock, patch
import owner_start as start


class OwnerPackaging(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.inputs = self.folder / 'acceptance'; self.inputs.mkdir()
        self.release = 'a' * 40
        self.cases = []
        for index in (1, 2):
            pdf = b'%PDF-1.4 synthetic ' + str(index).encode()
            name = 'synthetic-' + str(index) + '.pdf'
            (self.inputs / name).write_bytes(pdf)
            self.cases.append({'case': 'case' + str(index), 'bill_id': str(100 + index),
                               'filename': name, 'sha256': hashlib.sha256(pdf).hexdigest(),
                               'expected': {'invoice_number': 'SYN-' + str(index), 'total': '118.00'}})
        self.manifest = self.inputs / 'manifest.json'
        self.write_manifest()
        self.patch = patch.dict(os.environ, {'APP_AUTOMATION_POSTING': '1'}, clear=False)
        self.patch.start(); self.addCleanup(self.patch.stop)
        self.network = patch.object(socket, 'create_connection', side_effect=AssertionError('Network forbidden'))
        self.network.start(); self.addCleanup(self.network.stop)

    def write_manifest(self, release=None, cases=None):
        self.manifest.write_text(json.dumps({'release': release or self.release,
                                             'cases': self.cases if cases is None else cases}))

    def test_only_two_hashed_separate_pdfs_are_loaded(self):
        cases, digest = start.load_cases(self.folder, self.release)
        self.assertEqual(len(cases), 2)
        self.assertTrue(all(case['pdf'].startswith(b'%PDF-') for case in cases))
        self.assertEqual(digest, hashlib.sha256(self.manifest.read_bytes()).hexdigest())

    def test_missing_or_changed_pdf_stops_before_provider_use(self):
        for change in ('missing', 'changed'):
            with self.subTest(change=change):
                path = self.inputs / self.cases[0]['filename']
                if change == 'missing': path.unlink()
                else: path.write_bytes(b'%PDF-different')
                with self.assertRaises((OSError, RuntimeError)):
                    start.load_cases(self.folder, self.release)
                path.write_bytes(b'%PDF-1.4 synthetic 1')

    def test_wrong_release_or_case_count_refused(self):
        for release, cases in [('b' * 40, self.cases), (self.release, self.cases[:1]),
                               (self.release, self.cases + [self.cases[0]])]:
            with self.subTest(release=release, count=len(cases)):
                self.write_manifest(release, cases)
                with self.assertRaises(RuntimeError): start.load_cases(self.folder, self.release)

    def test_repeated_pdf_or_bill_or_case_refused(self):
        for field in ('sha256', 'bill_id', 'case'):
            with self.subTest(field=field):
                cases = copy.deepcopy(self.cases); cases[1][field] = cases[0][field]
                self.write_manifest(cases=cases)
                with self.assertRaises(RuntimeError): start.load_cases(self.folder, self.release)

    def test_relative_and_absolute_path_escape_refused(self):
        for name in ('../other.pdf', '/other.pdf', 'C:' + chr(92) + 'other.pdf'):
            with self.subTest(name=name):
                cases = copy.deepcopy(self.cases); cases[0]['filename'] = name
                self.write_manifest(cases=cases)
                with self.assertRaises(RuntimeError): start.load_cases(self.folder, self.release)

    def test_encoded_pdf_in_manifest_or_oversized_manifest_refused(self):
        cases = copy.deepcopy(self.cases); cases[0]['pdf_b64'] = 'secret'
        self.write_manifest(cases=cases)
        with self.assertRaises(RuntimeError): start.load_cases(self.folder, self.release)
        self.manifest.write_bytes(b'x' * 65537)
        with self.assertRaises(RuntimeError): start.load_cases(self.folder, self.release)

    def test_oversized_pdf_refused(self):
        path = self.inputs / self.cases[0]['filename']
        path.write_bytes(b'%PDF-' + b'x' * 5000000)
        with self.assertRaises(RuntimeError): start.load_cases(self.folder, self.release)

    def test_receipt_must_bind_release_manifest_and_complete_case_proof(self):
        runtime = self.runtime()
        cases, digest, engine, app, automation, calls = runtime
        self.assertFalse(self.accepted(cases, digest, automation))
        self.assertTrue(self.run_checks(runtime))
        receipt = self.folder / 'owner-original-acceptance.json'
        original = json.loads(receipt.read_text())
        for key, value in [('release', 'b' * 40), ('manifest_sha256', 'd' * 64), ('all_pass', False)]:
            with self.subTest(key=key):
                receipt.write_text(json.dumps(dict(original, **{key: value})))
                self.assertFalse(self.accepted(cases, digest, automation))
        for mutate in (lambda value: value.update(case_count=1), lambda value: value.update(cases=[]),
                       lambda value: value['cases'][0]['checks'].pop('readback_verified'),
                       lambda value: value['cases'][0].update(bill_id='999')):
            changed = copy.deepcopy(original); mutate(changed); receipt.write_text(json.dumps(changed))
            with self.assertRaises(RuntimeError): self.accepted(cases, digest, automation)

    def runtime(self, failure=None):
        cases, digest = start.load_cases(self.folder, self.release)
        db = self.folder / 'review.sqlite3'
        with contextlib.closing(sqlite3.connect(db)) as conn, conn:
            conn.execute('CREATE TABLE invoices (id TEXT PRIMARY KEY, filename TEXT, sha256 TEXT UNIQUE, created_at TEXT, pdf BLOB)')
            conn.execute('CREATE TABLE suggestions (invoice_id TEXT PRIMARY KEY, payload_json TEXT, created_at TEXT)')
        jobs = {}
        for case in cases:
            parsed = dict(case['expected'], confidence='high', issues=[], multi_invoice_count=1)
            doc_id = case['sha256'][:32]
            jobs[doc_id] = {'id': doc_id, 'policy': 'synthetic-policy', 'pdf_sha256': case['sha256'],
                            'status': 'duplicate', 'bill_id': case['bill_id'],
                            'parsed': parsed, 'source_check': copy.deepcopy(parsed),
                            'verification': {'ok': True, 'errors': []}, 'source_evidence': {'matched': True}}
        with contextlib.closing(sqlite3.connect(db)) as conn, conn:
            for case in cases:
                doc_id = case['sha256'][:32]
                conn.execute('INSERT INTO invoices VALUES(?,?,?,?,?)',
                             (doc_id, case['filename'], case['sha256'], 'synthetic', case['pdf']))
                intent = {'kind':'automatic_upload_intent','policy':'synthetic-policy','id':doc_id,'pdf_sha256':case['sha256']}
                conn.execute('INSERT INTO suggestions VALUES(?,?,?)', (doc_id,json.dumps(intent),'synthetic'))
        first_id = cases[0]['sha256'][:32]
        if failure == 'wrong_bill': jobs[first_id]['bill_id'] = '999'
        if failure == 'wrong_total': jobs[first_id]['parsed']['total'] = '117.99'
        if failure == 'missing_reading': jobs[first_id].pop('source_check')
        if failure == 'empty_reading': jobs[first_id]['source_check'] = {}
        if failure == 'second_wrong_total': jobs[first_id]['source_check']['total'] = '117.99'
        if failure == 'unsafe_stage': jobs[first_id]['status'] = 'posting'
        if failure == 'create_attempted': jobs[first_id]['create_attempted'] = True
        if failure == 'wrong_source': jobs[first_id]['pdf_sha256'] = 'f' * 64
        if failure == 'old_verification_missing': jobs[first_id].pop('verification')
        if failure == 'conflicting_payload': jobs[first_id]['payload'] = {'bill_number': 'CONFLICT'}
        state = self.folder / 'automation'; state.mkdir(exist_ok=True)
        for key, job in jobs.items(): (state / (key + '.json')).write_text(json.dumps(job))
        def save(job, status, **fields):
            job.update(fields); job['status'] = status
            (state / (job['id'] + '.json')).write_text(json.dumps(job))
        engine = types.SimpleNamespace(read=lambda key: jobs[key], folder=state,
                                       lock=threading.Lock(), save=save, core=object(),
                                       source_reader=lambda pdf: 'synthetic text')
        calls = []
        def call(route):
            self.assertEqual(os.environ['APP_AUTOMATION_POSTING'], '0')
            calls.append(route)
            if failure == 'read_error': raise OSError('fake transient')
            if failure == 'empty_record': return {'bill': {}}
            number = next(case['expected']['invoice_number'] for case in cases if route.endswith('/' + case['bill_id']))
            result = {'bill': {'bill_id': route.split('/')[-1], 'vendor_id': '7', 'bill_number': number,
                               'date': '2026-01-01', 'currency_code': 'INR', 'sub_total': '100.00',
                               'tax_total': '18.00', 'total': '118.00',
                               'line_items': [{'rate': '100.00', 'quantity': '1'}]}}
            if failure == 'record_changed' and len(calls) > 2: result['bill']['total'] = '119.00'
            if failure == 'missing_record_total': result['bill'].pop('total')
            if failure == 'nonfinite_record_total': result['bill']['total'] = 'NaN'
            return result
        def atomic(path, payload): path.write_text(json.dumps(payload))
        def verify(core, bid, payload, parsed, detail, owned=False):
            return {'ok': failure != 'readback_mismatch', 'errors': [] if failure != 'readback_mismatch' else ['synthetic mismatch']}
        def validate(parsed, core, vendors, check_zoho_rounding=False):
            if failure == 'validation_hold': raise RuntimeError('Synthetic source/vendor hold')
            self.assertEqual(vendors, {'SYNTHETIC': {'id': '7', 'gst_no': 'synthetic'}})
            self.assertFalse(check_zoho_rounding)
            return {'bill_number': parsed['invoice_number'], 'vendor_id': '7',
                    'total': '118.00', 'line_items': [{'rate': '100.00', 'quantity': '1'}]}
        def reconcile(first, second, text, core):
            complete = lambda v: isinstance(v,dict) and v.get('confidence')=='high' and v.get('issues')==[] and v.get('multi_invoice_count')==1
            if (not complete(first) or not complete(second) or first != second
                    or any(not first.get(key) for key in ('invoice_number','total'))):
                raise RuntimeError('Synthetic reading mismatch')
            digest = lambda v: hashlib.sha256(json.dumps(v,sort_keys=True).encode()).hexdigest()
            return copy.deepcopy(first), {'matched':True,'raw_reading_sha256':[digest(first),digest(second)]}
        client = types.SimpleNamespace(call=call, all=lambda *args: [
            {'contact_name': 'SYNTHETIC', 'contact_id': '7', 'gst_no': 'synthetic'}])
        automation = types.SimpleNamespace(Zoho=lambda: client, MONEY={'total'}, validate=validate,
                                           POLICY='synthetic-policy', TERMINAL={'duplicate', 'needs_review'}, atomic=atomic,
                                           reconcile_readings=reconcile, source_text=lambda pdf:'synthetic text',
                                           source_signature=lambda reading: json.dumps(reading, sort_keys=True),
                                           load_core=lambda: object(), verify_detail=verify,
                                           corroborate_source=lambda parsed, text, core: {'matched': True})
        app = types.SimpleNamespace(connect=sqlite3.connect)
        return cases, digest, engine, app, automation, calls

    def accepted(self, cases, digest, automation):
        return start.accepted_receipt(self.folder / 'owner-original-acceptance.json',
                                      self.release, digest, cases, self.folder, automation)

    def run_checks(self, runtime):
        cases, digest, engine, app, automation, calls = runtime
        with contextlib.redirect_stdout(io.StringIO()):
            start.run_original_checks(engine, self.folder, self.release, cases, digest, app, automation)
        return self.accepted(cases, digest, automation)

    def test_both_duplicates_and_unchanged_records_enable_posting(self):
        cases, digest, engine, app, automation, calls = self.runtime()
        with contextlib.redirect_stdout(io.StringIO()):
            start.run_original_checks(engine, self.folder, self.release, cases, digest, app, automation)
        self.assertEqual(os.environ['APP_AUTOMATION_POSTING'], '1')
        self.assertEqual(len(calls), 4)
        for case in cases:
            job = engine.read(case['sha256'][:32])
            self.assertTrue(job['payload'])
            self.assertIn(case['sha256'], job['payload']['notes'])
        self.assertTrue(self.accepted(cases, digest, automation))
        with contextlib.closing(sqlite3.connect(self.folder / 'review.sqlite3')) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM invoices').fetchone()[0], 2)
            self.assertEqual(db.execute('SELECT count(*) FROM suggestions').fetchone()[0], 2)
        with contextlib.redirect_stdout(io.StringIO()):
            start.run_original_checks(engine, self.folder, self.release, cases, digest, app, automation)
        with contextlib.closing(sqlite3.connect(self.folder / 'review.sqlite3')) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM invoices').fetchone()[0], 2)

    def test_mismatch_missing_reading_hold_and_read_failure_keep_posting_off(self):
        for failure in ('wrong_bill', 'wrong_total', 'missing_reading', 'empty_reading', 'second_wrong_total', 'unsafe_stage', 'record_changed', 'read_error', 'empty_record', 'missing_record_total', 'nonfinite_record_total', 'wrong_source', 'create_attempted', 'conflicting_payload', 'validation_hold', 'readback_mismatch'):
            with self.subTest(failure=failure):
                db = self.folder / 'review.sqlite3'
                if db.exists(): db.unlink()
                cases, digest, engine, app, automation, calls = self.runtime(failure)
                os.environ['APP_AUTOMATION_POSTING'] = '1'
                with contextlib.redirect_stdout(io.StringIO()):
                    start.run_original_checks(engine, self.folder, self.release, cases, digest, app, automation)
                self.assertEqual(os.environ['APP_AUTOMATION_POSTING'], '0')
                self.assertFalse(self.accepted(cases, digest, automation))

    def test_changed_or_missing_retained_job_cannot_reuse_passed_receipt(self):
        runtime = self.runtime()
        self.assertTrue(self.run_checks(runtime))
        cases, digest, engine, app, automation, calls = runtime
        path = engine.folder / (cases[0]['sha256'][:32] + '.json')
        original = path.read_bytes()
        job = json.loads(original); job['source_check'] = {}
        path.write_text(json.dumps(job))
        with self.assertRaises(RuntimeError): self.accepted(cases, digest, automation)
        path.write_bytes(original); path.unlink()
        with self.assertRaises(OSError): self.accepted(cases, digest, automation)

    def test_retained_original_blob_must_match_before_acceptance_or_restart(self):
        runtime = self.runtime()
        self.assertTrue(self.run_checks(runtime))
        cases, digest, engine, app, automation, calls = runtime
        with contextlib.closing(sqlite3.connect(self.folder / 'review.sqlite3')) as db, db:
            db.execute('UPDATE invoices SET pdf=? WHERE sha256=?', (b'%PDF-wrong original', cases[0]['sha256']))
        with self.assertRaises(RuntimeError): self.accepted(cases, digest, automation)
        self.assertFalse(self.run_checks(runtime))
        self.assertEqual(os.environ['APP_AUTOMATION_POSTING'], '0')

    def test_missing_approved_intent_holds_without_recreating_it(self):
        runtime = self.runtime()
        with contextlib.closing(sqlite3.connect(self.folder / 'review.sqlite3')) as db, db:
            db.execute('DELETE FROM suggestions')
        self.assertFalse(self.run_checks(runtime))
        with contextlib.closing(sqlite3.connect(self.folder / 'review.sqlite3')) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM suggestions').fetchone()[0], 0)

    def test_held_complete_readings_are_rechecked_without_provider_calls(self):
        runtime = self.runtime('old_verification_missing')
        cases,digest,engine,app,automation,calls=runtime
        for case in cases:engine.read(case['sha256'][:32])['status']='needs_review'
        for case in cases:
            job=engine.read(case['sha256'][:32])
            (engine.folder/(job['id']+'.json')).write_text(json.dumps(job))
        original = {case['sha256'][:32]: copy.deepcopy(engine.read(case['sha256'][:32])) for case in cases}
        self.assertTrue(self.run_checks(runtime))
        backup = self.folder/('acceptance-jobs-before-'+self.release+'.json')
        self.assertEqual(json.loads(backup.read_text())['jobs'], original)
        backup_bytes = backup.read_bytes()
        for case in cases:
            job=engine.read(case['sha256'][:32])
            self.assertTrue(job['acceptance_rechecked_from_retained'])
            prior = original[case['sha256'][:32]]
            self.assertEqual(job['parsed'],prior['parsed'])
            self.assertEqual(job['source_check'],prior['source_check'])
        self.assertTrue(self.run_checks(runtime))
        self.assertEqual(backup.read_bytes(), backup_bytes)

    def test_missing_reading_stops_before_worker_construction(self):
        cases,digest,engine,app,automation,calls = self.runtime('missing_reading')
        fake_app = types.SimpleNamespace(automation_enabled=lambda: True, Server=Mock(), main=Mock())
        def prepare():
            os.environ['APP_AUTOMATION_POSTING'] = '0'
            return self.folder, self.release, {}
        with patch.dict(sys.modules, {'app': fake_app, 'automation': automation}), \
                patch.object(start, 'prepare_owner', side_effect=prepare), \
                patch.object(start, 'load_cases', return_value=(cases, digest)), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, 'retained readings'): start.main()
        self.assertEqual(os.environ['APP_AUTOMATION_POSTING'], '0')
        fake_app.main.assert_not_called(); fake_app.Server.assert_not_called()
        self.assertEqual(calls, [])

    def test_low_confidence_or_issues_stop_before_worker_construction(self):
        cases,digest,engine,app,automation,calls = self.runtime()
        path = engine.folder/(cases[0]['sha256'][:32]+'.json')
        original = json.loads(path.read_text())
        fake_app = types.SimpleNamespace(automation_enabled=lambda: True, Server=Mock(), main=Mock())
        def prepare():
            os.environ['APP_AUTOMATION_POSTING'] = '0'
            return self.folder, self.release, {}
        for label in ('parsed','source_check'):
            for values in ({'confidence':'low'},{'issues':['unclear']},{'multi_invoice_count':2}):
                job=copy.deepcopy(original);job[label].update(values);path.write_text(json.dumps(job))
                with patch.dict(sys.modules, {'app': fake_app, 'automation': automation}), \
                        patch.object(start, 'prepare_owner', side_effect=prepare), \
                        patch.object(start, 'load_cases', return_value=(cases, digest)), \
                        contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaisesRegex(RuntimeError,'retained readings'): start.main()
                self.assertEqual(os.environ['APP_AUTOMATION_POSTING'], '0')
        fake_app.main.assert_not_called(); fake_app.Server.assert_not_called()
        self.assertEqual(calls, [])

    def test_changed_canonical_result_or_raw_hash_cannot_reuse_passed_receipt(self):
        runtime = self.runtime()
        self.assertTrue(self.run_checks(runtime))
        cases,digest,engine,app,automation,calls = runtime
        path = engine.folder/(cases[0]['sha256'][:32]+'.json')
        original = json.loads(path.read_text())
        for field in ('canonical', 'raw_hash'):
            job = copy.deepcopy(original)
            if field == 'canonical': job['reconciled_parsed']['total'] = '119.00'
            else: job['source_evidence']['raw_reading_sha256'][0] = 'f'*64
            path.write_text(json.dumps(job))
            self.assertFalse(all(start.reading_checks(cases[0],job,automation).values()))
            with self.assertRaises(RuntimeError): self.accepted(cases,digest,automation)

    def test_missing_mandatory_fields_or_source_proof_stop_before_worker(self):
        cases,digest,engine,app,automation,calls = self.runtime()
        path=engine.folder/(cases[0]['sha256'][:32]+'.json')
        original=json.loads(path.read_text())
        fake_app=types.SimpleNamespace(automation_enabled=lambda: True,Server=Mock(),main=Mock())
        def prepare():
            os.environ['APP_AUTOMATION_POSTING']='0'
            return self.folder,self.release,{}
        for failure in ('missing_fields','unreadable_source'):
            job=copy.deepcopy(original)
            if failure=='missing_fields':
                for label in ('parsed','source_check'):job[label].pop('invoice_number')
            path.write_text(json.dumps(job))
            source=patch.object(automation,'source_text',side_effect=RuntimeError('Synthetic unavailable PDF text')) if failure=='unreadable_source' else contextlib.nullcontext()
            with patch.dict(sys.modules,{'app':fake_app,'automation':automation}), \
                    patch.object(start,'prepare_owner',side_effect=prepare), \
                    patch.object(start,'load_cases',return_value=(cases,digest)), \
                    source,contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(RuntimeError,'source readings'):start.main()
            self.assertEqual(os.environ['APP_AUTOMATION_POSTING'],'0')
        fake_app.main.assert_not_called();fake_app.Server.assert_not_called()
        self.assertEqual(calls,[])

    def test_conflicting_existing_review_intent_is_preserved_and_holds(self):
        runtime = self.runtime()
        cases, digest, engine, app, automation, calls = runtime
        with contextlib.closing(sqlite3.connect(self.folder / 'review.sqlite3')) as db, db:
            case = cases[0]
            db.execute('UPDATE suggestions SET payload_json=? WHERE invoice_id=?',
                       ('{"kind":"manual"}',case['sha256'][:32]))
        self.assertFalse(self.run_checks(runtime))
        with contextlib.closing(sqlite3.connect(self.folder / 'review.sqlite3')) as db:
            self.assertEqual(db.execute('SELECT payload_json FROM suggestions').fetchone()[0], '{"kind":"manual"}')

    def test_invalid_pass_receipt_keeps_posting_off_before_worker_creation(self):
        runtime = self.runtime()
        self.assertTrue(self.run_checks(runtime))
        cases, digest, engine, app, automation, calls = runtime
        path = self.folder / 'owner-original-acceptance.json'
        receipt = json.loads(path.read_text()); receipt['cases'] = []; path.write_text(json.dumps(receipt))
        fake_app = types.SimpleNamespace(automation_enabled=lambda: True, Server=Mock(), main=Mock())
        def prepare():
            os.environ['APP_AUTOMATION_POSTING'] = '0'
            return self.folder, self.release, {}
        with patch.dict(sys.modules, {'app': fake_app, 'automation': automation}), \
                patch.object(start, 'prepare_owner', side_effect=prepare), \
                patch.object(start, 'load_cases', return_value=(cases, digest)), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(RuntimeError): start.main()
        self.assertEqual(os.environ['APP_AUTOMATION_POSTING'], '0')
        fake_app.main.assert_not_called(); fake_app.Server.assert_not_called()

    def test_expired_or_extended_window_forces_posting_off_before_volume_access(self):
        for stop in ('999', str(1000 + 8 * 86400)):
            with self.subTest(stop=stop), patch.dict(os.environ, {'DATA_DIR': '/data', 'OWNER_INBOX_STOP_AT_EPOCH': stop}), \
                    patch.object(start.time, 'time', return_value=1000), \
                    patch.object(start.container_start, 'prepare_and_drop') as drop:
                os.environ['APP_AUTOMATION_POSTING'] = '1'
                with self.assertRaises(RuntimeError): start.prepare_owner()
                self.assertEqual(os.environ['APP_AUTOMATION_POSTING'], '0')
                drop.assert_not_called()

    def test_actual_worker_duplicates_without_payload_are_reconciled_without_creation(self):
        try:
            import automation as actual
            from test_automation import core_fixture, invoice, source_fixture, GSTIN
        except ImportError:
            self.skipTest('Full-repository worker fixtures unavailable in source-only review export')
        core = core_fixture()
        parsed_by_pdf, bills, cases = {}, {}, []
        for index, source in enumerate(self.cases, 1):
            pdf = (self.inputs / source['filename']).read_bytes()
            parsed = invoice(); parsed['bill_number'] = 'SYNTHETIC-00' + str(index)
            parsed_by_pdf[pdf] = parsed
            expected = {key: parsed[key] for key in ('bill_number', 'bill_date', 'currency_code',
                                                     'vendor_gstin', 'buyer_gstin', 'invoice_total')}
            case = dict(source, expected=expected)
            cases.append(case)
            payload = actual.validate(copy.deepcopy(parsed), core, {
                'SYNTHETIC FREIGHT VENDOR': {'id': '123', 'gst_no': GSTIN}}, check_zoho_rounding=False)
            bills[case['bill_id']] = dict(payload, bill_id=case['bill_id'], currency_code='INR',
                sub_total=100, tax_total=18, total=118, gst_no=GSTIN,
                taxes=[{'tax_name': 'CGST9', 'tax_amount': 9}, {'tax_name': 'SGST9', 'tax_amount': 9}])
        self.write_manifest(cases=cases)
        cases, digest = start.load_cases(self.folder, self.release)
        db_path = self.folder / 'review.sqlite3'
        with contextlib.closing(sqlite3.connect(db_path)) as db, db:
            db.execute('CREATE TABLE invoices(id TEXT PRIMARY KEY, filename TEXT, sha256 TEXT UNIQUE, created_at TEXT, pdf BLOB)')
            db.execute('CREATE TABLE suggestions(invoice_id TEXT PRIMARY KEY, payload_json TEXT, created_at TEXT)')
            for case in cases:
                db.execute('INSERT INTO invoices VALUES(?,?,?,?,?)',
                           (case['sha256'][:32], case['filename'], case['sha256'], 'synthetic', case['pdf']))
                intent={'kind':'automatic_upload_intent','policy':actual.POLICY,'id':case['sha256'][:32],'pdf_sha256':case['sha256']}
                db.execute('INSERT INTO suggestions VALUES(?,?,?)',(case['sha256'][:32],json.dumps(intent),'synthetic'))
        stats = {'ai': 0, 'creates': 0, 'attachments': 0}
        def extract(pdf, second=False):
            stats['ai'] += 1
            return copy.deepcopy(parsed_by_pdf[pdf])
        def call(route, params=None, payload=None):
            if payload is not None:
                stats['creates'] += 1
                raise AssertionError('Acceptance must never create')
            return {'code': 0, 'bill': copy.deepcopy(bills[route.split('/')[-1]])}
        def find(vendor_id, number):
            return next(copy.deepcopy(bill) for bill in bills.values() if bill['bill_number'] == number)
        client = types.SimpleNamespace(call=call, find=find,
            all=lambda *args: [{'contact_name': 'SYNTHETIC FREIGHT VENDOR', 'contact_id': '123', 'gst_no': GSTIN}],
            attach=Mock(side_effect=AssertionError('Acceptance must never attach')),
            verify_attachment=Mock(side_effect=AssertionError('Acceptance must never attach')))
        engine = actual.Engine(db_path, core=core, ai=types.SimpleNamespace(extract=extract), zoho=client,
            source_reader=lambda pdf: source_fixture().replace('SYNTHETIC-001', parsed_by_pdf[pdf]['bill_number']))
        self.addCleanup(engine.close)
        os.environ['APP_AUTOMATION_POSTING'] = '0'
        with patch('urllib.request.urlopen', side_effect=AssertionError('Network forbidden')), \
                patch.object(actual,'source_text',side_effect=engine.source_reader):
            for case in cases:
                doc_id = case['sha256'][:32]
                engine.enqueue(doc_id); engine.process(doc_id)
                self.assertEqual(engine.read(doc_id)['status'], 'duplicate')
                self.assertNotIn('payload', engine.read(doc_id))
            before = copy.deepcopy(bills)
            with patch.object(actual, 'Zoho', return_value=client), contextlib.redirect_stdout(io.StringIO()):
                start.run_original_checks(engine, self.folder, self.release, cases, digest,
                                          types.SimpleNamespace(connect=sqlite3.connect), actual)
            self.assertEqual(os.environ['APP_AUTOMATION_POSTING'], '1')
            self.assertTrue(start.accepted_receipt(self.folder / 'owner-original-acceptance.json',
                self.release, digest, cases, self.folder, actual))
        self.assertEqual(bills, before)
        self.assertEqual(stats, {'ai': 4, 'creates': 0, 'attachments': 0})
        client.attach.assert_not_called(); client.verify_attachment.assert_not_called()
        for case in cases:
            self.assertTrue(engine.read(case['sha256'][:32])['payload'])


    def test_wrong_mount_is_refused_before_privilege_change(self):
        with patch.dict(os.environ, {'DATA_DIR': '/other'}), patch.object(start.container_start, 'prepare_and_drop') as drop:
            with self.assertRaises(RuntimeError): start.prepare_owner()
            drop.assert_not_called()
            self.assertEqual(os.environ['APP_AUTOMATION_POSTING'], '0')


if __name__ == '__main__':
    unittest.main()
