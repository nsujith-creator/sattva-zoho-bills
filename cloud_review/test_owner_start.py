import contextlib
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import socket
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

    def test_receipt_must_bind_exact_release_and_manifest(self):
        receipt = self.folder / 'owner-original-acceptance.json'
        self.assertFalse(start.accepted_receipt(receipt, self.release, 'c' * 64))
        data = {'release': self.release, 'manifest_sha256': 'c' * 64, 'all_pass': True, 'case_count': 2}
        receipt.write_text(json.dumps(data))
        self.assertTrue(start.accepted_receipt(receipt, self.release, 'c' * 64))
        for key, value in [('release', 'b' * 40), ('manifest_sha256', 'd' * 64),
                           ('all_pass', False), ('case_count', 1)]:
            with self.subTest(key=key):
                changed = dict(data, **{key: value}); receipt.write_text(json.dumps(changed))
                self.assertFalse(start.accepted_receipt(receipt, self.release, 'c' * 64))

    def runtime(self, failure=None):
        cases, digest = start.load_cases(self.folder, self.release)
        db = self.folder / 'review.sqlite3'
        with contextlib.closing(sqlite3.connect(db)) as conn, conn:
            conn.execute('CREATE TABLE invoices (id TEXT PRIMARY KEY, filename TEXT, sha256 TEXT UNIQUE, created_at TEXT, pdf BLOB)')
            conn.execute('CREATE TABLE suggestions (invoice_id TEXT PRIMARY KEY, payload_json TEXT, created_at TEXT)')
        jobs = {case['sha256'][:32]: {'status': 'duplicate', 'bill_id': case['bill_id'],
                                      'parsed': dict(case['expected']), 'source_check': {}}
                for case in cases}
        if failure == 'wrong_bill': jobs[cases[0]['sha256'][:32]]['bill_id'] = '999'
        if failure == 'wrong_total': jobs[cases[0]['sha256'][:32]]['parsed']['total'] = '117.99'
        if failure == 'missing_reading': jobs[cases[0]['sha256'][:32]].pop('source_check')
        if failure == 'held': jobs[cases[0]['sha256'][:32]]['status'] = 'needs_review'
        engine = types.SimpleNamespace(read=lambda key: jobs[key], folder=self.folder,
                                       lock=threading.Lock(), save=Mock())
        calls = []
        def call(route):
            self.assertEqual(os.environ['APP_AUTOMATION_POSTING'], '0')
            calls.append(route)
            if failure == 'read_error': raise OSError('fake transient')
            result = {'bill': {'bill_id': route.split('/')[-1], 'total': '118.00'}}
            if failure == 'record_changed' and len(calls) > 2: result['bill']['total'] = '119.00'
            return result
        client = types.SimpleNamespace(call=call)
        def atomic(path, payload): path.write_text(json.dumps(payload))
        automation = types.SimpleNamespace(Zoho=lambda: client, MONEY={'total'}, POLICY='synthetic-policy',
                                           TERMINAL={'duplicate', 'needs_review'}, atomic=atomic)
        app = types.SimpleNamespace(connect=sqlite3.connect)
        return cases, digest, engine, app, automation, calls

    def test_both_duplicates_and_unchanged_records_enable_posting(self):
        cases, digest, engine, app, automation, calls = self.runtime()
        with contextlib.redirect_stdout(io.StringIO()):
            start.run_original_checks(engine, self.folder, self.release, cases, digest, app, automation)
        self.assertEqual(os.environ['APP_AUTOMATION_POSTING'], '1')
        self.assertEqual(len(calls), 4)
        self.assertTrue(start.accepted_receipt(self.folder / 'owner-original-acceptance.json', self.release, digest))
        with contextlib.closing(sqlite3.connect(self.folder / 'review.sqlite3')) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM invoices').fetchone()[0], 2)
            self.assertEqual(db.execute('SELECT count(*) FROM suggestions').fetchone()[0], 2)
        with contextlib.redirect_stdout(io.StringIO()):
            start.run_original_checks(engine, self.folder, self.release, cases, digest, app, automation)
        with contextlib.closing(sqlite3.connect(self.folder / 'review.sqlite3')) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM invoices').fetchone()[0], 2)

    def test_mismatch_missing_reading_hold_and_read_failure_keep_posting_off(self):
        for failure in ('wrong_bill', 'wrong_total', 'missing_reading', 'held', 'record_changed', 'read_error'):
            with self.subTest(failure=failure):
                db = self.folder / 'review.sqlite3'
                if db.exists(): db.unlink()
                cases, digest, engine, app, automation, calls = self.runtime(failure)
                os.environ['APP_AUTOMATION_POSTING'] = '1'
                with contextlib.redirect_stdout(io.StringIO()):
                    start.run_original_checks(engine, self.folder, self.release, cases, digest, app, automation)
                self.assertEqual(os.environ['APP_AUTOMATION_POSTING'], '0')
                self.assertFalse(start.accepted_receipt(self.folder / 'owner-original-acceptance.json', self.release, digest))

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
