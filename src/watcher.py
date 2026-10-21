"""
Zoho Bills Watcher v2 - File-aware, notification-first.

Key fixes over v1:
1. Tracks files BY NAME - no silent misses when watcher restarts
2. Instant desktop notification the moment a new PDF is detected
3. Detailed per-file result notification: success / exact error
4. Auto-restarts via watchdog task if crashed
5. Startup notification so you always know it's alive
"""
import os, time, subprocess, json, sys, hashlib, uuid, datetime, base64

PYTHON_EXE   = r"C:\Users\sujit\AppData\Local\Programs\Python\Python312\python.exe"
MAIN_SCRIPT  = r"C:\temp\zoho\zoho_bills.py"
BILL_FOLDER  = r"C:\temp\bills"
LOG_FILE     = r"C:\temp\zoho\watcher.log"
LOCK_FILE    = r"C:\temp\zoho\watcher.lock"
SEEN_FILE    = r"C:\temp\zoho\seen_files.json"   # tracks processed filenames
INVOICE_LOG_FILE = r"C:\temp\zoho\invoice_log.json"
SYSTEM_HEALTH_FILE = r"C:\temp\zoho\system_health.json"
PENDING_FILE = r"C:\temp\zoho\pending_files.json"   # non-terminal outcomes awaiting change/backoff
QUOTA_HOLD_FILE = r"C:\temp\zoho\zoho_quota_hold.json"
WATCHER_HEARTBEAT_MAX_AGE_SECONDS = 300
# Live removals are disabled by default because historical invoice_log bill_numbers may be wrong.
SEEN_RECON_APPLY_REMOVALS = False

# Zoho Books uses a daily quota that resets just before midnight IST.  The
# extra five minutes means the watcher never probes a just-reset quota edge.
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
QUOTA_RESUME_BUFFER_SECONDS = 300


def _hidden_subprocess_creationflags():
    """Return Windows no-console creation flags while staying safe elsewhere."""
    if os.name != "nt":
        return 0
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


# Session 1: run_id generation
def generate_run_id():
    """Deterministic run ID: YYYYMMDD-HHMMSS-<8hex>. Stdlib only."""
    ts  = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    uid = uuid.uuid4().hex[:8]
    return f"{ts}-{uid}"


def safe_json_read(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def atomic_json_write(path, data):
    directory = os.path.dirname(os.path.abspath(path))
    tmp_path = os.path.join(directory, f".{os.path.basename(path)}.{os.getpid()}.tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, path)


def write_watcher_heartbeat(status="running", extra=None):
    try:
        health = safe_json_read(SYSTEM_HEALTH_FILE, {})
        if not isinstance(health, dict):
            health = {}

        watcher = {
            "status": status,
            "pid": os.getpid(),
            "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "heartbeat_epoch": time.time(),
        }
        if extra:
            watcher.update(extra)

        health["watcher"] = watcher
        atomic_json_write(SYSTEM_HEALTH_FILE, health)
    except Exception:
        pass


# PREFLIGHT TEST GATE
def enforce_test_gate():
    """
    Runs MBL extraction test harness before watcher starts.
    If any test fails, watcher refuses to start.
    Skips silently if test suite not yet installed.
    """
    test_script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "test", "run_tests.py")

    if not os.path.exists(test_script):
        print(f"[INFO] Test harness not found at {test_script} - skipping preflight")
        return  # Non-blocking until test suite is deployed

    result = subprocess.run(
        [PYTHON_EXE, test_script],
        capture_output=False,  # Print directly to console
        creationflags=_hidden_subprocess_creationflags(),
    )

    if result.returncode != 0:
        print("\nPREFLIGHT FAILED - WATCHER WILL NOT START")
        print("   Fix extraction issues in extract_mbl.py then restart.\n")
        sys.exit(1)

    print("Preflight passed - watcher starting\n")


# Logging
def log(message):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"{ts}  {message}"
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    print(line)


def _process_exists(pid):
    if not pid or pid <= 0:
        return False
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=_hidden_subprocess_creationflags(),
        )
    except Exception:
        return True
    return f'"{pid}"' in result.stdout


def cleanup_watcher_lock():
    """Remove watcher.lock only when it belongs to this process."""
    try:
        with open(LOCK_FILE, encoding="utf-8") as f:
            lock_pid = int(f.read().strip())
    except Exception:
        return False

    if lock_pid != os.getpid():
        return False

    try:
        with open(LOCK_FILE, encoding="utf-8") as f:
            lock_pid = int(f.read().strip())
    except Exception:
        return False

    if lock_pid != os.getpid():
        return False

    try:
        os.remove(LOCK_FILE)
        return True
    except Exception:
        return False


# Desktop notifications
def _notification_call_succeeded(result, title, stage):
    if result:
        return True
    log(f"[NOTIFY_FAILED] stage={stage} title={title}: Shell_NotifyIconW returned 0")
    return False


def _notify_with_powershell(title, message, icon):
    """Fallback when Shell_NotifyIcon cannot create an icon in this process."""
    icon_name = {
        "warning": "Warning",
        "error": "Error",
    }.get(str(icon).lower(), "Information")

    def ps_literal(value, limit):
        value = str(value).replace("\r", " ").replace("\n", " ")[:limit]
        return "'" + value.replace("'", "''") + "'"

    script = (
        "$ErrorActionPreference = 'Stop'; "
        "Add-Type -AssemblyName System.Windows.Forms; "
        "$n = New-Object System.Windows.Forms.NotifyIcon; "
        f"$n.Icon = [System.Drawing.SystemIcons]::{icon_name}; "
        f"$n.BalloonTipTitle = {ps_literal(title, 63)}; "
        f"$n.BalloonTipText = {ps_literal(message, 255)}; "
        "$n.Visible = $true; $n.ShowBalloonTip(10000); "
        "Start-Sleep -Seconds 4; $n.Dispose()"
    )
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden",
             "-EncodedCommand", encoded],
            capture_output=True, text=True, timeout=15,
            creationflags=_hidden_subprocess_creationflags(),
        )
    except Exception as e:
        log(f"[NOTIFY_FAILED] fallback title={title}: {type(e).__name__}: {e}")
        return False
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "no error detail").strip().replace("\n", " ")[:160]
        log(f"[NOTIFY_FAILED] fallback title={title}: exit={result.returncode} detail={detail}")
        return False
    log(f"[NOTIFY_FALLBACK_DISPATCHED] title={title}")
    return True


