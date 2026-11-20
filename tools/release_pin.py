"""Print Railway release values for a merged commit: python tools/release_pin.py <sha>"""
import hashlib, json, subprocess, sys
sha = subprocess.check_output(['git', 'rev-parse', sys.argv[1]], text=True).strip()
files = {n: 'cloud_review/'+n for n in ('app.py', 'review.py', 'extraction.py', 'auth.py',
         'container_start.py', 'automation.py', 'owner_start.py', 'leakage.py', 'leakage_app.py')}
files['zoho_core.py'] = 'src/zoho_bills.py'
hashes = {k: hashlib.sha256(subprocess.check_output(['git', 'show', f'{sha}:{v}'])).hexdigest()
          for k, v in files.items()}
print('RELEASE_COMMIT='+sha)
print('EXPECTED_AUTOMATION_SOURCE_SHA256='+json.dumps(hashes, separators=(',', ':')))
