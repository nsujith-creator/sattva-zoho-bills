# Releasing the owner bills app (books.sattvaglobal.in)

Railway service `sattva-owner-bills`, project `sattva-bills-review`, environment `acceptance`.

Read this before every release. The app refuses to start unless three things agree on the commit being deployed. A merge alone deploys nothing, because the service source is pinned to a commit.

## Before you start

- **Every deploy causes downtime** of 1–3 minutes. The service has a volume (`/data`), so Railway stops the old container before starting the new one. If the new one fails, the site stays down until you fix it or roll back. Deploy when no one needs the site.
- Never paste secrets or the hash JSON into the Railway Console. The Console is a shell, not a notes field.

## Steps

1. **Merge the PR** on GitHub. Copy the full merge commit SHA (40 characters) from the PR page or from `git log -1 main`.

2. **Generate the release values** from a checkout of that commit:

   ```bash
   python tools/release_pin.py <merge-sha>
   ```

   It prints two lines: `RELEASE_COMMIT=…` and `EXPECTED_AUTOMATION_SOURCE_SHA256=…`.

3. **Update the acceptance manifest on the volume** (Railway → service → *Console*), with the same SHA:

   ```bash
   python3 -c "import json,pathlib;p=pathlib.Path('/data/owner/acceptance/manifest.json');d=json.loads(p.read_text());d['release']='<merge-sha>';p.write_text(json.dumps(d,indent=2))"
   ```

   The running container only reads this file at startup, so changing it now is safe. **Skipping this step is what caused the 52-minute outage on 20 Nov 2026**: `load_cases` refuses a manifest whose `release` differs from `RELEASE_COMMIT`.

4. **Set the two variables** (Railway → service → *Variables*): paste the values from step 2 into `RELEASE_COMMIT` and `EXPECTED_AUTOMATION_SOURCE_SHA256`. Railway stages the change; do not deploy yet.

5. **Point the service at the new commit and deploy**: Settings → Source → set the commit to `<merge-sha>` (or use *Deploy latest commit*), then press **Deploy** on the staged changes. Variables and source go out together in one deploy.

6. **Verify** (about 3 minutes):
   - The deployment shows **ACTIVE**, not FAILED or CRASHED.
   - `https://books.sattvaglobal.in/healthz` returns 200.
   - The logs show the retained acceptance check passing (`all_pass true`), after which automatic posting turns itself on.
   - Sign in and open `/leakage`; it loads.

## If it fails

- **Deployment FAILED / CRASHED.** Open its logs. `Acceptance must match this release` means step 3 was missed or used a different SHA: fix the manifest in the Console, then *Redeploy*. A source-hash mismatch means step 4's values came from a different commit than step 5's.
- **Rollback** (Deployments → previous ACTIVE one → *Rollback*) restores that deployment's image **and** variables. The manifest on the volume is not rolled back: set its `release` back to the old SHA first, or the rollback fails the same way.
- **Restart** re-runs the same deployment with its own config. It never picks up new variables or a new commit.

## Adding a Python file to the image

Any new `.py` copied in `cloud_review/Dockerfile` must also be added to `SOURCE_NAMES` in `cloud_review/owner_start.py`, to `tools/release_pin.py`, and to `.dockerignore`. `ReleasePinCoverage` in `test_leakage_app.py` fails if one is missing.