def notify(title, message, icon="Information"):
    """
    Desktop notification without PowerShell/cmd/subprocess.
    Uses Windows Shell_NotifyIcon balloon via ctypes.
    """
    try:
        import ctypes
        from ctypes import wintypes

        title = str(title)[:63]
        message = str(message)[:255]

        NIM_ADD = 0x00000000
        NIM_MODIFY = 0x00000001
        NIM_DELETE = 0x00000002
        NIF_MESSAGE = 0x00000001
        NIF_ICON = 0x00000002
        NIF_TIP = 0x00000004
        NIF_INFO = 0x00000010
        NIIF_INFO = 0x00000001
        NIIF_WARNING = 0x00000002
        NIIF_ERROR = 0x00000003

        icon_flag = NIIF_INFO
        if str(icon).lower() == "warning":
            icon_flag = NIIF_WARNING
        elif str(icon).lower() == "error":
            icon_flag = NIIF_ERROR

        class NOTIFYICONDATAW(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("hWnd", wintypes.HWND),
                ("uID", wintypes.UINT),
                ("uFlags", wintypes.UINT),
                ("uCallbackMessage", wintypes.UINT),
                ("hIcon", wintypes.HICON),
                ("szTip", wintypes.WCHAR * 128),
                ("dwState", wintypes.DWORD),
                ("dwStateMask", wintypes.DWORD),
                ("szInfo", wintypes.WCHAR * 256),
                ("uTimeoutOrVersion", wintypes.UINT),
                ("szInfoTitle", wintypes.WCHAR * 64),
                ("dwInfoFlags", wintypes.DWORD),
                ("guidItem", ctypes.c_byte * 16),
                ("hBalloonIcon", wintypes.HICON),
            ]

        shell32 = ctypes.windll.shell32
        user32 = ctypes.windll.user32

        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = None
        nid.uID = 9876
        nid.uFlags = NIF_ICON | NIF_TIP | NIF_INFO
        nid.hIcon = user32.LoadIconW(None, 32516)  # IDI_INFORMATION
        nid.szTip = "Zoho Bills Watcher"
        nid.szInfo = message
        nid.szInfoTitle = title
        nid.dwInfoFlags = icon_flag
        nid.uTimeoutOrVersion = 12000

        added = shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))
        if not _notification_call_succeeded(added, title, "add"):
            return _notify_with_powershell(title, message, icon)
        shown = shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))
        if not _notification_call_succeeded(shown, title, "show"):
            return _notify_with_powershell(title, message, icon)
        return True

        # Do not sleep here; remove icon later is not worth spawning timers/subprocesses.
        # Windows usually clears the balloon automatically.
    except Exception as e:
        try:
            log(f"[NOTIFY_FAILED] {title}: {e}")
        except Exception:
            pass
        return _notify_with_powershell(title, message, icon)

def notify_detected(filename):
    """Instant notification the moment file is seen."""
    return notify(
        "Zoho: Bill Detected",
        f"Processing: {filename[:60]}",
        icon="Information"
    )


def notify_success(filename, bill_number, vendor):
    return notify(
        "Zoho: Bill Created",
        f"{bill_number} | {vendor[:30]} | {filename[:40]}",
        icon="Information"
    )


def notify_skipped(filename, bill_number):
    return notify(
        "Zoho: Already Exists",
        f"{bill_number} already in Zoho - {filename[:50]}",
        icon="Information"
    )


def notify_error(filename, error_detail):
    """Show exact error - no more vague 'check log' messages."""
    short_err = error_detail[:120] if error_detail else "Unknown error"
    return notify(
        "Zoho: Bill FAILED",
        f"{filename[:40]}\n{short_err}",
        icon="Error"
    )


def notify_needs_review(filename, reason):
    return notify(
        "Zoho: Needs Manual Review",
        f"{filename[:40]}: {reason[:80]}",
        icon="Warning"
    )


def should_mark_seen(status):
    """Only a verified bill or a confirmed duplicate is terminal (audit F03)."""
    return status in ("verified", "skipped")


# Seen-files tracker
def load_seen():
    """Load set of filenames already processed."""
    if os.path.exists(SEEN_FILE):
        try:
            with open(SEEN_FILE, encoding="utf-8") as f:
                return set(json.load(f))
        except Exception:
            pass
    return set()


