"""Start the retained owner inbox from repository code and private volume inputs."""
import contextlib
import copy
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


def job_fingerprint(job):
    return hashlib.sha256(json.dumps(job, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def reading_checks(case, job, automation):
    first, second = job.get('parsed'), job.get('source_check')
    checks = {}
    for label, reading in (('first', first), ('second', second)):
        for key, expected in case['expected'].items():
            try:
                value = reading.get(key) if isinstance(reading, dict) else None
                if key in automation.MONEY:
                    number, wanted = decimal.Decimal(str(value)), decimal.Decimal(str(expected))
                    ok = number.is_finite() and wanted.is_finite() and number == wanted
                else:
                    ok = value == expected
            except (ValueError, TypeError, decimal.InvalidOperation):
                ok = False
            checks[label + '.' + key] = ok
    complete = lambda value: (isinstance(value, dict) and bool(value)
                             and value.get('confidence') == 'high'
                             and value.get('issues') in ([], '', None)
                             and value.get('multi_invoice_count') == 1)
    try:
        canonical, evidence = automation.reconcile_readings(first, second, automation.source_text(case['pdf']), automation.load_core())
        checks['both_readings_retained'] = complete(first) and complete(second)
        checks['retained_canonical_reading'] = job.get('reconciled_parsed') == canonical
        checks['raw_readings_bound'] = (job.get('source_evidence', {}).get('raw_reading_sha256') == evidence.get('raw_reading_sha256')
                                          and evidence.get('raw_reading_sha256') is not None)
    except Exception:
        checks['both_readings_retained'] = False
        checks['retained_canonical_reading'] = False
        checks['raw_readings_bound'] = False
    checks['job_source_identity'] = (job.get('policy') == automation.POLICY
                                      and job.get('pdf_sha256') == case['sha256'])
    checks['reconciled_payload_retained'] = isinstance(job.get('payload'), dict) and bool(job['payload'])
    checks['worker_verification_retained'] = (isinstance(job.get('verification'), dict)
                                               and job['verification'].get('ok') is True
                                               and isinstance(job.get('source_evidence'), dict)
                                               and bool(job['source_evidence']))
    return checks


def retained_original(db, case):
    row = db.execute('SELECT id,sha256,pdf FROM invoices WHERE sha256=?', (case['sha256'],)).fetchone()
    if row is None:
        return None
    if (not re.fullmatch(r'[a-f0-9]{32}', row[0]) or not isinstance(row[2], bytes)
            or hashlib.sha256(row[2]).hexdigest() != case['sha256'] or row[2] != case['pdf']):
        raise RuntimeError('Retained original differs from the approved PDF')
    return row[0]


def required_checks(case):
    return ({label + '.' + key for label in ('first', 'second') for key in case['expected']}
            | {'both_readings_retained', 'retained_canonical_reading', 'raw_readings_bound', 'job_source_identity', 'worker_verification_retained', 'reconciled_payload_retained',
               'retained_original_matches', 'duplicate_without_creation', 'readback_verified',
               'existing_record_unchanged', 'source_corroborated'})


def accepted_receipt(path, release, manifest_hash, cases, folder, automation):
    if not path.exists():
        return False
    receipt = json.loads(read_private(path, 65536))
    if receipt.get('release') != release or receipt.get('manifest_sha256') != manifest_hash or receipt.get('all_pass') is not True:
        return False
    evidence = receipt.get('cases')
    if (receipt.get('case_count') != 2 or receipt.get('financial_creation_enabled_during_tests') is not False
            or not isinstance(evidence, list) or len(evidence) != 2):
        raise RuntimeError('Passed receipt lacks complete acceptance evidence')
    by_name = {item.get('case'): item for item in evidence if isinstance(item, dict)}
    if set(by_name) != {case['case'] for case in cases}:
        raise RuntimeError('Passed receipt case identities differ')
    with contextlib.closing(sqlite3.connect(folder / 'review.sqlite3')) as db:
        for case in cases:
            item = by_name[case['case']]
            doc_id = retained_original(db, case)
            if not doc_id or item.get('id') != doc_id:
                raise RuntimeError('Passed receipt has no matching retained original')
            job = json.loads(read_private(folder / 'automation' / (doc_id + '.json'), 2000000))
            checks = reading_checks(case, job, automation)
            saved_checks = item.get('checks') or {}
            if (item.get('passed') is not True or item.get('bill_id') != case['bill_id']
                    or item.get('pdf_sha256') != case['sha256'] or job.get('id') != doc_id
                    or job.get('status') != 'duplicate' or job.get('bill_id') != case['bill_id']
                    or item.get('job_sha256') != job_fingerprint(job)
                    or not all(checks.values()) or set(saved_checks) != required_checks(case)
                    or any(value is not True for value in saved_checks.values())
                    or not re.fullmatch(r'[a-f0-9]{64}', str(item.get('record_sha256', '')))):
                raise RuntimeError('Passed receipt or retained job evidence is incomplete or changed')
    return True


def read_existing(client, case):
    bill = client.call('bills/' + case['bill_id']).get('bill')
    if (not isinstance(bill, dict) or str(bill.get('bill_id')) != case['bill_id']
            or any(not str(bill.get(key) or '').strip() for key in ('vendor_id', 'bill_number', 'date', 'currency_code'))
            or not isinstance(bill.get('line_items'), list) or not bill['line_items']):
        raise RuntimeError('Existing bill read-back identity or fields are incomplete')
    for key in ('sub_total', 'tax_total', 'total'):
        try:
            value = bill[key]
            if isinstance(value, bool) or not decimal.Decimal(str(value)).is_finite():
                raise ValueError
        except (KeyError, ValueError, TypeError, decimal.InvalidOperation) as exc:
            raise RuntimeError('Existing bill numeric read-back is incomplete') from exc
    return bill


def enable_posting(engine, automation):
    os.environ['APP_AUTOMATION_POSTING'] = '1'
    with engine.lock:
        for path in engine.folder.glob('*.json'):
            if not re.fullmatch(r'[a-f0-9]{32}\.json', path.name):
                continue
            job = engine.read(path.stem)
            if job and job.get('status') == 'validated' and job.get('policy') == automation.POLICY:
                engine.save(job, 'validating', message='Source checks completed; approved automatic posting is now active.')


def rebuild_payload(parsed, case, doc_id, client, automation, core):
    vendors = {}
    for contact in client.all('contacts', 'contacts', {'contact_type': 'vendor', 'filter_by': 'Status.Active'}):
        name = contact.get('contact_name', '').strip().upper()
        entry = {'id': contact.get('contact_id'), 'gst_no': contact.get('gst_no') or ''}
        if name in vendors:
            vendors[name].setdefault('duplicates', []).append(entry)
        else:
            vendors[name] = entry
    payload = automation.validate(copy.deepcopy(parsed), core, vendors, check_zoho_rounding=False)
    marker = 'Sattva source SHA256 ' + case['sha256'] + '; upload ' + doc_id
    payload['notes'] = str(payload.get('notes') or '') + '\n' + marker
    return payload


def retained_case_jobs(folder, cases, automation):
    retained = []
    with contextlib.closing(sqlite3.connect(folder / 'review.sqlite3')) as db:
        for case in cases:
            doc_id = retained_original(db, case)
            if not doc_id:
                raise RuntimeError('Approved original missing; no extra extraction is authorized')
            job = json.loads(read_private(folder / 'automation' / (doc_id + '.json'), 2000000))
            if (job.get('id') != doc_id or job.get('policy') != automation.POLICY
                    or job.get('pdf_sha256') != case['sha256'] or job.get('create_attempted')
                    or (job.get('bill_id') is not None and job['bill_id'] != case['bill_id'])
                    or job.get('status') not in {'duplicate', 'needs_review'}
                    or not isinstance(job.get('parsed'), dict) or not job['parsed']
                    or not isinstance(job.get('source_check'), dict) or not job['source_check']):
                raise RuntimeError('Complete safe retained readings required; no extra extraction authorized')
            saved = db.execute('SELECT payload_json FROM suggestions WHERE invoice_id=?', (doc_id,)).fetchone()
            intent = {'kind': 'automatic_upload_intent', 'policy': automation.POLICY,
                      'id': doc_id, 'pdf_sha256': case['sha256']}
            if not saved or json.loads(saved[0]) != intent:
                raise RuntimeError('Approved source intent missing or changed')
            retained.append((case, doc_id, job))
    return retained


def backup_acceptance_jobs(folder, release, retained):
    path = folder / ('acceptance-jobs-before-' + release + '.json')
    if path.exists():
        saved = json.loads(read_private(path, 5000000))
        if saved.get('release') != release or set(saved.get('jobs', {})) != {doc_id for _, doc_id, _ in retained}:
            raise RuntimeError('Retained acceptance backup identity differs')
        return
    receipt = folder / 'owner-original-acceptance.json'
    saved = {'release': release, 'jobs': {doc_id: job for _, doc_id, job in retained},
             'receipt': json.loads(read_private(receipt, 65536)) if receipt.exists() else None}
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf8') as stream:
        json.dump(saved, stream, allow_nan=False); stream.flush(); os.fsync(stream.fileno())
    if os.name == 'posix':
        descriptor = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try: os.fsync(descriptor)
        finally: os.close(descriptor)


def run_original_checks(engine, folder, release, cases, manifest_hash, app, automation):
    receipt_path = folder / 'owner-original-acceptance.json'
    os.environ['APP_AUTOMATION_POSTING'] = '0'
    try:
        client = automation.Zoho()
        before = {case['case']: read_existing(client, case) for case in cases}
        with engine.lock:
            retained = retained_case_jobs(folder, cases, automation)
            backup_acceptance_jobs(folder, release, retained)
            result = {'release': release, 'manifest_sha256': manifest_hash, 'case_count': 2,
                      'all_pass': True, 'financial_creation_enabled_during_tests': False, 'cases': []}
            for case, doc_id, prior_job in retained:
                job = engine.read(doc_id) or {}
                if job != prior_job:
                    raise RuntimeError('Acceptance job changed during snapshot')
                core = engine.core or automation.load_core()
                parsed, evidence = automation.reconcile_readings(job.get('parsed'), job.get('source_check'),
                                                                  engine.source_reader(case['pdf']), core)
                payload = rebuild_payload(parsed, case, doc_id, client, automation, core)
                if job.get('payload') and job['payload'] != payload:
                    raise RuntimeError('Retained payload differs from current source/vendor validation')
                after = read_existing(client, case)
                detail = automation.verify_detail(core, case['bill_id'], payload, parsed, after, owned=False)
                scalar_checks = reading_checks(case, job, automation)
                scalar_ok = all(value for key, value in scalar_checks.items() if key.startswith(('first.', 'second.')))
                safe = detail.get('ok') is True and after == before[case['case']] and scalar_ok
                if safe:
                    engine.save(job, 'duplicate', payload=payload, verification=detail,
                                reconciled_parsed=parsed, source_evidence=evidence, bill_id=case['bill_id'],
                                acceptance_rechecked_from_retained=True,
                                message='Retained readings rechecked against unchanged existing bill; no creation.')
                checks = reading_checks(case, job, automation)
                checks['job_source_identity'] = checks['job_source_identity'] and job.get('id') == doc_id
                with contextlib.closing(app.connect(folder / 'review.sqlite3')) as db:
                    checks['retained_original_matches'] = retained_original(db, case) == doc_id
                checks['duplicate_without_creation'] = (job.get('status') == 'duplicate'
                    and job.get('bill_id') == case['bill_id'] and not job.get('create_attempted'))
                checks['existing_record_unchanged'] = after == before[case['case']]
                checks['source_corroborated'] = isinstance(evidence, dict) and bool(evidence)
                checks['readback_verified'] = detail.get('ok') is True
                passed = set(checks) == required_checks(case) and all(value is True for value in checks.values())
                result['cases'].append({'case': case['case'], 'id': doc_id, 'bill_id': case['bill_id'],
                    'pdf_sha256': case['sha256'], 'job_sha256': job_fingerprint(job),
                    'record_sha256': job_fingerprint(after), 'status': job.get('status'), 'passed': passed, 'checks': checks})
                result['all_pass'] = result['all_pass'] and passed
        automation.atomic(receipt_path, result)
        if result['all_pass']: enable_posting(engine, automation)
        print('CLOUD_ORIGINAL_ACCEPTANCE ' + json.dumps({
            'release': release, 'all_pass': result['all_pass'], 'retained_readings_only': True,
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
    retained_case_jobs(folder, cases, automation)  # Before worker: no new model calls for acceptance.
    prior_pass = accepted_receipt(folder / 'owner-original-acceptance.json', release, manifest_hash, cases, folder, automation)
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
