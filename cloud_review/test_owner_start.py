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
                            'verification': {'ok': True, 'errors': []}, 'source_evidence': {'matched': True},
                            'payload': {'bill_number': case['expected']['invoice_number'], 'vendor_id': '7',
                                        'total': '118.00', 'line_items': [{'rate': '100.00', 'quantity': '1'}]}}
        first_id = cases[0]['sha256'][:32]
        if failure == 'wrong_bill': jobs[first_id]['bill_id'] = '999'
        if failure == 'wrong_total': jobs[first_id]['parsed']['total'] = '117.99'
        if failure == 'missing_reading': jobs[first_id].pop('source_check')
        if failure == 'empty_reading': jobs[first_id]['source_check'] = {}
        if failure == 'second_wrong_total': jobs[first_id]['source_check']['total'] = '117.99'
        if failure == 'held': jobs[first_id]['status'] = 'needs_review'
        if failure == 'wrong_source': jobs[first_id]['pdf_sha256'] = 'f' * 64
        if failure == 'missing_worker_verification': jobs[first_id].pop('verification')
        if failure == 'missing_payload': jobs[first_id].pop('payload')
        state = self.folder / 'automation'; state.mkdir(exist_ok=True)
        for key, job in jobs.items(): (state / (key + '.json')).write_text(json.dumps(job))
        engine = types.SimpleNamespace(read=lambda key: jobs[key], folder=state,
                                       lock=threading.Lock(), save=Mock(), core=object(),
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
        automation = types.SimpleNamespace(Zoho=lambda: types.SimpleNamespace(call=call), MONEY={'total'},
                                           POLICY='synthetic-policy', TERMINAL={'duplicate', 'needs_review'}, atomic=atomic,
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
        self.assertTrue(self.accepted(cases, digest, automation))
        with contextlib.closing(sqlite3.connect(self.folder / 'review.sqlite3')) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM invoices').fetchone()[0], 2)
            self.assertEqual(db.execute('SELECT count(*) FROM suggestions').fetchone()[0], 2)
        with contextlib.redirect_stdout(io.StringIO()):
            start.run_original_checks(engine, self.folder, self.release, cases, digest, app, automation)
        with contextlib.closing(sqlite3.connect(self.folder / 'review.sqlite3')) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM invoices').fetchone()[0], 2)

    def test_mismatch_missing_reading_hold_and_read_failure_keep_posting_off(self):
        for failure in ('wrong_bill', 'wrong_total', 'missing_reading', 'empty_reading', 'second_wrong_total', 'held', 'record_changed', 'read_error', 'empty_record', 'missing_record_total', 'nonfinite_record_total', 'wrong_source', 'missing_worker_verification', 'missing_payload', 'readback_mismatch'):
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

    def test_existing_approved_rows_receive_missing_intents(self):
        runtime = self.runtime()
        cases, digest, engine, app, automation, calls = runtime
        with contextlib.closing(sqlite3.connect(self.folder / 'review.sqlite3')) as db, db:
            for case in cases:
                db.execute('INSERT INTO invoices VALUES(?,?,?,?,?)',
                           (case['sha256'][:32], case['filename'], case['sha256'], 'synthetic', case['pdf']))
        self.assertTrue(self.run_checks(runtime))
        with contextlib.closing(sqlite3.connect(self.folder / 'review.sqlite3')) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM suggestions').fetchone()[0], 2)

    def test_conflicting_existing_review_intent_is_preserved_and_holds(self):
        runtime = self.runtime()
        cases, digest, engine, app, automation, calls = runtime
        with contextlib.closing(sqlite3.connect(self.folder / 'review.sqlite3')) as db, db:
            case = cases[0]
            db.execute('INSERT INTO invoices VALUES(?,?,?,?,?)',
                       (case['sha256'][:32], case['filename'], case['sha256'], 'synthetic', case['pdf']))
            db.execute('INSERT INTO suggestions VALUES(?,?,?)', (case['sha256'][:32], '{"kind":"manual"}', 'synthetic'))
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

    def test_wrong_mount_is_refused_before_privilege_change(self):
        with patch.dict(os.environ, {'DATA_DIR': '/other'}), patch.object(start.container_start, 'prepare_and_drop') as drop:
            with self.assertRaises(RuntimeError): start.prepare_owner()
            drop.assert_not_called()
            self.assertEqual(os.environ['APP_AUTOMATION_POSTING'], '0')


if __name__ == '__main__':
    unittest.main()