def save_seen(seen: set):
    _tmp = SEEN_FILE + ".tmp"
    with open(_tmp, "w", encoding="utf-8") as f:
        json.dump(sorted(seen), f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(_tmp, SEEN_FILE)


# Pending tracker: non-terminal outcomes (needs_review / failed / error).
# Prevents the unbounded per-poll retry loop that exhausted the Zoho daily
# API quota on 2026-08-26. Files are still NOT marked seen, so they stay
# visible and retryable -- they are just not re-run until something changes.
PENDING_TRANSIENT_STATUSES = ("failed", "error")
PENDING_BACKOFF_BASE_SEC = 300
PENDING_BACKOFF_MAX_SEC = 3600
PENDING_MAX_ATTEMPTS = 12


def load_pending():
    if os.path.exists(PENDING_FILE):
        try:
            with open(PENDING_FILE, encoding="utf-8-sig") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {}


def save_pending(pending: dict):
    _tmp = PENDING_FILE + ".tmp"
    with open(_tmp, "w", encoding="utf-8") as f:
        json.dump(pending, f, indent=2, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(_tmp, PENDING_FILE)


def file_signature(filename):
    """(mtime, size) identity of a file in the bill folder, or None if absent."""
    try:
        st = os.stat(os.path.join(BILL_FOLDER, filename))
    except OSError:
        return None
    return [round(st.st_mtime, 3), st.st_size]


def pending_hold_reason(filename, entry, now=None):
    """Reason string if this file must NOT be re-run yet, else None."""
    if not isinstance(entry, dict):
        return None
    sig = file_signature(filename)
    if sig is None or sig != entry.get("signature"):
        return None  # re-dropped or replaced -> always allow a fresh attempt
    now = time.time() if now is None else now
    status = entry.get("status", "")
    attempts = int(entry.get("attempts", 0) or 0)
    if status not in PENDING_TRANSIENT_STATUSES:
        return f"held_awaiting_change status={status} attempts={attempts}"
    if attempts >= PENDING_MAX_ATTEMPTS:
        return f"held_max_attempts status={status} attempts={attempts}"
    next_ts = float(entry.get("next_attempt_ts", 0) or 0)
    if now < next_ts:
        return f"backoff status={status} attempts={attempts} retry_in={int(next_ts - now)}s"
    return None


def record_pending(pending, filename, status, detail):
    """Record a non-terminal outcome and schedule the next allowed attempt."""
    prev = pending.get(filename)
    prev = prev if isinstance(prev, dict) else {}
    sig = file_signature(filename)
    attempts = (int(prev.get("attempts", 0) or 0) + 1) if prev.get("signature") == sig else 1
    now = time.time()
    if status in PENDING_TRANSIENT_STATUSES:
        delay = min(PENDING_BACKOFF_BASE_SEC * (2 ** (attempts - 1)), PENDING_BACKOFF_MAX_SEC)
        next_ts = now + delay
    else:
        next_ts = 0
    pending[filename] = {
        "status": status,
        "reason": str(detail)[:300],
        "attempts": attempts,
        "signature": sig,
        "last_attempt_ts": now,
        "last_attempt_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        "next_attempt_ts": next_ts,
    }
    return pending[filename]


def is_quota_exhaustion(status, detail):
    """Fail closed on a rate-limit response from a watcher-launched run."""
    return status in PENDING_TRANSIENT_STATUSES and "http error 429" in str(detail).lower()


def next_ist_quota_resume_epoch(now=None):
    """Return 00:05 IST on the next calendar day after a daily-quota 429."""
    now = time.time() if now is None else now
    local_now = datetime.datetime.fromtimestamp(now, IST)
    next_day = (local_now + datetime.timedelta(days=1)).date()
    resume_at = datetime.datetime.combine(next_day, datetime.time(0, 0), tzinfo=IST)
    return resume_at.timestamp() + QUOTA_RESUME_BUFFER_SECONDS


def load_quota_hold():
    data = safe_json_read(QUOTA_HOLD_FILE, {})
    return data if isinstance(data, dict) else {}


def save_quota_hold(quota_hold):
    atomic_json_write(QUOTA_HOLD_FILE, quota_hold)


def quota_hold_reason(quota_hold, now=None):
    """Return the active daily quota hold reason, otherwise None."""
    try:
        resume_at = float(quota_hold.get("resume_at_epoch", 0))
    except (AttributeError, TypeError, ValueError):
        return None
    now = time.time() if now is None else now
    if now >= resume_at:
        return None
    resume_local = datetime.datetime.fromtimestamp(resume_at, IST).strftime("%Y-%m-%d %H:%M IST")
    return f"zoho_quota_exhausted resume_at={resume_local} retry_in={int(resume_at - now)}s"


def activate_quota_hold(detail, now=None):
    """Persist a no-call hold so restarts cannot resume quota-burning retries."""
    now = time.time() if now is None else now
    quota_hold = {
        "reason": str(detail)[:300],
        "activated_at_utc": datetime.datetime.fromtimestamp(now, datetime.timezone.utc).isoformat(),
        "resume_at_epoch": next_ist_quota_resume_epoch(now),
    }
    save_quota_hold(quota_hold)
    return quota_hold


def fetch_existing_bill_numbers_readonly():
    """
    Fetch existing Zoho bill numbers through zoho_bills.py read-only list mode.
    Returns a normalized set on success, or None when the list is unavailable.
    """
    try:
        result = subprocess.run(
            [PYTHON_EXE, MAIN_SCRIPT, "--list-existing-bills-json"],
            capture_output=True,
            text=True,
            timeout=120,
            creationflags=_hidden_subprocess_creationflags(),
        )
    except subprocess.TimeoutExpired:
        log("  [SEEN_RECON_ZOHO_LIST_FAILED] reason=timeout")
        return None
    except Exception as e:
        log(f"  [SEEN_RECON_ZOHO_LIST_FAILED] reason=subprocess_error type={type(e).__name__}")
        return None

    if result.returncode != 0:
        log(f"  [SEEN_RECON_ZOHO_LIST_FAILED] reason=returncode_{result.returncode}")
        return None

    try:
        payload = json.loads(result.stdout)
    except Exception:
        log("  [SEEN_RECON_ZOHO_LIST_FAILED] reason=invalid_json")
        return None

    if not isinstance(payload, dict):
        log("  [SEEN_RECON_ZOHO_LIST_FAILED] reason=malformed_json_root")
        return None

    if payload.get("ok") is not True:
        log("  [SEEN_RECON_ZOHO_LIST_FAILED] reason=ok_false")
        return None

    bill_numbers = payload.get("bill_numbers")
    if not isinstance(bill_numbers, list):
        log("  [SEEN_RECON_ZOHO_LIST_FAILED] reason=malformed_bill_numbers")
        return None

    existing = {
        str(bill_number).strip().upper()
        for bill_number in bill_numbers
        if str(bill_number).strip()
    }
    log(f"  [SEEN_RECON_ZOHO_LIST_OK] count={len(existing)}")
    return existing


def _load_invoice_log_for_reconcile(invoice_log_path=INVOICE_LOG_FILE):
    try:
        with open(invoice_log_path, encoding="utf-8") as f:
            invoice_log = json.load(f)
    except Exception as e:
        log(f"  [SEEN_RECON_SKIPPED] invoice_log unavailable path={invoice_log_path} error={type(e).__name__}")
        return None
    if not isinstance(invoice_log, list):
        log(f"  [SEEN_RECON_SKIPPED] invoice_log invalid path={invoice_log_path} reason=not_list")
        return None
    return invoice_log


def _latest_invoice_log_entries_by_file(invoice_log):
    latest = {}
    for entry in invoice_log:
        if not isinstance(entry, dict):
            continue
        filename = entry.get("file")
        if filename:
            latest[filename] = entry
    return latest


def _reconcile_seen_files_core(seen, invoice_log, existing_bills):
    """
    Pure reconciliation core. Conservative rule: only the latest invoice_log
    entry for a file may prove a created bill; newer non-created entries win.
    """
    if existing_bills is None:
        return set(seen), [], "existing_bills_unavailable"
    if invoice_log is None or not isinstance(invoice_log, list):
        return set(seen), [], "invoice_log_unavailable"

    existing = {
        str(bill_number).strip().upper()
        for bill_number in existing_bills
        if str(bill_number).strip()
    }
    latest_by_file = _latest_invoice_log_entries_by_file(invoice_log)
    updated = set(seen)
    removed = []

    for filename in sorted(set(seen)):
        latest = latest_by_file.get(filename)
        if not latest:
            continue
        if latest.get("status") not in ("created", "verified"):
            continue
        bill_number = str(latest.get("bill_number") or "").strip()
        if not bill_number:
            continue
        if bill_number.upper() in existing:
            continue
        updated.discard(filename)
        removed.append((filename, bill_number))

    return updated, removed, None


def reconcile_seen_files_with_invoice_log(seen, existing_bills=None, invoice_log_path=INVOICE_LOG_FILE):
    """
    Reconcile seen_files.json against invoice_log.json plus a supplied live Zoho
    bill-number set. This function never fetches Zoho directly.
    """
    original = set(seen)
    if existing_bills is None:
        log("  [SEEN_RECON_SKIPPED] reason=zoho_existing_bills_unavailable")
        return original

    invoice_log = _load_invoice_log_for_reconcile(invoice_log_path)
    updated, removed, skip_reason = _reconcile_seen_files_core(original, invoice_log, existing_bills)
    if skip_reason:
        log(f"  [SEEN_RECON_SKIPPED] reason={skip_reason}")
        return original

    if not removed:
        log(f"  [SEEN_RECON_COMPLETED] checked={len(original)} removed=0")
        return updated

    for filename, bill_number in removed:
        log(f"  SEEN_RECON_CANDIDATE file={filename} bill_number={bill_number}")

    if not SEEN_RECON_APPLY_REMOVALS:
        log(f"  SEEN_RECON_REPORT_ONLY checked={len(original)} candidates={len(removed)} applied=0")
        return original

    for filename, bill_number in removed:
        log(f"  [SEEN_RECON_REMOVED] file={filename} bill_number={bill_number}")
    save_seen(updated)
    log(f"  [SEEN_RECON_COMPLETED] checked={len(original)} removed={len(removed)}")
    return updated


def startup_reconcile_seen(seen):
    existing_bills = fetch_existing_bill_numbers_readonly()
    if existing_bills is None:
        log("  [SEEN_RECON_SKIPPED] reason=zoho_list_unavailable_no_seen_removal")
        return set(seen)
    return reconcile_seen_files_with_invoice_log(
        seen,
        existing_bills=existing_bills,
        invoice_log_path=INVOICE_LOG_FILE,
    )


# Network check
def is_network_up():
    try:
        import urllib.request
        urllib.request.urlopen("https://accounts.zoho.in", timeout=5)
        return True
    except Exception:
        return False


# Run pipeline for ONE specific file
def run_for_file(filename, run_id):
    """
    Run zoho_bills.py and parse its output to find the result for this file.
    Passes --run-id so zoho_bills.py can tag all its log events with this run_id.
    Returns (status, detail) where status in: verified / post_verify_failed /
    awaiting_approval / skipped / needs_review / failed / error. The watcher never
    passes --approve, so it can never post a bill (audit F04).
    """
    filepath = os.path.join(BILL_FOLDER, filename)
    SUBPROCESS_TIMEOUT = 240  # seconds - kills hung API calls
    _zlock = r"C:\temp\zoho\zoho.lock"
    # NOTE: pre-spawn lock deletion removed (audit F15).
    # Deleting zoho.lock before spawn can abort an active run.
    # zoho_bills.py manages its own lock lifecycle.
    proc = subprocess.Popen(
        [PYTHON_EXE, MAIN_SCRIPT, "--file", filepath, "--run-id", run_id],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        creationflags=_hidden_subprocess_creationflags(),
    )
    try:
        stdout, stderr = proc.communicate(timeout=SUBPROCESS_TIMEOUT)
        output = stdout + stderr
        # zoho.lock is owned by zoho_bills.py - watcher must not remove it
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()  # drain pipes after kill
        log(f"  [SUBPROCESS_TIMEOUT] run_id={run_id} file={filename} timeout={SUBPROCESS_TIMEOUT}s -- process killed")
        # zoho.lock is owned by zoho_bills.py - watcher must not remove it
        return "error", f"subprocess_timeout_{SUBPROCESS_TIMEOUT}s"

    # Log full output for debugging
    for line in output.splitlines():
        if line.strip():
            log(f"  | {line}")

    # Parse run_log.json for this specific file's result
    run_log_path = r"C:\temp\zoho\run_log.json"
    if os.path.exists(run_log_path):
        try:
            with open(run_log_path, encoding="utf-8") as f:
                run_log = json.load(f)

            for entry in run_log.get("verified", []):
                if entry.get("file") == filename:
                    return "verified", f"Bill ID: {entry.get('bill_id','')} | Bill#: {entry.get('bill_number','')}"

            for entry in run_log.get("post_verify_failed", []):
                if entry.get("file") == filename:
                    return "post_verify_failed", f"Bill ID: {entry.get('bill_id','')} | {entry.get('error','')}"

            for entry in run_log.get("awaiting_approval", []):
                if entry.get("file") == filename:
                    return "awaiting_approval", entry.get("reason", "awaiting_approval")

            for entry in run_log.get("skipped_duplicate", []):
                if entry.get("file") == filename:
                    return "skipped", f"Bill# {entry.get('bill_number','')} already in Zoho"

            for entry in run_log.get("needs_review", []):
                if entry.get("file") == filename:
                    return "needs_review", entry.get("reason", "Manual review required")

            for entry in run_log.get("failed", []):
                if entry.get("file") == filename:
                    return "failed", entry.get("error", "Unknown error")

        except Exception as e:
            log(f"  Could not parse run_log: {e}")

    # Fallback: check invoice_log.json
    inv_log_path = r"C:\temp\zoho\invoice_log.json"
    if os.path.exists(inv_log_path):
        try:
            with open(inv_log_path, encoding="utf-8") as f:
                inv_log = json.load(f)
            for entry in reversed(inv_log):
                if entry.get("file") == filename:
                    return entry.get("status", "unknown"), entry.get("error", "")
        except Exception:
            pass

    if proc.returncode != 0:
        lines = output.splitlines()
        err_lines = [l for l in lines if "error" in l.lower() or "failed" in l.lower() or "exception" in l.lower()]
        detail = err_lines[-1] if err_lines else output[-200:]
        return "error", detail

    return "unknown", "Could not determine result - check watcher.log"


# Main watcher loop
def run_watcher():
    # Write lock
    try:
        with open(LOCK_FILE, "x") as f:
            f.write(str(os.getpid()))
    except FileExistsError:
        existing_pid = None
        try:
            with open(LOCK_FILE, encoding="utf-8") as f:
                existing_pid = int(f.read().strip())
        except Exception:
            pass
        if _process_exists(existing_pid):
            print("Watcher already running.")
            sys.exit(0)
        try:
            os.remove(LOCK_FILE)
        except OSError as e:
            print(f"Watcher lock is stale but could not be removed: {e}")
            sys.exit(1)
        with open(LOCK_FILE, "x") as f:
            f.write(str(os.getpid()))

    write_watcher_heartbeat("running")

    log("=" * 60)
    log("  ZOHO BILLS WATCHER v2 STARTED")
    log(f"  Watching: {BILL_FOLDER}")
    log(f"  Poll: every 30 seconds")
    log("=" * 60)

    # Startup toast - so you know it's alive
    notify(
        "Zoho Watcher Active",
        f"Watching {BILL_FOLDER} - drop a PDF to auto-ingest",
        icon="Information"
    )

    seen = load_seen()
    seen = startup_reconcile_seen(seen)
    log(f"  Already-seen files: {len(seen)}")

    pending = load_pending()
    log(f"  Pending (non-terminal) files: {len(pending)}")
    held_logged = {}
    quota_hold = load_quota_hold()
    quota_hold_logged = None

    retry_queue = []  # files pending retry due to network

    while True:
        try:
            write_watcher_heartbeat("running")

            if not os.path.exists(BILL_FOLDER):
                log(f"ERROR: Folder {BILL_FOLDER} missing!")
                notify("Zoho Watcher Error", f"Bills folder missing: {BILL_FOLDER}", icon="Error")
                time.sleep(60)
                continue

            current_pdfs = set(
                f for f in os.listdir(BILL_FOLDER)
                if f.lower().endswith(".pdf")
            )

            # Find genuinely new files
            new_files = sorted(current_pdfs - seen)

            # A daily-quota 429 is not a transient per-file failure.  Stop
            # every watcher-launched Zoho call until the next quota window.
            _quota_reason = quota_hold_reason(quota_hold)
            if _quota_reason:
                if current_pdfs - seen and quota_hold_logged != _quota_reason.split(" retry_in=")[0]:
                    quota_hold_logged = _quota_reason.split(" retry_in=")[0]
                    log(f"  [QUOTA_HOLD] {_quota_reason}")
                time.sleep(30)
                continue

            # Add back any retry-queue items still pending
            for f in retry_queue:
                if f in current_pdfs and f not in new_files:
                    new_files.append(f)
            retry_queue = []

            # Drop pending entries for files no longer in the folder
            _gone = [k for k in pending if k not in current_pdfs]
            if _gone:
                for k in _gone:
                    pending.pop(k, None)
                    held_logged.pop(k, None)
                save_pending(pending)
                log(f"  [PENDING_CLEARED] removed_from_folder={_gone}")

            # Hold files whose last outcome was non-terminal and nothing has changed
            if new_files:
                _runnable = []
                for f in new_files:
                    _reason = pending_hold_reason(f, pending.get(f))
                    if _reason is None:
                        _runnable.append(f)
                        held_logged.pop(f, None)
                    elif held_logged.get(f) != _reason.split(" retry_in=")[0]:
                        held_logged[f] = _reason.split(" retry_in=")[0]
                        log(f"  [PENDING_HOLD] file={f} {_reason}")
                new_files = _runnable

            if new_files:
                log(f"  {len(new_files)} new file(s) detected: {new_files}")

                if not is_network_up():
                    log("  No network - queuing for retry.")
                    for f in new_files:
                        notify_error(f, "No internet connection. Will retry when online.")
                    retry_queue.extend(new_files)
                    time.sleep(30)
                    continue

                for filename in new_files:
                    run_id = generate_run_id()
                    log(f"  [WATCHER_DETECTED] run_id={run_id} file={filename}")
                    log(f"\n  Processing: {filename}")

                    # Instant "detected" notification
                    notify_detected(filename)

                    try:
                        status, detail = run_for_file(filename, run_id)
                    except Exception as e:
                        status, detail = "error", f"watcher_execution_error:{type(e).__name__}: {e}"
                        log(f"  [WATCHER_FILE_ERROR] file={filename} detail={detail}")

                    quota_exhausted = is_quota_exhaustion(status, detail)
                    if quota_exhausted:
                        quota_hold = activate_quota_hold(detail)
                        quota_hold_logged = None
                        _quota_reason = quota_hold_reason(quota_hold)
                        detail = f"{detail} | watcher paused: {_quota_reason}"
                        log(f"  [QUOTA_HOLD_ACTIVATED] file={filename} {_quota_reason}")
                    log(f"  Result: {status} | {detail}")

                    if status == "verified":
                        # Parse vendor from detail or invoice_log
                        vendor = ""
                        bill_no = detail
                        try:
                            inv_log_path = r"C:\temp\zoho\invoice_log.json"
                            with open(inv_log_path, encoding="utf-8") as f:
                                inv_log = json.load(f)
                            for entry in reversed(inv_log):
                                if entry.get("file") == filename and entry.get("status") == "verified":
                                    vendor = entry.get("vendor", "")
                                    bill_no = entry.get("bill_number", "")
                                    break
                        except Exception:
                            pass
                        notify_success(filename, bill_no, vendor)

                    elif status == "skipped":
                        notify_skipped(filename, detail)

                    elif status == "needs_review":
                        notify_needs_review(filename, detail)
                        log(f"  [RETRY_PENDING_REVIEW] file={filename} reason={detail}")

                    elif status == "awaiting_approval":
                        notify_needs_review(filename, f"Ready for approval: {detail}")
                        log(f"  [AWAITING_APPROVAL] file={filename} reason={detail}")

                    elif status in ("post_verify_failed", "posted_unverified"):
                        # Posted but not proven correct: never terminal (audit F03).
                        notify_error(filename, f"Posted to Zoho but read-back failed: {detail}")
                        log(f"  [POST_VERIFY_FAILED] file={filename} detail={detail}")

                    elif status in ("failed", "error"):
                        notify_error(filename, detail)
                        # DON'T add to seen - allow retry next cycle if user re-drops
                        log(f"  ERROR on {filename}: {detail}")

                    else:
                        notify_error(filename, f"Unknown status: {detail}")

                    if should_mark_seen(status):
                        seen.add(filename)
                        pending.pop(filename, None)
                        held_logged.pop(filename, None)
                    else:
                        _entry = record_pending(pending, filename, status, detail)
                        _wait = max(0, int(_entry["next_attempt_ts"] - time.time()))
                        log(
                            f"  [PENDING_RECORDED] file={filename} status={status} "
                            f"attempts={_entry['attempts']} next_attempt_in={_wait}s"
                        )
                    save_pending(pending)

                    if quota_exhausted:
                        # Do not process later files from this poll after the
                        # first 429; they remain untouched for the next window.
                        break

                    time.sleep(3)  # Brief pause between files

                save_seen(seen)

            time.sleep(30)  # Poll every 30 seconds

        except Exception as e:
            write_watcher_heartbeat("error", {"last_error": str(e)})
            log(f"Watcher loop error: {e}")
            import traceback
            log(traceback.format_exc())
            time.sleep(10)


def run_self_tests():
    import tempfile

    global LOCK_FILE, LOG_FILE, SEEN_FILE, INVOICE_LOG_FILE, SYSTEM_HEALTH_FILE, QUOTA_HOLD_FILE, SEEN_RECON_APPLY_REMOVALS
    original_lock_file = LOCK_FILE
    original_log_file = LOG_FILE
    original_seen_file = SEEN_FILE
    original_invoice_log_file = INVOICE_LOG_FILE
    original_health_file = SYSTEM_HEALTH_FILE
    original_quota_hold_file = QUOTA_HOLD_FILE
    original_seen_recon_apply_removals = SEEN_RECON_APPLY_REMOVALS
    original_subprocess_run = subprocess.run
    original_subprocess_popen = subprocess.Popen
    original_os_path_exists = os.path.exists
    original_fetch_existing = fetch_existing_bill_numbers_readonly
    original_save_seen = save_seen
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            test_lock = os.path.join(tmpdir, "watcher.lock")
            test_seen = os.path.join(tmpdir, "seen_files.json")
            test_log = os.path.join(tmpdir, "watcher.log")
            test_invoice_log = os.path.join(tmpdir, "invoice_log.json")
            LOCK_FILE = test_lock
            SEEN_FILE = test_seen
            LOG_FILE = test_log
            INVOICE_LOG_FILE = test_invoice_log

            with open(test_lock, "w", encoding="utf-8") as f:
                f.write(str(os.getpid()))
            assert cleanup_watcher_lock() is True
            assert not os.path.exists(test_lock)

            with open(test_lock, "w", encoding="utf-8") as f:
                f.write(str(os.getpid() + 100000))
            assert cleanup_watcher_lock() is False
            assert os.path.exists(test_lock)

            os.remove(test_lock)
            assert cleanup_watcher_lock() is False

            with open(test_lock, "w", encoding="utf-8") as f:
                f.write("not-a-pid")
            assert cleanup_watcher_lock() is False
            assert os.path.exists(test_lock)

            test_json = os.path.join(tmpdir, "atomic.json")
            atomic_json_write(test_json, {"ok": True, "items": [1, 2, 3]})
            with open(test_json, encoding="utf-8") as f:
                assert json.load(f) == {"ok": True, "items": [1, 2, 3]}

            missing_json = os.path.join(tmpdir, "missing.json")
            assert safe_json_read(missing_json, {"default": True}) == {"default": True}

            QUOTA_HOLD_FILE = os.path.join(tmpdir, "zoho_quota_hold.json")
            quota_now = datetime.datetime(2026, 8, 27, 13, 48, tzinfo=IST).timestamp()
            assert is_quota_exhaustion("error", "urllib.error.HTTPError: HTTP Error 429:") is True
            assert is_quota_exhaustion("needs_review", "HTTP Error 429") is False
            quota_hold = activate_quota_hold("HTTP Error 429", now=quota_now)
            assert quota_hold_reason(quota_hold, now=quota_now).startswith(
                "zoho_quota_exhausted resume_at=2026-08-28 00:05 IST"
            )
            assert quota_hold_reason(load_quota_hold(), now=quota_now) is not None
            assert quota_hold_reason(quota_hold, now=quota_hold["resume_at_epoch"]) is None

            assert should_mark_seen("verified") is True
            assert should_mark_seen("created") is False
            assert should_mark_seen("post_verify_failed") is False
            assert should_mark_seen("posted_unverified") is False
            assert should_mark_seen("awaiting_approval") is False
            assert should_mark_seen("skipped") is True
            assert should_mark_seen("needs_review") is False
            assert should_mark_seen("failed") is False
            assert should_mark_seen("error") is False
            assert _notification_call_succeeded(False, "Test notification", "show") is False
            with open(test_log, encoding="utf-8") as f:
                assert "[NOTIFY_FAILED] stage=show title=Test notification" in f.read()

            notification_calls = []

            class FakeNotificationResult:
                returncode = 0
                stdout = ""
                stderr = ""

            def fake_notification_run(args, **kwargs):
                notification_calls.append((args, kwargs))
                return FakeNotificationResult()

            subprocess.run = fake_notification_run
            assert _notify_with_powershell("Test notification", "Body", "Information") is True
            subprocess.run = original_subprocess_run
            assert notification_calls[0][0][:6] == [
                "powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-EncodedCommand"
            ]
            with open(test_log, encoding="utf-8") as f:
                assert "[NOTIFY_FALLBACK_DISPATCHED] title=Test notification" in f.read()

            corrupt_json = os.path.join(tmpdir, "corrupt.json")
            with open(corrupt_json, "w", encoding="utf-8") as f:
                f.write("{not valid json")
            assert safe_json_read(corrupt_json, {"default": True}) == {"default": True}

            seen = {"present.pdf"}
            inv_log = [{"file": "present.pdf", "status": "created", "bill_number": "BILL-1"}]
            updated, removed, reason = _reconcile_seen_files_core(seen, inv_log, {"bill-1"})
            assert updated == {"present.pdf"}
            assert removed == []
            assert reason is None

            seen = {"missing.pdf"}
            inv_log = [{"file": "missing.pdf", "status": "created", "bill_number": "BILL-2"}]
            updated, removed, reason = _reconcile_seen_files_core(seen, inv_log, {"BILL-1"})
            assert updated == set()
            assert removed == [("missing.pdf", "BILL-2")]
            assert reason is None

            seen = {"unlogged.pdf"}
            inv_log = [{"file": "other.pdf", "status": "created", "bill_number": "BILL-3"}]
            updated, removed, reason = _reconcile_seen_files_core(seen, inv_log, set())
            assert updated == {"unlogged.pdf"}
            assert removed == []
            assert reason is None

            for status in ("needs_review", "failed", "skipped_duplicate", "unknown"):
                seen = {f"{status}.pdf"}
                inv_log = [{"file": f"{status}.pdf", "status": status, "bill_number": "BILL-X"}]
                updated, removed, reason = _reconcile_seen_files_core(seen, inv_log, set())
                assert updated == seen
                assert removed == []
                assert reason is None

            seen = {"corrupt-safe.pdf"}
            updated, removed, reason = _reconcile_seen_files_core(seen, None, set())
            assert updated == seen
            assert removed == []
            assert reason == "invoice_log_unavailable"

            seen = {"zoho-unavailable.pdf"}
            inv_log = [{"file": "zoho-unavailable.pdf", "status": "created", "bill_number": "BILL-4"}]
            updated, removed, reason = _reconcile_seen_files_core(seen, inv_log, None)
            assert updated == seen
            assert removed == []
            assert reason == "existing_bills_unavailable"

            seen = {"ambiguous.pdf"}
            inv_log = [
                {"file": "ambiguous.pdf", "status": "created", "bill_number": "BILL-OLD"},
                {"file": "ambiguous.pdf", "status": "needs_review", "bill_number": ""},
            ]
            updated, removed, reason = _reconcile_seen_files_core(seen, inv_log, set())
            assert updated == seen
            assert removed == []
            assert reason is None

            seen = {"latest-created.pdf"}
            inv_log = [
                {"file": "latest-created.pdf", "status": "failed", "bill_number": ""},
                {"file": "latest-created.pdf", "status": "created", "bill_number": "BILL-NEW"},
            ]
            updated, removed, reason = _reconcile_seen_files_core(seen, inv_log, set())
            assert updated == set()
            assert removed == [("latest-created.pdf", "BILL-NEW")]
            assert reason is None

            invoice_log_path = os.path.join(tmpdir, "invoice_log.json")
            atomic_json_write(invoice_log_path, [
                {"file": "save-remove.pdf", "status": "created", "bill_number": "BILL-5"},
                {"file": "save-keep.pdf", "status": "created", "bill_number": "BILL-6"},
            ])

            save_seen_calls = []

            def fake_save_seen(seen_arg):
                save_seen_calls.append(set(seen_arg))
                raise AssertionError("save_seen must not be called in report-only mode")

            globals()["save_seen"] = fake_save_seen
            SEEN_RECON_APPLY_REMOVALS = False
            result = reconcile_seen_files_with_invoice_log(
                {"save-remove.pdf", "save-keep.pdf"},
                existing_bills={"BILL-6"},
                invoice_log_path=invoice_log_path,
            )
            assert result == {"save-remove.pdf", "save-keep.pdf"}
            assert save_seen_calls == []
            assert not os.path.exists(test_seen)
            with open(test_log, encoding="utf-8") as f:
                report_only_log = f.read()
            assert "SEEN_RECON_CANDIDATE file=save-remove.pdf bill_number=BILL-5" in report_only_log
            assert "SEEN_RECON_REPORT_ONLY checked=2 candidates=1 applied=0" in report_only_log

            globals()["save_seen"] = original_save_seen
            SEEN_RECON_APPLY_REMOVALS = True
            result = reconcile_seen_files_with_invoice_log(
                {"save-remove.pdf", "save-keep.pdf"},
                existing_bills={"BILL-6"},
                invoice_log_path=invoice_log_path,
            )
            assert result == {"save-keep.pdf"}
            with open(test_seen, encoding="utf-8") as f:
                assert json.load(f) == ["save-keep.pdf"]
            SEEN_RECON_APPLY_REMOVALS = False

            result = reconcile_seen_files_with_invoice_log(
                {"no-live.pdf"},
                existing_bills=None,
                invoice_log_path=invoice_log_path,
            )
            assert result == {"no-live.pdf"}

            missing_invoice_log = os.path.join(tmpdir, "missing_invoice_log.json")
            result = reconcile_seen_files_with_invoice_log(
                {"missing-log.pdf"},
                existing_bills=set(),
                invoice_log_path=missing_invoice_log,
            )
            assert result == {"missing-log.pdf"}

            corrupt_invoice_log = os.path.join(tmpdir, "corrupt_invoice_log.json")
            with open(corrupt_invoice_log, "w", encoding="utf-8") as f:
                f.write("{bad json")
            result = reconcile_seen_files_with_invoice_log(
                {"corrupt-log.pdf"},
                existing_bills=set(),
                invoice_log_path=corrupt_invoice_log,
            )
            assert result == {"corrupt-log.pdf"}

            class FakeCompleted:
                def __init__(self, stdout, returncode=0):
                    self.stdout = stdout
                    self.stderr = "stderr is not json"
                    self.returncode = returncode

            def set_fake_run(fake_run):
                subprocess.run = fake_run

            calls = []
            full_list_payload = {
                "ok": True,
                "count": 3,
                "bill_numbers": [" inv-1 ", "Inv-2", "", "INV-1"],
            }
            set_fake_run(lambda args, **kwargs: calls.append((args, kwargs)) or FakeCompleted(json.dumps(full_list_payload)))
            existing = fetch_existing_bill_numbers_readonly()
            assert existing == {"INV-1", "INV-2"}
            assert calls == [(
                [PYTHON_EXE, MAIN_SCRIPT, "--list-existing-bills-json"],
                {
                    "capture_output": True,
                    "text": True,
                    "timeout": 120,
                    "creationflags": _hidden_subprocess_creationflags(),
                },
            )]
            with open(test_log, encoding="utf-8") as f:
                helper_log = f.read()
            assert "count=2" in helper_log
            assert "inv-1" not in helper_log.lower()
            assert "Inv-2" not in helper_log

            set_fake_run(lambda args, **kwargs: FakeCompleted("{not json"))
            assert fetch_existing_bill_numbers_readonly() is None

            set_fake_run(lambda args, **kwargs: FakeCompleted(json.dumps({"ok": False, "error": "Zoho unavailable"})))
            assert fetch_existing_bill_numbers_readonly() is None

            set_fake_run(lambda args, **kwargs: FakeCompleted(json.dumps({"ok": True, "bill_numbers": "INV-1"})))
            assert fetch_existing_bill_numbers_readonly() is None

            set_fake_run(lambda args, **kwargs: (_ for _ in ()).throw(subprocess.TimeoutExpired(args, 120)))
            assert fetch_existing_bill_numbers_readonly() is None

            set_fake_run(lambda args, **kwargs: FakeCompleted("{}", returncode=1))
            assert fetch_existing_bill_numbers_readonly() is None

            subprocess.run = original_subprocess_run

            atomic_json_write(test_invoice_log, [
                {"file": "safe-missing.pdf", "status": "created", "bill_number": "MISS-1"},
                {"file": "safe-present.pdf", "status": "created", "bill_number": "KEEP-1"},
                {"file": "ambiguous-startup.pdf", "status": "created", "bill_number": "OLD-1"},
                {"file": "ambiguous-startup.pdf", "status": "needs_review", "bill_number": ""},
                {"file": "failed-startup.pdf", "status": "failed", "bill_number": "FAIL-1"},
                {"file": "duplicate-startup.pdf", "status": "skipped_duplicate", "bill_number": "DUP-1"},
            ])

            globals()["fetch_existing_bill_numbers_readonly"] = lambda: {"KEEP-1"}
            globals()["save_seen"] = fake_save_seen
            SEEN_RECON_APPLY_REMOVALS = False
            result = startup_reconcile_seen({
                "safe-missing.pdf",
                "safe-present.pdf",
                "ambiguous-startup.pdf",
                "failed-startup.pdf",
                "duplicate-startup.pdf",
            })
            assert result == {
                "safe-missing.pdf",
                "safe-present.pdf",
                "ambiguous-startup.pdf",
                "failed-startup.pdf",
                "duplicate-startup.pdf",
            }
            assert save_seen_calls == []
            with open(test_log, encoding="utf-8") as f:
                startup_report_only_log = f.read()
            assert "SEEN_RECON_CANDIDATE file=safe-missing.pdf bill_number=MISS-1" in startup_report_only_log
            assert "SEEN_RECON_REPORT_ONLY checked=5 candidates=1 applied=0" in startup_report_only_log

            globals()["save_seen"] = original_save_seen
            SEEN_RECON_APPLY_REMOVALS = True
            result = startup_reconcile_seen({
                "safe-missing.pdf",
                "safe-present.pdf",
                "ambiguous-startup.pdf",
                "failed-startup.pdf",
                "duplicate-startup.pdf",
            })
            assert result == {
                "safe-present.pdf",
                "ambiguous-startup.pdf",
                "failed-startup.pdf",
                "duplicate-startup.pdf",
            }
            with open(test_seen, encoding="utf-8") as f:
                assert json.load(f) == [
                    "ambiguous-startup.pdf",
                    "duplicate-startup.pdf",
                    "failed-startup.pdf",
                    "safe-present.pdf",
                ]
            SEEN_RECON_APPLY_REMOVALS = False

            os.remove(test_seen)
            globals()["fetch_existing_bill_numbers_readonly"] = lambda: None
            result = startup_reconcile_seen({"none-safe.pdf"})
            assert result == {"none-safe.pdf"}
            assert not os.path.exists(test_seen)
            globals()["fetch_existing_bill_numbers_readonly"] = original_fetch_existing

            health_json = os.path.join(tmpdir, "system_health.json")
            existing_health = {
                "last_run": {"status": "ok"},
                "rolling_5_avg": 12.5,
                "alert_flags": ["sample"],
            }
            atomic_json_write(health_json, existing_health)

            SYSTEM_HEALTH_FILE = health_json
            write_watcher_heartbeat("running")
            updated_health = safe_json_read(health_json, {})

            assert updated_health["last_run"] == existing_health["last_run"]
            assert updated_health["rolling_5_avg"] == existing_health["rolling_5_avg"]
            assert updated_health["alert_flags"] == existing_health["alert_flags"]
            assert "watcher" in updated_health

            watcher = updated_health["watcher"]
            assert watcher["pid"] == os.getpid()
            assert watcher["status"] == "running"
            assert isinstance(watcher["updated_at"], str) and watcher["updated_at"]
            assert isinstance(watcher["heartbeat_epoch"], (int, float))

            hidden_flags = _hidden_subprocess_creationflags()
            assert hidden_flags == (getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0)

            preflight_calls = []

            class FakeReturnCode:
                returncode = 0

            def fake_preflight_run(args, **kwargs):
                preflight_calls.append((args, kwargs))
                return FakeReturnCode()

            test_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test", "run_tests.py")
            subprocess.run = fake_preflight_run
            os.path.exists = lambda path: True if path == test_script else original_os_path_exists(path)
    # enforce_test_gate disabled: prevents visible startup subprocess flash
            assert preflight_calls == [(
                [PYTHON_EXE, test_script],
                {"capture_output": False, "creationflags": hidden_flags},
            )]
            subprocess.run = original_subprocess_run
            os.path.exists = original_os_path_exists

            popen_calls = []

            class FakeProcess:
                returncode = 0

                def communicate(self, timeout=None):
                    return "", ""

                def kill(self):
                    raise AssertionError("run_for_file test must not hit timeout")

            def fake_popen(args, **kwargs):
                popen_calls.append((args, kwargs))
                return FakeProcess()

            subprocess.Popen = fake_popen
            status, detail = run_for_file("sample.pdf", "test-run-id")
            expected_filepath = os.path.join(BILL_FOLDER, "sample.pdf")
            assert (status, detail) == ("unknown", "Could not determine result - check watcher.log")
            assert popen_calls == [(
                [PYTHON_EXE, MAIN_SCRIPT, "--file", expected_filepath, "--run-id", "test-run-id"],
                {
                    "stdout": subprocess.PIPE,
                    "stderr": subprocess.PIPE,
                    "text": True,
                    "creationflags": hidden_flags,
                },
            )]
            subprocess.Popen = original_subprocess_popen

        print("watcher.py self-test passed")
        return 0
    finally:
        LOCK_FILE = original_lock_file
        LOG_FILE = original_log_file
        SEEN_FILE = original_seen_file
        INVOICE_LOG_FILE = original_invoice_log_file
        SYSTEM_HEALTH_FILE = original_health_file
        QUOTA_HOLD_FILE = original_quota_hold_file
        SEEN_RECON_APPLY_REMOVALS = original_seen_recon_apply_removals
        subprocess.run = original_subprocess_run
        subprocess.Popen = original_subprocess_popen
        os.path.exists = original_os_path_exists
        globals()["fetch_existing_bill_numbers_readonly"] = original_fetch_existing
        globals()["save_seen"] = original_save_seen


if __name__ == "__main__":
    if "--test" in sys.argv[1:]:
        sys.exit(run_self_tests())
    # enforce_test_gate() disabled: was spawning visible startup subprocesses
    try:
        run_watcher()
    finally:
        cleanup_watcher_lock()
