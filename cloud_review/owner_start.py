"""Start the retained owner inbox from repository code and private volume inputs."""
import contextlib
import decimal
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import subprocess
import threading
import time
from datetime import datetime, timezone
import container_start

SOURCE_NAMES = {'app.py', 'review.py', 'extraction.py', 'auth.py',
                'container_start.py', 'automation.py', 'zoho_core.py', 'owner_start.py'}
RETAINED = ('review.sqlite3', 'pre-r10-acceptance.sqlite3', 'acceptance-r10-backup.sqlite3')


def read_private(path, maximum):
    if path.is_symlink():
        raise RuntimeError('Symlink acceptance input refused')
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(descriptor, 'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise RuntimeError('Acceptance input must be a regular file')
        data = stream.read(maximum + 1)
    if len(data) > maximum:
        raise RuntimeError('Acceptance input exceeds its limit')
    return data


def load_cases(folder, release):
    directory = folder / 'acceptance'
    if directory.is_symlink():
        raise RuntimeError('Symlink acceptance folder refused')
    manifest = read_private(directory / 'manifest.json', 65536)
    config = json.loads(manifest)
    cases = config['cases']
    if config.get('release') != release or len(cases) != 2:
        raise RuntimeError('Acceptance must match this release and the two approved cases')
    seen_hashes, seen_ids, seen_names = set(), set(), set()
    loaded = []
    for case in cases:
        if set(case) != {'case', 'bill_id', 'filename', 'sha256', 'expected'}:
            raise RuntimeError('Unsupported acceptance manifest fields')
        name, digest, bill_id = case['filename'], case['sha256'], case['bill_id']
        if (not isinstance(name, str) or not name.lower().endswith('.pdf') or
                '/' in name or chr(92) in name or name in {'.', '..'} or
                not re.fullmatch(r'[a-f0-9]{64}', digest) or
                not re.fullmatch(r'[0-9]+', bill_id) or
                not isinstance(case['expected'], dict) or not case['expected'] or
                digest in seen_hashes or bill_id in seen_ids or case['case'] in seen_names):
            raise RuntimeError('Invalid or repeated acceptance case')
        pdf = read_private(directory / name, 5000000)
        if not pdf.startswith(b'%PDF-') or hashlib.sha256(pdf).hexdigest() != digest:
            raise RuntimeError('Acceptance PDF differs from its approved hash')
        loaded.append(dict(case, pdf=pdf))
        seen_hashes.add(digest); seen_ids.add(bill_id); seen_names.add(case['case'])
    return loaded, hashlib.sha256(manifest).hexdigest()


def accepted_receipt(path, release, manifest_hash):
    if not path.exists():
        return False
    receipt = json.loads(read_private(path, 65536))
    return (receipt.get('release') == release and receipt.get('manifest_sha256') == manifest_hash
            and receipt.get('all_pass') is True and receipt.get('case_count') == 2)


def enable_posting(engine, automation):
    os.environ['APP_AUTOMATION_POSTING'] = '1'
    with engine.lock:
        for path in engine.folder.glob('*.json'):
            if not re.fullmatch(r'[a-f0-9]{32}\.json', path.name):
                continue
            job = engine.read(path.stem)
            if job and job.get('status') == 'validated' and job.get('policy') == automation.POLICY:
                engine.save(job, 'validating', message='Source checks completed; approved automatic posting is now active.')


def run_original_checks(engine, folder, release, cases, manifest_hash, app, automation):
    receipt_path = folder / 'owner-original-acceptance.json'
    os.environ['APP_AUTOMATION_POSTING'] = '0'
    try:
        client = automation.Zoho()
        before = {case['case']: client.call('bills/' + case['bill_id'])['bill'] for case in cases}
        ids = []
        with contextlib.closing(app.connect(folder / 'review.sqlite3')) as db, db:
            for case in cases:
                row = db.execute('SELECT id FROM invoices WHERE sha256=?', (case['sha256'],)).fetchone()
                doc_id = row[0] if row else case['sha256'][:32]
                if not row:
                    db.execute('INSERT INTO invoices VALUES(?,?,?,?,?)',
                               (doc_id, case['filename'], case['sha256'], datetime.now(timezone.utc).isoformat(), case['pdf']))
                    intent = {'kind': 'automatic_upload_intent', 'policy': automation.POLICY,
                              'id': doc_id, 'pdf_sha256': case['sha256']}
                    db.execute('INSERT INTO suggestions VALUES(?,?,?)',
                               (doc_id, json.dumps(intent), datetime.now(timezone.utc).isoformat()))
                ids.append(doc_id)
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            jobs = [engine.read(doc_id) for doc_id in ids]
            if all(job and job['status'] in automation.TERMINAL for job in jobs):
                break
            time.sleep(1)
        result = {'release': release, 'manifest_sha256': manifest_hash, 'case_count': 2,
                  'all_pass': True, 'financial_creation_enabled_during_tests': False, 'cases': []}
        for case, doc_id in zip(cases, ids):
            job = engine.read(doc_id) or {}
            parsed = job.get('parsed') or {}
            checks = {}
            for key, expected in case['expected'].items():
                try:
                    checks[key] = (decimal.Decimal(str(parsed.get(key))) == decimal.Decimal(expected)
                                   if key in automation.MONEY else parsed.get(key) == expected)
                except (ValueError, TypeError, decimal.InvalidOperation):
                    checks[key] = False
            checks['duplicate_without_creation'] = job.get('status') == 'duplicate' and job.get('bill_id') == case['bill_id']
            checks['both_readings_retained'] = isinstance(job.get('source_check'), dict)
            checks['existing_record_unchanged'] = client.call('bills/' + case['bill_id'])['bill'] == before[case['case']]
            passed = all(checks.values())
            result['cases'].append({'case': case['case'], 'id': doc_id, 'status': job.get('status'),
                                    'passed': passed, 'checks': checks})
            result['all_pass'] = result['all_pass'] and passed
        automation.atomic(receipt_path, result)
        if result['all_pass']:
            enable_posting(engine, automation)
        print('CLOUD_ORIGINAL_ACCEPTANCE ' + json.dumps({
            'release': release, 'all_pass': result['all_pass'],
            'automatic_posting_enabled': os.environ['APP_AUTOMATION_POSTING'] == '1',
            'cases': [{'case': item['case'], 'status': item['status'], 'passed': item['passed'],
                       'failed_checks': [key for key, ok in item['checks'].items() if not ok]}
                      for item in result['cases']]}), flush=True)
    except Exception as exc:
        os.environ['APP_AUTOMATION_POSTING'] = '0'
        result = {'release': release, 'manifest_sha256': manifest_hash,
                  'case_count': 2, 'all_pass': False, 'error_type': type(exc).__name__}
        automation.atomic(receipt_path, result)
        print('CLOUD_ORIGINAL_ACCEPTANCE ' + json.dumps(result), flush=True)


def prepare_owner():
    os.environ['APP_AUTOMATION_POSTING'] = '0'
    if os.environ.get('DATA_DIR') != '/data':
        raise RuntimeError('Owner startup requires DATA_DIR=/data')
    stop_at = int(os.environ['OWNER_INBOX_STOP_AT_EPOCH'])
    remaining = stop_at - time.time()
    if not 0 < remaining <= 7 * 86400:
        raise RuntimeError('Approved service window expired or exceeds its bound')
    timer = threading.Timer(remaining, lambda: os._exit(0))
    timer.daemon = True; timer.start()
    container_start.prepare_and_drop()
    folder = Path('/data/owner')
    descriptor = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 65532:
            raise RuntimeError('Existing private owner directory required')
        os.fchmod(descriptor, 0o700)
    finally:
        os.close(descriptor)
    if not (folder / '.owner-inbox-initialized').is_file() or not (folder / 'review.sqlite3').is_file():
        raise RuntimeError('Existing owner inbox required')
    release = os.environ['RELEASE_COMMIT']
    if not re.fullmatch(r'[a-f0-9]{40}', release):
        raise RuntimeError('Pinned release required')
    expected = json.loads(os.environ['EXPECTED_AUTOMATION_SOURCE_SHA256'])
    if set(expected) != SOURCE_NAMES:
        raise RuntimeError('Complete reviewed-source manifest required')
    actual = {name: hashlib.sha256(Path('/app', name).read_bytes()).hexdigest() for name in expected}
    if actual != expected:
        raise RuntimeError('Deployed files differ from the approved source')
    retained = {name: hashlib.sha256(Path('/data', name).read_bytes()).hexdigest() for name in RETAINED}
    if retained['acceptance-r10-backup.sqlite3'] != os.environ['EXPECTED_SYNTHETIC_BACKUP_SHA256']:
        raise RuntimeError('Retained synthetic backup changed')
    backup = folder / ('pre-owner-launch-' + release[:12] + '.sqlite3')
    if not backup.exists():
        with contextlib.closing(sqlite3.connect(folder / 'review.sqlite3')) as source, contextlib.closing(sqlite3.connect(backup)) as target:
            if source.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise RuntimeError('Owner database integrity check failed')
            source.backup(target)
    with contextlib.closing(sqlite3.connect(backup)) as db:
        if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise RuntimeError('Owner backup integrity check failed')
        count = db.execute('SELECT COUNT(*) FROM invoices').fetchone()[0]
    os.chmod(backup, 0o600)
    if {name: hashlib.sha256(Path('/data', name).read_bytes()).hexdigest() for name in RETAINED} != retained:
        raise RuntimeError('Retained data changed during startup')
    if not shutil.which('pdfinfo') or not shutil.which('pdftotext'):
        raise RuntimeError('Approved PDF readers missing')
    subprocess.run(['pdftotext', '-v'], capture_output=True, timeout=10, check=True)
    if os.environ['APP_AI_BUDGET_USD'] != '0.50':
        raise RuntimeError('Unapproved AI allowance')
    os.environ['DATA_DIR'] = str(folder)
    return folder, release, {'release': release, 'uid': os.geteuid(), 'gid': os.getegid(),
                            'owner_directory': str(folder), 'source_sha256': actual,
                            'backup_sha256': hashlib.sha256(backup.read_bytes()).hexdigest(),
                            'backed_up_original_count': count, 'retained_synthetic_hashes': retained,
                            'stop_at': stop_at, 'posting_enabled': False, 'ai_allowance_usd': '0.50'}


def main():
    folder, release, proof = prepare_owner()
    cases, manifest_hash = load_cases(folder, release)
    import app
    import automation
    if not app.automation_enabled():
        raise RuntimeError('Approved cloud automation policy required')
    automation.load_core()
    print('AUTOMATION_LAUNCH_PROOF ' + json.dumps(proof), flush=True)
    prior_pass = accepted_receipt(folder / 'owner-original-acceptance.json', release, manifest_hash)
    original_server = app.Server

    class OwnerServer(original_server):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            if prior_pass:
                enable_posting(self.automation, automation)
                print('CLOUD_ORIGINAL_ACCEPTANCE ' + json.dumps({
                    'release': release, 'retained_acceptance_passed': True,
                    'automatic_posting_enabled': True}), flush=True)
            else:
                threading.Thread(target=run_original_checks,
                                 args=(self.automation, folder, release, cases, manifest_hash, app, automation),
                                 daemon=True, name='approved-original-acceptance').start()

    app.Server = OwnerServer
    app.main()


if __name__ == '__main__':
    main()
