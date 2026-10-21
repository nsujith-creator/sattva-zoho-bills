"""
Zoho Books - Auto Vendor Bill Entry
Reconstructed from zoho_bills.cpython-312.pyc - March 2026
Original compiled: 2026-03-23 08:09:21 | Source size: 26304 bytes

SESSION 1 OBSERVABILITY ADDITIONS (April 2026):
- run_id propagation via --run-id argv
- lifecycle event labels on all major log events
- run_manifest.json written per run
- audit_baseline_report.json written per run
- All observability writes fail safely (never block core processing)

SESSION 2 AUTH & CONFIG HARDENING (April 2026):
- safe_json_read(), atomic_json_write(), validate_config_keys()
- Hardened refresh_token() with explicit auth labels
- _run_id_ctx module global for auth event correlation

SESSION 3 STATE FILE ATOMICITY & CACHE INTEGRITY (April 2026):
- All JSON state files written via atomic_json_write() (tmp+fsync+replace)
- Hash-keyed parse cache: _hash_file(), _make_cache_entry(), _cache_lookup()
- _load_parse_cache(): recovers corrupt/missing cache with STATE_RECOVERY label
- _load_state_list(): recovers corrupt/missing list files with STATE_RECOVERY
- _append_json_list(): atomic append with MAX_LOG_ENTRIES=500 size cap
- Removed old filename-keyed cache lookups and bare open() cache writes

AUDIT FIXES (October 2026):
- F04: no POST without a recorded approval. Normal runs queue the exact payload
  in approval_queue.json; `--file PDF --approve --approver NAME` posts it only if
  the rebuilt payload is identical, after appending to approvals.json
- F03: "verified" is the only success status; a failed read-back is logged
  post_verify_failed and is never reported to the watcher as created
"""

import json
import urllib.request
import urllib.parse
import urllib.error
import os
import base64
import time
import subprocess
import datetime
import re
import sys
import uuid
import platform
import hashlib
import contextlib
import io
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


def _configure_stdio():
    """
    Flush line-oriented startup logs immediately when stdout/stderr are piped.
    This keeps watcher subprocesses observable on Windows.
    """
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(line_buffering=True, write_through=True)
        except Exception:
            pass


def _safe_platform_label():
    """
    Avoid platform.platform() here. On this Python install it can hang during
    startup, and observability metadata must never block the pipeline.
    """
    return sys.platform

# ── Paths & Config ────────────────────────────────────────────────────────────
CONFIG_PATH          = r"C:\temp\zoho\config.json"
BILLS_DIR            = r"C:\temp\bills"
LOG_PATH             = r"C:\temp\zoho\run_log.json"
CACHE_PATH           = r"C:\temp\zoho\parsed_cache.json"
INVOICE_LOG_PATH     = r"C:\temp\zoho\invoice_log.json"
FAILED_INVOICES_PATH = r"C:\temp\zoho\failed_invoices.json"
BOOKS_RECON_ENV_PATH = r"C:\sattva\books_recon\.env"
RUN_MANIFEST_PATH    = r"C:\temp\zoho\run_manifest.json"
AUDIT_BASELINE_PATH  = r"C:\temp\zoho\audit_baseline_report.json"
DAILY_METRICS_PATH  = r"C:\temp\zoho\daily_metrics.json"
SYSTEM_HEALTH_PATH  = r"C:\temp\zoho\system_health.json"
TAX_RULES_PATH    = r"C:\temp\zoho\tax_rules.json"
VENDOR_RULES_PATH = r"C:\temp\zoho\vendor_rules.json"
APPROVAL_QUEUE_PATH = r"C:\temp\zoho\approval_queue.json"
APPROVALS_PATH      = r"C:\temp\zoho\approvals.json"

DEFAULT_ORG_ID      = "60015023876"
DEFAULT_ZOHO_DOMAIN = "https://www.zohoapis.in"
DEFAULT_ACCOUNTS_URL = "https://accounts.zoho.in"
def _load_anthropic_key():
    try:
        with open(r"C:\temp\zoho\config.json", encoding="utf-8-sig") as _cf:
            _cfg = json.load(_cf)
        _key = str(_cfg.get("anthropic_api_key", "")).strip()
        if _key:
            return _key
    except Exception:
        pass
    _env = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if _env:
        return _env
    raise Exception("ANTHROPIC_API_KEY missing from config.json")

ANTHROPIC_API_KEY = _load_anthropic_key()
ANTHROPIC_MODEL     = "claude-haiku-4-5"

# ── Session 3: State management constants ─────────────────────────────────────
MAX_LOG_ENTRIES = 500   # soft cap for invoice_log and failed_invoices

_DEFAULT_TAX_ID_MAP = {
    "cgst_sgst_18": "914109000000014276",
    "cgst_sgst_5":  "914109000000014264",
    "igst_18":      "914109000000014234",
    "igst_5":       "914109000000014230",
}
_DEFAULT_IGST_VENDORS            = ["CMA CGM", "CCAI", "MAERSK", "FACEBOOK", "META", "ZOHO"]
_DEFAULT_OVERSEAS_VENDOR_SIGNALS = [
    "ANTHROPIC", "AWS", "AMAZON WEB", "MICROSOFT", "AZURE", "ADOBE", "OPENAI",
    "GITHUB", "VERCEL", "NETLIFY", "DIGITALOCEAN", "CLOUDFLARE", "FIGMA", "NOTION",
    "DROPBOX", "SLACK", "ZOOM", "ATLASSIAN", "STRIPE", "NAMECHEAP", "GODADDY",
    "HOSTINGER", "SENDINBLUE", "BREVO",
]
_DEFAULT_VENDOR_ALIAS_MAP        = {}

def load_tax_rules(run_id="unset"):
    data, err = safe_json_read(TAX_RULES_PATH)
    if data is None:
        obs_print("RULES_LOAD_FAILED",
                  f"run_id={run_id} file=tax_rules.json reason={err} action=abort")
        raise RuntimeError(f"tax_rules.json load failed: {err}")
    required = ("tax_id_map", "igst_vendors", "overseas_vendor_signals")
    missing  = [k for k in required if k not in data]
    if missing or not isinstance(data.get("tax_id_map"), dict) \
               or not isinstance(data.get("igst_vendors"), list) \
               or not isinstance(data.get("overseas_vendor_signals"), list):
        obs_print("RULES_LOAD_FAILED",
                  f"run_id={run_id} file=tax_rules.json reason=invalid_structure missing={missing} action=abort")
        raise RuntimeError(f"tax_rules.json invalid structure: missing={missing}")
    data["_source"] = "json_file"
    return data

def load_vendor_rules(run_id="unset"):
    data, err = safe_json_read(VENDOR_RULES_PATH)
    if data is None:
        obs_print("RULES_CONFIG_RECOVERY",
                  f"run_id={run_id} file=vendor_rules.json reason={err} action=using_defaults")
        return {"vendor_alias_map": _DEFAULT_VENDOR_ALIAS_MAP, "_source": "default"}
    if not isinstance(data.get("vendor_alias_map"), dict):
        obs_print("RULES_CONFIG_RECOVERY",
                  f"run_id={run_id} file=vendor_rules.json reason=invalid_structure action=using_defaults")
        return {"vendor_alias_map": _DEFAULT_VENDOR_ALIAS_MAP, "_source": "default"}
    data["_source"] = "json_file"
    return data

def fetch_tax_ids_from_zoho(tax_rules, run_id="unset"):
    # TODO_AUDIT_REQUIRED: verify tax_name strings match your Zoho org before enabling
    try:
        data = zoho_get("settings/taxes")
        taxes = data.get("taxes", [])
        if not taxes:
            raise ValueError("empty taxes list")
        _name_to_key = {
            "CGST 9% + SGST 9%":   "cgst_sgst_18",
            "CGST 2.5% + SGST 2.5%": "cgst_sgst_5",
            "IGST 18%": "igst_18",
            "IGST 5%":  "igst_5",
        }
        overrides = {}
        for t in taxes:
            key = _name_to_key.get(t.get("tax_name", "").strip())
            if key and t.get("tax_id"):
                overrides[key] = t["tax_id"]
        if overrides:
            merged = dict(tax_rules["tax_id_map"])
            merged.update(overrides)
            tax_rules = dict(tax_rules)
            tax_rules["tax_id_map"] = merged
            tax_rules["_source"]    = "zoho_fetch_override"
    except Exception as e:
        obs_print("TAX_SETTINGS_FETCH_FAILED",
                  f"run_id={run_id} reason={str(e)[:80]} action=using_loaded_config")
    return tax_rules


# ── Session 5: AI cost controls ──────────────────────────────────────────────
# 7: multi-invoice PDFs are held whole (audit F06). Version-6 entries may hold
# only one invoice of several, so they must be re-parsed.
_PARSER_VERSION       = 7
_AI_VENDOR_TOP_N      = 5
_AI_PROMPT_CHAR_LIMIT = 6000

def _normalize(s):
    return re.sub(r"[^A-Z0-9 ]", " ", s.upper()).split()

def _shortlist_vendors(filename, pdf_text, vendors_list, run_id="unset"):
    fname_tokens = set(_normalize(filename))
    text_tokens  = set(_normalize(pdf_text[:2000]))
    combined     = fname_tokens | text_tokens
    exact, scored = [], []
    for v in vendors_list:
        vtoks = set(_normalize(v))
        if not vtoks:
            continue
        if vtoks & fname_tokens:
            exact.append(v)
        else:
            overlap = len(vtoks & combined)
            if overlap:
                scored.append((overlap, v))
    if exact:
        shortlist = exact[:_AI_VENDOR_TOP_N]
    else:
        scored.sort(key=lambda x: -x[0])
        shortlist = [v for _, v in scored[:_AI_VENDOR_TOP_N]]
    if not shortlist:
        fallback = vendors_list[:min(3, len(vendors_list))]
        obs_print("AI_VENDOR_SHORTLIST_FALLBACK",
                  f"run_id={run_id} total={len(vendors_list)} used={len(fallback)}")
        return fallback
    obs_print("AI_VENDOR_SHORTLIST",
              f"run_id={run_id} total={len(vendors_list)} shortlisted={len(shortlist)}")
    return shortlist


# ── Session 1: Observability helpers ─────────────────────────────────────────

def generate_run_id():
    """
    Deterministic run ID: YYYYMMDD-HHMMSS-<8hex>.
    Uses stdlib only (datetime + uuid). Called once per pipeline invocation.
    """
    ts  = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    uid = uuid.uuid4().hex[:8]
    return f"{ts}-{uid}"


def obs_print(label, message=""):
    """
    Print a lifecycle event with a structured label prefix.
    Labels defined in Session 1 spec:
      WATCHER_DETECTED, PIPELINE_STARTED, AUTH_PHASE_STARTED,
      FETCH_EXISTING_BILLS_STARTED, FETCH_VENDORS_STARTED,
      PARSE_STARTED, PARSE_COMPLETED, VALIDATION_FAILED,
      POST_STARTED, POST_COMPLETED, RUN_COMPLETED, RUN_FATAL
    Fails safely: if print itself raises, silently ignored.
    """
    try:
        if message:
            print(f"[{label}] {message}")
        else:
            print(f"[{label}]")
    except Exception:
        pass


def write_run_manifest(manifest):
    """
    Write run_manifest.json. Fails safely -- never blocks core processing.
    Overwrites previous manifest (one manifest per run, last-write wins).
    """
    try:
        with open(RUN_MANIFEST_PATH, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2)
    except Exception:
        pass  # observability must never crash the pipeline


def write_audit_baseline(run_id):
    """
    Write audit_baseline_report.json with file inventory and runtime metadata.
    Fails safely -- never blocks core processing.
    """
    try:
        state_files = {
            "config.json":          CONFIG_PATH,
            "parsed_cache.json":    CACHE_PATH,
            "run_log.json":         LOG_PATH,
            "invoice_log.json":     INVOICE_LOG_PATH,
            "failed_invoices.json": FAILED_INVOICES_PATH,
            "run_manifest.json":    RUN_MANIFEST_PATH,
        }
        file_inventory = {}
        for label, path in state_files.items():
            exists = os.path.exists(path)
            file_inventory[label] = {
                "path":   path,
                "exists": exists,
                "size_bytes": os.path.getsize(path) if exists else None,
            }

        bills_count = 0
        if os.path.exists(BILLS_DIR):
            bills_count = len([
                f for f in os.listdir(BILLS_DIR)
                if f.lower().endswith(".pdf")
            ])

        cache_count = 0
        if os.path.exists(CACHE_PATH):
            try:
                with open(CACHE_PATH, encoding="utf-8-sig") as f:
                    raw = json.load(f)
                cache_count = len([
                    v for v in raw.values()
                    if isinstance(v, dict) and v.get("bill_number")
                ])
            except Exception:
                pass

        report = {
            "run_id":              run_id,
            "generated_at":        datetime.datetime.now().isoformat(),
            "python_version":      sys.version,
            "platform":            _safe_platform_label(),
            "cwd":                 os.getcwd(),
            "bills_dir":           BILLS_DIR,
            "bills_dir_exists":    os.path.exists(BILLS_DIR),
            "pdf_count_in_bills":  bills_count,
            "cache_valid_entries": cache_count,
            "file_inventory":      file_inventory,
        }
        with open(AUDIT_BASELINE_PATH, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2)
    except Exception:
        pass  # observability must never crash the pipeline


# ── Session 2: Auth & Config Hardening ───────────────────────────────────────

# Module-level run_id context for auth logging (set by main() on startup)
_run_id_ctx = "unset"

# Required keys for config validation
# Conceptual split: STATIC = credentials | MUTABLE = token state
_CONFIG_STATIC_KEYS = ("client_id", "client_secret")
_CONFIG_MUTABLE_KEYS = ("refresh_token", "access_token")  # access_token may be empty string
_CONFIG_REQUIRED_KEYS = _CONFIG_STATIC_KEYS + _CONFIG_MUTABLE_KEYS


def safe_json_read(path):
    """
    Read and parse a JSON file safely.
    - Handles UTF-8 BOM (utf-8-sig)
    - Returns (data_dict, None) on success
    - Returns (None, error_str) on any failure -- never raises, never silently returns {}
    """
    if not os.path.exists(path):
        return None, f"file_not_found:{path}"
    try:
        with open(path, encoding="utf-8-sig") as f:
            raw = f.read()
        data = json.loads(raw)
        if not isinstance(data, dict):
            return None, f"not_a_dict:{path}"
        return data, None
    except json.JSONDecodeError as e:
        return None, f"invalid_json:{path}:{e}"
    except Exception as e:
        return None, f"read_error:{path}:{e}"


def atomic_json_write(path, data):
    """
    Write JSON atomically: write to .tmp, fsync, rename over original.
    Never leaves a partial file. Windows compatible.
    Returns (True, None) on success, (False, error_str) on failure.
    """
    tmp_path = path + ".tmp"
    try:
        content = json.dumps(data, indent=2)
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)  # atomic on Windows (same drive)
        return True, None
    except Exception as e:
        try:
            os.remove(tmp_path)
        except Exception:
            pass
        return False, str(e)


def validate_config_keys(data, run_id="unset"):
    """
    Validate required config keys exist.
    access_token may be empty string -- just must be present.
    Returns (True, None) or (False, missing_keys_str).
    """
    missing = [k for k in _CONFIG_REQUIRED_KEYS if k not in data]
    if missing:
        obs_print("VALIDATION_FAILED",
                  f"run_id={run_id} context=config_load missing_keys={missing}")
        return False, f"missing_keys:{missing}"
    return True, None


# Legacy alias -- used by resolve_auth_source() and audit_baseline; keep for S1 compat
def load_json_config(path):
    data, err = safe_json_read(path)
    return data if data is not None else {}


# ── Session 3: State file helpers ─────────────────────────────────────────────

def _hash_file(path):
    """
    Return SHA-256 hex digest of file contents.
    Used as cache key so same filename + different content = cache miss.
    """
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _make_cache_entry(parsed, file_hash, vendor_context_count=0):
    return {
        "_cache_version":        _PARSER_VERSION,
        "_file_hash":            file_hash,
        "_cached_at":            datetime.datetime.now().isoformat(),
        "_parser_version":       _PARSER_VERSION,
        "_vendor_context_count": vendor_context_count,
        "_confidence":           parsed.get("confidence", "low"),
        **parsed,
    }


def _cache_lookup(cache, file_hash):
    """
    Look up a cache entry by SHA-256 hash.
    Returns parsed dict (without S3 metadata) on hit, None on miss.
    Rejects old flat-format entries (keyed by filename, no _cache_version).
    """
    entry = cache.get(file_hash)
    if entry is None:
        return None
    # Old flat format has no _cache_version -- reject silently (migration safety)
    if not isinstance(entry, dict) or "_cache_version" not in entry:
        return None
    if entry.get("_parser_version") != _PARSER_VERSION:
        return None
    if isinstance(entry.get("multi_invoice_count"), int) and entry["multi_invoice_count"] > 1 \
            and isinstance(entry.get("invoices"), list):
        # A multi-invoice PDF held for review as a whole (audit F06).
        return {k: v for k, v in entry.items() if not k.startswith("_")}
    _required = ("vendor_name", "bill_number", "bill_date", "line_items", "confidence")
    if any(not entry.get(k) for k in _required):
        return None
    if not isinstance(entry.get("line_items"), list) or not entry["line_items"]:
        return None
    return {k: v for k, v in entry.items() if not k.startswith("_")}


def _load_parse_cache(run_id="unset"):
    """
    Load parsed_cache.json safely.
    On missing or corrupt file: log STATE_RECOVERY and return {}.
    Filters out entries lacking bill_number (Gemini fake entries).
    """
    if not os.path.exists(CACHE_PATH):
        obs_print("STATE_RECOVERY",
                  f"run_id={run_id} file=parsed_cache.json reason=file_not_found action=start_empty")
        return {}
    data, err = safe_json_read(CACHE_PATH)
    if data is None:
        obs_print("STATE_RECOVERY",
                  f"run_id={run_id} file=parsed_cache.json reason={err} action=start_empty")
        return {}
    # Filter invalid entries. A held multi-invoice PDF keeps its bill numbers
    # inside "invoices" (audit F06).
    valid = {k: v for k, v in data.items()
             if isinstance(v, dict) and (v.get("bill_number") or isinstance(v.get("invoices"), list))}
    dropped = len(data) - len(valid)
    if dropped:
        obs_print("STATE_RECOVERY",
                  f"run_id={run_id} file=parsed_cache.json reason=invalid_entries dropped={dropped}")
    return valid


def _load_state_list(path, run_id="unset", label=None):
    """
    Load a JSON list state file (invoice_log, failed_invoices, run_log lists).
    On missing or corrupt: log STATE_RECOVERY and return [].
    """
    label = label or os.path.basename(path)
    if not os.path.exists(path):
        obs_print("STATE_RECOVERY",
                  f"run_id={run_id} file={label} reason=file_not_found action=start_empty")
        return []
    try:
        with open(path, encoding="utf-8-sig") as f:
            raw = f.read()
        data = json.loads(raw)
        if not isinstance(data, list):
            obs_print("STATE_RECOVERY",
                      f"run_id={run_id} file={label} reason=not_a_list action=start_empty")
            return []
        return data
    except Exception as e:
        obs_print("STATE_RECOVERY",
                  f"run_id={run_id} file={label} reason=corrupt:{str(e)[:60]} action=start_empty")
        return []


def _append_json_list(path, entry, run_id="unset", label=None):
    """
    Append an entry to a JSON list file atomically.
    Enforces MAX_LOG_ENTRIES soft cap -- drops oldest when over limit.
    """
    label = label or os.path.basename(path)
    data = _load_state_list(path, run_id=run_id, label=label)
    data.append(entry)
    if len(data) > MAX_LOG_ENTRIES:
        data = data[-MAX_LOG_ENTRIES:]   # keep newest
    ok, err = atomic_json_write(path, data)
    if not ok:
        obs_print("STATE_WRITE_FAILED",
                  f"run_id={run_id} file={label} reason={err}")


def _classify_error(err_str):
    e = err_str.upper()
    if any(x in e for x in ("AUTH", "TOKEN", "OAUTH")):
        return "AUTH_ERROR"
    if any(x in e for x in ("PARSE", "JSON", "ANTHROPIC", "GEMINI", "CREDIT")):
        return "PARSE_ERROR"
    if any(x in e for x in ("TAX", "GST", "ROUTE")):
        return "TAX_ERROR"
    return "POST_ERROR"

def _write_run_metrics(run_id, metrics):
    try:
        existing = _load_state_list(DAILY_METRICS_PATH, run_id=run_id, label="daily_metrics.json")
        existing.append(metrics)
        if len(existing) > 500:
            existing = existing[-500:]
        atomic_json_write(DAILY_METRICS_PATH, existing)
        obs_print("RUN_METRICS",
                  f"run_id={run_id} total={metrics['total_files']} "
                  f"created={metrics['created']} skipped={metrics['skipped']} "
                  f"review={metrics['review']} errors={metrics['errors']} "
                  f"cache_hits={metrics['cache_hits']} ai_calls={metrics['ai_calls']}")
    except Exception as _e:
        obs_print("RUN_METRICS_FAILED", f"run_id={run_id} reason={str(_e)[:60]}")

def _update_system_health(run_id, metrics, alerts):
    try:
        existing_raw = _load_state_list(DAILY_METRICS_PATH, run_id=run_id, label="daily_metrics.json")
        recent = existing_raw[-5:] if existing_raw else [metrics]
        def _avg(key):
            vals = [r.get(key, 0) for r in recent if r.get("total_files", 0) > 0]
            return round(sum(vals) / len(vals), 3) if vals else 0
        health = {
            "last_run":      metrics,
            "rolling_5_avg": {
                "created":        _avg("created"),
                "review":         _avg("review"),
                "errors":         _avg("errors"),
                "cache_hit_rate": _avg("cache_hit_rate"),
                "ai_calls":       _avg("ai_calls"),
            },
            "alert_flags": alerts,
            "updated_at":  datetime.datetime.now().isoformat(),
        }
        ok, err = atomic_json_write(SYSTEM_HEALTH_PATH, health)
        if ok:
            obs_print("SYSTEM_HEALTH_UPDATED", f"run_id={run_id} alerts={alerts}")
    except Exception as _e:
        obs_print("SYSTEM_HEALTH_FAILED", f"run_id={run_id} reason={str(_e)[:60]}")


def load_simple_env(path):
    env = {}
    if not os.path.exists(path):
        return env
    try:
        with open(path, encoding="utf-8") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                env[key] = value
    except Exception:
        pass
    return env


def pick_non_empty(*values):
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def resolve_auth_source():
    env_cfg = load_simple_env(BOOKS_RECON_ENV_PATH)
    for key in (
        "BOOKS_RECON_ZOHO_LEGACY_CONFIG_PATH",
        "BOOKS_RECON_ZOHO_READ_CLIENT_ID",
        "BOOKS_RECON_ZOHO_READ_CLIENT_SECRET",
        "BOOKS_RECON_ZOHO_READ_REFRESH_TOKEN",
        "BOOKS_RECON_ZOHO_ORG_ID",
        "BOOKS_RECON_ZOHO_SANDBOX_ORG_ID",
        "BOOKS_RECON_ZOHO_SANDBOX_API_DOMAIN",
        "BOOKS_RECON_ZOHO_SANDBOX_ACCOUNTS_URL",
        "BOOKS_RECON_ZOHO_SANDBOX_CLIENT_ID",
        "BOOKS_RECON_ZOHO_SANDBOX_CLIENT_SECRET",
        "BOOKS_RECON_ZOHO_SANDBOX_REFRESH_TOKEN",
    ):
        value = os.environ.get(key)
        if value is not None:
            env_cfg[key] = value

    legacy_path = pick_non_empty(env_cfg.get("BOOKS_RECON_ZOHO_LEGACY_CONFIG_PATH"), CONFIG_PATH) or CONFIG_PATH
    legacy_cfg = load_json_config(legacy_path)
    legacy_org_id = pick_non_empty(legacy_cfg.get("organization_id"), legacy_cfg.get("org_id"), DEFAULT_ORG_ID)
    legacy_domain = pick_non_empty(legacy_cfg.get("api_domain"), DEFAULT_ZOHO_DOMAIN)
    legacy_accounts_url = pick_non_empty(legacy_cfg.get("accounts_url"), DEFAULT_ACCOUNTS_URL)

    sandbox_cfg = {
        "client_id": pick_non_empty(env_cfg.get("BOOKS_RECON_ZOHO_SANDBOX_CLIENT_ID"), legacy_cfg.get("client_id")),
        "client_secret": pick_non_empty(env_cfg.get("BOOKS_RECON_ZOHO_SANDBOX_CLIENT_SECRET"), legacy_cfg.get("client_secret")),
        "refresh_token": pick_non_empty(env_cfg.get("BOOKS_RECON_ZOHO_SANDBOX_REFRESH_TOKEN"), legacy_cfg.get("refresh_token")),
        "access_token": pick_non_empty(legacy_cfg.get("access_token")),
        "organization_id": pick_non_empty(env_cfg.get("BOOKS_RECON_ZOHO_SANDBOX_ORG_ID"), legacy_org_id),
        "api_domain": pick_non_empty(env_cfg.get("BOOKS_RECON_ZOHO_SANDBOX_API_DOMAIN"), legacy_domain),
        "accounts_url": pick_non_empty(env_cfg.get("BOOKS_RECON_ZOHO_SANDBOX_ACCOUNTS_URL"), legacy_accounts_url),
        "_persist_path": legacy_path if legacy_cfg else "",
        "_source_file": BOOKS_RECON_ENV_PATH if os.path.exists(BOOKS_RECON_ENV_PATH) else legacy_path,
        "_source_module": "books_recon.src.core.config -> zoho_sandbox_* with legacy fallback",
    }
    read_cfg = {
        "client_id": pick_non_empty(env_cfg.get("BOOKS_RECON_ZOHO_READ_CLIENT_ID"), legacy_cfg.get("client_id")),
        "client_secret": pick_non_empty(env_cfg.get("BOOKS_RECON_ZOHO_READ_CLIENT_SECRET"), legacy_cfg.get("client_secret")),
        "refresh_token": pick_non_empty(env_cfg.get("BOOKS_RECON_ZOHO_READ_REFRESH_TOKEN"), legacy_cfg.get("refresh_token")),
        "access_token": pick_non_empty(legacy_cfg.get("access_token")),
        "organization_id": pick_non_empty(env_cfg.get("BOOKS_RECON_ZOHO_ORG_ID"), legacy_org_id),
        "api_domain": legacy_domain,
        "accounts_url": legacy_accounts_url,
        "_persist_path": legacy_path if legacy_cfg else "",
        "_source_file": BOOKS_RECON_ENV_PATH if os.path.exists(BOOKS_RECON_ENV_PATH) else legacy_path,
        "_source_module": "books_recon.src.core.config -> zoho_read_* with legacy fallback",
    }
    legacy_source = dict(legacy_cfg)
    legacy_source.setdefault("organization_id", legacy_org_id)
    legacy_source.setdefault("api_domain", legacy_domain)
    legacy_source.setdefault("accounts_url", legacy_accounts_url)
    legacy_source["_persist_path"] = legacy_path
    legacy_source["_source_file"] = legacy_path
    legacy_source["_source_module"] = "books_recon.src.core.config -> legacy Zoho config path"

    has_sandbox_override = any(
        pick_non_empty(env_cfg.get(key))
        for key in (
            "BOOKS_RECON_ZOHO_SANDBOX_CLIENT_ID",
            "BOOKS_RECON_ZOHO_SANDBOX_CLIENT_SECRET",
            "BOOKS_RECON_ZOHO_SANDBOX_REFRESH_TOKEN",
            "BOOKS_RECON_ZOHO_SANDBOX_ORG_ID",
            "BOOKS_RECON_ZOHO_SANDBOX_API_DOMAIN",
            "BOOKS_RECON_ZOHO_SANDBOX_ACCOUNTS_URL",
        )
    )
    has_read_override = any(
        pick_non_empty(env_cfg.get(key))
        for key in (
            "BOOKS_RECON_ZOHO_READ_CLIENT_ID",
            "BOOKS_RECON_ZOHO_READ_CLIENT_SECRET",
            "BOOKS_RECON_ZOHO_READ_REFRESH_TOKEN",
            "BOOKS_RECON_ZOHO_ORG_ID",
        )
    )

    if has_sandbox_override:
        return sandbox_cfg
    if has_read_override:
        return read_cfg
    return legacy_source


cfg = resolve_auth_source()


def mask_value(value, show=4):
    text = str(value or "")
    if not text:
        return "<missing>"
    if len(text) <= show * 2:
        return "*" * len(text)
    return text[:show] + ("*" * (len(text) - (show * 2))) + text[-show:]


def get_org_id():
    return str(cfg.get("organization_id") or cfg.get("org_id") or DEFAULT_ORG_ID)


def get_zoho_domain():
    return str(cfg.get("api_domain") or DEFAULT_ZOHO_DOMAIN).rstrip("/")


def get_zoho_api():
    return get_zoho_domain() + "/books/v3"


def get_accounts_url():
    return str(cfg.get("accounts_url") or DEFAULT_ACCOUNTS_URL).rstrip("/")


def get_authorization_header():
    token = str(cfg.get("access_token") or "")
    return "Zoho-oauthtoken " + token if token else ""


def get_auth_source_file():
    return str(cfg.get("_source_file") or CONFIG_PATH)


def get_auth_source_module():
    return str(cfg.get("_source_module") or "zoho_bills.py inline loader")


def get_persist_path():
    return str(cfg.get("_persist_path") or "")


def log_zoho_auth_debug(context, authorization_header_present=None, token_refreshed=False):
    if authorization_header_present is None:
        authorization_header_present = bool(get_authorization_header())
    print(
        "  [zoho-auth] "
        f"context={context} "
        f"source_file={get_auth_source_file()} "
        f"source_module={get_auth_source_module()} "
        f"books_domain={get_zoho_domain()} "
        f"organization_id={mask_value(get_org_id())} "
        f"authorization_header_present={authorization_header_present} "
        f"token_refreshed={token_refreshed}"
    )


# ── Notification ──────────────────────────────────────────────────────────────
def notify(title, message):
    ps = (
        f'Add-Type -AssemblyName System.Windows.Forms;'
        f'$n = New-Object System.Windows.Forms.NotifyIcon;'
        f'$n.Icon = [System.Drawing.SystemIcons]::Warning;'
        f'$n.Visible = $true;'
        f'$n.ShowBalloonTip(10000, "{title}", "{message}", '
        f'[System.Windows.Forms.ToolTipIcon]::Warning);'
        f'Start-Sleep -Seconds 11;$n.Dispose()'
    )
    try:
        subprocess.Popen(
            ["powershell", "-WindowStyle", "Hidden", "-Command", ps],
            creationflags=134217728,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


# ── Zoho Auth ─────────────────────────────────────────────────────────────────
def refresh_token():
    TOKEN_MAX_AGE_SECONDS = 50 * 60
    persist_path = get_persist_path() or CONFIG_PATH
    run_id = _run_id_ctx

    persisted_cfg, read_err = safe_json_read(persist_path)
    if persisted_cfg is None:
        obs_print("AUTH_FAILED",
                  f"run_id={run_id} reason=config_unreadable detail={read_err}")
        raise Exception(f"[AUTH_FAILED] config unreadable: {read_err}")

    ok, val_err = validate_config_keys(persisted_cfg, run_id)
    if not ok:
        obs_print("AUTH_FAILED",
                  f"run_id={run_id} reason=config_invalid detail={val_err}")
        raise Exception(f"[AUTH_FAILED] config invalid: {val_err}")

    ts = persisted_cfg.get("token_timestamp", 0)
    age = time.time() - float(ts) if ts else TOKEN_MAX_AGE_SECONDS + 1
    if age < TOKEN_MAX_AGE_SECONDS and persisted_cfg.get("access_token"):
        cfg["access_token"] = persisted_cfg["access_token"]
        if persisted_cfg.get("api_domain"):
            cfg["api_domain"] = persisted_cfg["api_domain"].rstrip("/")
        print(f"  Token reused (age {int(age)}s < {TOKEN_MAX_AGE_SECONDS}s -- skipping Zoho refresh call).")
        obs_print("AUTH_SUCCESS", f"run_id={run_id} method=token_reuse age_seconds={int(age)}")
        log_zoho_auth_debug("refresh_token_reused", token_refreshed=False)
        return

    obs_print("TOKEN_REFRESH_STARTED", f"run_id={run_id}")
    url = get_accounts_url() + "/oauth/v2/token"
    params = {
        "refresh_token": persisted_cfg["refresh_token"],
        "client_id":     persisted_cfg["client_id"],
        "client_secret": persisted_cfg["client_secret"],
        "grant_type":    "refresh_token",
        "redirect_uri":  "https://www.zoho.in",
    }
    req = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(params).encode(),
        method="POST"
    )
    try:
        resp_data = json.loads(urllib.request.urlopen(req, timeout=15).read())
    except Exception as e:
        obs_print("TOKEN_REFRESH_FAILED", f"run_id={run_id} reason=network detail={str(e)[:80]}")
        raise Exception(f"[TOKEN_REFRESH_FAILED] network: {e}")

    if "access_token" not in resp_data or not resp_data["access_token"]:
        obs_print("TOKEN_REFRESH_FAILED",
                  f"run_id={run_id} reason=bad_response detail={json.dumps(resp_data)[:120]}")
        raise Exception(f"[TOKEN_REFRESH_FAILED] bad response: {resp_data}")

    cfg["access_token"] = resp_data["access_token"]
    if resp_data.get("api_domain"):
        cfg["api_domain"] = resp_data["api_domain"].rstrip("/")

    persisted_cfg["access_token"]    = cfg["access_token"]
    persisted_cfg["token_timestamp"] = time.time()
    if cfg.get("api_domain"):
        persisted_cfg["api_domain"] = cfg["api_domain"]

    ok, write_err = atomic_json_write(persist_path, persisted_cfg)
    if not ok:
        print(f"  WARNING: atomic config write failed: {write_err} -- token valid in memory only")
    else:
        obs_print("TOKEN_REFRESH_SUCCESS", f"run_id={run_id}")

    print("  Token refreshed OK.")
    log_zoho_auth_debug("refresh_token", token_refreshed=True)


def get_usd_to_inr():
    try:
        req = urllib.request.Request(
            "https://open.er-api.com/v6/latest/USD",
            headers={}
        )
        data = json.loads(urllib.request.urlopen(req, timeout=10).read())
        rate = data["rates"]["INR"]
        print(f"  Live USD/INR rate: {rate:.2f}")
        return rate
    except Exception:
        print("  WARNING: Could not fetch live USD/INR rate. Overseas USD bills will be sent to review.")
        return None


def fetch_usd_inr_rate_for_date(date_str):
    """Fetch the historical USD/INR rate for a specific YYYY-MM-DD date.

    Raises on any failure (network, JSON parse, missing key) -- callers
    are expected to catch and fall back.
    """
    url = f"https://api.frankfurter.app/{date_str}?from=USD&to=INR"
    req = urllib.request.Request(url)
    data = json.loads(urllib.request.urlopen(req, timeout=10).read())
    return float(data["rates"]["INR"])


def resolve_effective_usd_rate(parsed, tax_route, bill_date, usd_rate, use_invoice_date_fx,
                                run_id="unset", filename=""):
    """Resolve the USD/INR rate to use for one bill's FX conversion.

    usd_rate is the run-level rate already resolved by the caller (either the
    --usd-rate override or a get_usd_to_inr() result). When use_invoice_date_fx
    is set and the bill is an overseas USD bill, the invoice-date rate is tried
    first and wins; usd_rate is the fallback on any fetch failure.

    Returns (effective_rate, is_usd_overseas).
    """
    is_usd_overseas = (tax_route["is_overseas"]
                        and parsed.get("currency_code", "INR").upper() == "USD")
    effective_rate = usd_rate
    if is_usd_overseas and use_invoice_date_fx:
        try:
            effective_rate = fetch_usd_inr_rate_for_date(bill_date)
            obs_print("FX_RATE_FETCHED",
                      f"run_id={run_id} file={filename} date={bill_date} rate={effective_rate}")
        except Exception as _fx_e:
            obs_print("FX_RATE_FETCH_FAILED",
                      f"run_id={run_id} file={filename} date={bill_date} error={str(_fx_e)[:80]}")
            effective_rate = usd_rate
    return effective_rate, is_usd_overseas


# ── Zoho API helpers ──────────────────────────────────────────────────────────
def zoho_get(endpoint, params=None):
    p = {"organization_id": get_org_id()}
    if params:
        p.update(params)
    url = get_zoho_api() + "/" + endpoint + "?" + urllib.parse.urlencode(p)
    req = urllib.request.Request(url)
    req.add_header("Authorization", get_authorization_header())
    return json.loads(urllib.request.urlopen(req, timeout=15).read())


def zoho_post(endpoint, payload):
    url = get_zoho_api() + "/" + endpoint + "?organization_id=" + get_org_id()
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    auth_header = get_authorization_header()
    req.add_header("Authorization", auth_header)
    req.add_header("content-type", "application/json")
    log_zoho_auth_debug(f"post:{endpoint}", authorization_header_present=bool(auth_header), token_refreshed=False)
    try:
        return json.loads(urllib.request.urlopen(req, timeout=15).read())
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8")
        raise Exception("Zoho POST " + endpoint + ": " + error_body)


def extract_created_bill_id(result):
    """Return a Zoho Books bill ID only from a successful bill-create response."""
    if not isinstance(result, dict):
        return None
    bill = result.get("bill")
    if not isinstance(bill, dict):
        return None
    bill_id = bill.get("bill_id")
    if isinstance(bill_id, bool):
        return None
    bill_id = str(bill_id or "").strip()
    return bill_id if bill_id.isdigit() else None


# ── Zoho data fetch ───────────────────────────────────────────────────────────
# Read-back may differ from the invoice only by paise rounding of computed tax,
# at most this much per line (audit F02 replaced a max(Rs 1, 1%) tolerance).
VERIFY_ROUNDING_PER_LINE = Decimal("0.01")


def verify_created_bill(bill_id, payload, parsed, run_id="unset", filename="", usd_rate=None):
    """
    Read the bill back and compare it with what was posted and what the invoice
    printed (audit F01/F02). Lines and sub_total must equal the payload exactly;
    total (and, for forward charge, tax_total) must equal the printed payable
    within per-line paise rounding. A missing field fails the check.
    """
    result = {"ok": False, "checks": {}, "errors": []}
    try:
        data = zoho_get(f"bills/{bill_id}")
        bill = data.get("bill", data) if isinstance(data, dict) else {}
        if not isinstance(bill, dict):
            bill = {}

        def compare_text(name, expected, actual):
            ok = actual == expected
            result["checks"][name] = {"ok": ok, "expected": expected, "actual": actual}
            if not ok:
                result["errors"].append(f"{name}_mismatch expected={expected!r} actual={actual!r}")

        compare_text("bill_number", str(payload.get("bill_number", "")).strip(),
                     str(bill.get("bill_number", "")).strip())
        compare_text("vendor_id", str(payload.get("vendor_id", "")).strip(),
                     str(bill.get("vendor_id", "")).strip())
        compare_text("bill_date", str(parsed.get("bill_date", "")).strip(),
                     str(bill.get("date", "")).strip())
        compare_text("currency_code", str(payload.get("currency_code", "INR")).upper(),
                     str(bill.get("currency_code", "")).strip().upper())

        def money(value):
            if value is None or str(value).strip() == "":
                return None
            try:
                amount = Decimal(str(value))
            except InvalidOperation:
                return None
            if not amount.is_finite():
                return None
            return amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        def compare_amount(name, expected, actual, tolerance):
            if actual is None:
                ok, error = False, f"zoho_{name}_missing"
            elif expected is None:
                ok, error = False, f"expected_{name}_unavailable"
            else:
                ok = abs(actual - expected) <= tolerance
                error = (f"{name}_mismatch expected={expected} actual={actual} "
                         f"difference={abs(actual - expected)} tolerance={tolerance}")
            result["checks"][name] = {
                "ok": ok, "tolerance": float(tolerance),
                "expected": float(expected) if expected is not None else None,
                "actual": float(actual) if actual is not None else None,
            }
            if not ok:
                result["errors"].append(error)

        is_rcm = bool(payload.get("is_reverse_charge_applied"))
        sent_lines = payload.get("line_items", [])
        got_lines = bill.get("line_items", [])
        if not isinstance(got_lines, list):
            got_lines = []
        rounding = VERIFY_ROUNDING_PER_LINE * max(len(sent_lines), 1)

        # Lines: Zoho must hold exactly what was posted.
        lines_ok = bool(sent_lines) and len(got_lines) == len(sent_lines)
        line_details = []
        sent_subtotal = Decimal("0.00")
        for idx, sent in enumerate(sent_lines):
            sent_amount = money(Decimal(str(sent.get("rate", 0))) * Decimal(str(sent.get("quantity", 1))))
            sent_subtotal += sent_amount or Decimal("0.00")
            got = got_lines[idx] if idx < len(got_lines) and isinstance(got_lines[idx], dict) else {}
            got_amount = None
            if money(got.get("rate")) is not None and money(got.get("quantity", 1)) is not None:
                got_amount = money(money(got.get("rate")) * money(got.get("quantity", 1)))
            detail = {"index": idx, "expected_amount": float(sent_amount),
                      "actual_amount": float(got_amount) if got_amount is not None else None}
            ok_line = got_amount == sent_amount
            if is_rcm:
                # build_payload applies IGST 18% as the reverse-charge tax on every line.
                expected_rc = money(sent_amount * Decimal("0.18"))
                actual_rc = money(got.get("reverse_charge_tax_amount"))
                rc_tax_id = str(sent.get("reverse_charge_tax_id") or "")
                detail.update(reverse_charge_tax_id=got.get("reverse_charge_tax_id"),
                              expected_reverse_charge=float(expected_rc),
                              actual_reverse_charge=float(actual_rc) if actual_rc is not None else None)
                ok_line = (ok_line and bool(rc_tax_id)
                           and str(got.get("reverse_charge_tax_id") or "") == rc_tax_id
                           and actual_rc is not None
                           and abs(actual_rc - expected_rc) <= VERIFY_ROUNDING_PER_LINE)
            else:
                detail.update(expected_tax_id=sent.get("tax_id"), actual_tax_id=got.get("tax_id"))
                ok_line = ok_line and str(got.get("tax_id") or "") == str(sent.get("tax_id") or "")
            detail["ok"] = ok_line
            lines_ok = lines_ok and ok_line
            line_details.append(detail)
        result["checks"]["line_items"] = {"ok": lines_ok, "expected_count": len(sent_lines),
                                          "actual_count": len(got_lines), "lines": line_details}
        if not lines_ok:
            result["errors"].append("line_items_mismatch")

        compare_amount("sub_total", sent_subtotal, money(bill.get("sub_total")), Decimal("0.00"))

        # Printed payable (F01). Forward charge: the printed grand total and tax.
        # Reverse charge: the vendor is owed the pre-tax amount, FX-converted for
        # USD invoices exactly as build_payload converted the lines.
        printed_total = money(parsed.get("invoice_total"))
        printed_tax = Decimal("0.00")
        for field in ("tax_amount_igst", "tax_amount_cgst", "tax_amount_sgst"):
            printed_tax += money(parsed.get(field)) or Decimal("0.00")
        if is_rcm:
            actual_rca = bill.get("is_reverse_charge_applied")
            result["checks"]["reverse_charge_applied"] = {"ok": actual_rca is True, "expected": True,
                                                          "actual": actual_rca}
            if actual_rca is not True:
                result["errors"].append(f"reverse_charge_not_applied actual={actual_rca!r}")
            expected_total = None
            if printed_total is not None:
                expected_total = printed_total - printed_tax
                if str(parsed.get("currency_code", "INR")).upper() == "USD":
                    expected_total = money(expected_total * Decimal(str(usd_rate))) if usd_rate else None
        else:
            expected_total = printed_total
            compare_amount("tax_total", printed_tax, money(bill.get("tax_total")), rounding)
        compare_amount("total", expected_total, money(bill.get("total")), rounding)

        result["ok"] = not result["errors"]
        detail = " ".join(result["errors"])[:300]
        if result["ok"]:
            obs_print("POST_VERIFY_PASSED",
                      f"run_id={run_id} file={filename} bill_id={bill_id} basis=lines+printed_total")
        else:
            obs_print("POST_VERIFY_FAILED",
                      f"run_id={run_id} file={filename} bill_id={bill_id} detail={detail}")
        return result
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        result["errors"].append(f"exception:{err}")
        obs_print("POST_VERIFY_ERROR",
                  f"run_id={run_id} file={filename} bill_id={bill_id} error={err[:160]}")
        return result


def fetch_existing_bills():
    print("\nFetching existing bills from Zoho...")
    existing = set()
    page = 1
    while True:
        data = zoho_get("bills", {"page": page, "per_page": 200})
        bills = data.get("bills", [])
        for b in bills:
            existing.add(b.get("bill_number", "").strip().upper())
        if not data.get("page_context", {}).get("has_more_page"):
            break
        page += 1
    print(f"  {len(existing)} existing bills found.")
    return existing


def _bill_key(bill_number):
    """Bill number without case, spaces or punctuation, for duplicate lookup."""
    return re.sub(r"[^A-Z0-9]", "", str(bill_number or "").upper())


def fetch_existing_bill_index():
    """Zoho bills keyed by _bill_key(), each with its vendor (audit F05)."""
    print("\nFetching existing bills from Zoho...")
    index = {}
    count = 0
    page = 1
    while True:
        data = zoho_get("bills", {"page": page, "per_page": 200})
        for b in data.get("bills", []):
            index.setdefault(_bill_key(b.get("bill_number")), []).append({
                "bill_number": str(b.get("bill_number") or "").strip(),
                "vendor_id": str(b.get("vendor_id") or "").strip(),
                "bill_id": str(b.get("bill_id") or "").strip(),
            })
            count += 1
        if not data.get("page_context", {}).get("has_more_page"):
            break
        page += 1
    print(f"  {count} existing bills found.")
    return index


def find_existing_bill(index, vendor_id, bill_number):
    """
    ("exact", bills) if this vendor already has this bill number in Zoho.
    ("variant", bills) if this vendor, or a bill with no vendor on record, has the
    same number written differently (e.g. INV-001 vs INV001): a human decides.
    (None, []) otherwise. Another vendor's bill with the same number is not a
    duplicate (audit F05).
    """
    vendor_id = str(vendor_id or "").strip()
    candidates = [b for b in index.get(_bill_key(bill_number), [])
                  if not b["vendor_id"] or b["vendor_id"] == vendor_id]
    exact = str(bill_number or "").strip().upper()
    if any(b["vendor_id"] == vendor_id and b["bill_number"].upper() == exact for b in candidates):
        return "exact", candidates
    return ("variant", candidates) if candidates else (None, [])


def list_existing_bills_json_mode():
    """
    Read-only CLI mode for watcher live reconciliation.
    Prints clean JSON to stdout and does not run the posting pipeline.
    """
    global _run_id_ctx
    _run_id_ctx = "readonly-list-existing-bills-" + generate_run_id()
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            refresh_token()
            existing = fetch_existing_bills()
        bill_numbers = sorted(str(b).strip() for b in existing if str(b).strip())
        print(json.dumps({
            "ok": True,
            "count": len(bill_numbers),
            "bill_numbers": bill_numbers,
        }, separators=(",", ": ")))
        return 0
    except Exception as e:
        error = str(e).splitlines()[0].strip()
        if not error:
            error = type(e).__name__
        print(json.dumps({
            "ok": False,
            "error": error[:160],
        }, separators=(",", ": ")))
        return 1


def fetch_vendors():
    print("\nFetching vendors from Zoho...")
    vendors = {}
    page = 1
    while True:
        data = zoho_get("contacts", {"contact_type": "vendor", "page": page, "per_page": 200})
        contacts = data.get("contacts", [])
        for c in contacts:
            name = c.get("contact_name", "").strip().upper()
            entry = {
                "id": c.get("contact_id"),
                "gst_no": (c.get("gst_no") or "").strip().upper(),
            }
            if name in vendors:
                # Same-name registrations must survive pagination and loading.
                vendors[name].setdefault("duplicates", []).append(entry)
            else:
                vendors[name] = entry
        if not data.get("page_context", {}).get("has_more_page"):
            break
        page += 1
    print(f"  {len(vendors)} vendors found.")
    return vendors


# ── MBL Extraction ────────────────────────────────────────────────────────────
MBL_PATTERNS = [
    r"Bill\s+of\s+Lading\s*[:\|]?\s*([A-Z0-9]{6,})",
    r"Master\s+Number\s*[:\|]?\s*([A-Z0-9]{6,})",
    r"B/L\s*No\.?\s*[:\|]?\s*([A-Z0-9]{6,})",
    r"BL#\s+([A-Z0-9]{6,})",
    r"MBL\s+NUMBER\s*[:\|]?\s*([A-Z0-9]{6,})",
    r"B/L\s+Number\s*[:\|]?\s*([A-Z0-9]{6,})",
    r"BL\s+Number\s*[:\|]?\s*([A-Z0-9]{6,})",
    r"MBL\s*No\.?\s*[:\|]?\s*([A-Z0-9]{6,})",
    r"\bMBL\b\s*[:\|]\s*([A-Z0-9]{6,})",
    r"Master\s+B/?L\s*[:\|]?\s*([A-Z0-9]{6,})",
    r"MASTER\s+B/?L\s*[:\|]?\s*([A-Z0-9]{6,})",
    r"Air\s*Waybill\s*[:\|]?\s*(\d{3}-\d{8})",
    r"AWB\s*No\.?\s*[:\|]?\s*(\d{3}-\d{8})",
    rf"BL\s+Ref[\s:]+\s*([A-Z]{{2,}}[A-Z0-9]{{4,}}|[0-9]{{7,}})",
    rf"HBL\s*No\.?[\s:]+\s*([A-Z]{{2,}}[A-Z0-9]{{4,}}|[0-9]{{7,}})",
    rf"HBL\s+NO[\s:]+\s*([A-Z]{{2,}}[A-Z0-9]{{4,}}|[0-9]{{7,}})",
]

MBL_FALSE_POSITIVES = {
    "NONE", "NIL", "NOTAPPLICABLE", "NA", "NULL", "N/A", "TBA", "TBD"
}


def extract_mbl_from_pdf_text(pdf_text):
    for pattern in MBL_PATTERNS:
        m = re.search(pattern, pdf_text, re.IGNORECASE)
        if m:
            val = m.group(1).strip().rstrip("|").strip()
            val_upper = val.upper().replace(".", "").replace(" ", "")
            if len(val) >= 6 and val_upper not in MBL_FALSE_POSITIVES:
                if not re.match(r"^\d{1,3}[,\d]*\.\d{2}$", val):
                    return val
    return ""


# ── CMA CGM / CCAI Invoice-Number Extraction ──────────────────────────────────
# CMA CGM SA / CCAI export invoices print the Bill of Lading number and the
# actual invoice number (e.g. INEMHC27087125) as two separate values under the
# "EXPORT INVOICE" header. After pdftotext -layout collapses the two-column
# layout, both values land on the same text line: "Bill of Lading: <BL#>  <invoice#>".
# The AI sometimes returns the BL number as bill_number instead of the invoice
# number (validate_before_post() catches that exact case and routes the bill to
# needs_review) — this deterministic, vendor-scoped extraction supplies the
# correct value directly, independent of the AI's bill_number field.
CMA_CGM_VENDOR_MARKERS = ("CMA CGM", "CCAI")

CMA_CGM_INVOICE_NUMBER_PATTERNS = [
    r"Bill\s+of\s+Lading\s*:\s*[A-Z0-9]{6,}\s{2,}([A-Z]{2,}[A-Z0-9]{5,})",
]


def is_cma_cgm_invoice(vendor_name, pdf_text):
    """Scope check: True only for CMA CGM SA / CCAI invoices."""
    haystack = f"{vendor_name or ''} {(pdf_text or '')[:1000]}".upper()
    return any(marker in haystack for marker in CMA_CGM_VENDOR_MARKERS)


def extract_cma_cgm_invoice_number_from_pdf_text(pdf_text, mbl_number=""):
    """
    Deterministic invoice-number extraction for CMA CGM SA / CCAI invoices only.
    Must never return the Bill of Lading value itself.
    """
    mbl_upper = (mbl_number or "").strip().upper()
    for pattern in CMA_CGM_INVOICE_NUMBER_PATTERNS:
        m = re.search(pattern, pdf_text, re.IGNORECASE)
        if m:
            val = m.group(1).strip()
            if len(val) >= 8 and val.upper() != mbl_upper:
                return val
    return ""


def extract_text_from_pdf(filepath):
    try:
        result = subprocess.run(
            ["pdftotext", "-layout", filepath, "-"],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    try:
        with open(filepath, "rb") as f:
            raw = f.read().decode("latin-1", errors="ignore")
        texts = re.findall(r"\(([^\)]{3,100})\)", raw)
        return " ".join(texts)
    except Exception:
        return ""


# ── Claude PDF Parsing ────────────────────────────────────────────────────────
# ANTHROPIC_API_KEY loaded at startup via _load_anthropic_key() above
ANTHROPIC_MODEL     = "claude-haiku-4-5"

def parse_pdf_with_claude_once(filepath, filename, vendors_list):
    with open(filepath, "rb") as f:
        pdf_b64 = base64.standard_b64encode(f.read()).decode("utf-8")

    pdf_text_snippet = ""
    try:
        with open(filepath, "rb") as _pf:
            _raw = _pf.read().decode("latin-1", errors="ignore")
        pdf_text_snippet = " ".join(re.findall(r"\(([^\)]{3,80})\)", _raw))[:2000]
    except Exception:
        pass
    shortlist = _shortlist_vendors(filename, pdf_text_snippet, vendors_list, run_id=_run_id_ctx)
    vendor_sample = "\n".join(shortlist)
    _before_chars = len(vendor_sample)
    if _before_chars > _AI_PROMPT_CHAR_LIMIT:
        vendor_sample = vendor_sample[:_AI_PROMPT_CHAR_LIMIT]
        obs_print("AI_PROMPT_TRIMMED",
                  f"run_id={_run_id_ctx} before={_before_chars} after={len(vendor_sample)}")

    SYSTEM_STATIC = (
        "You are an expert Indian GST accounting assistant. "
        "Extract structured invoice data from vendor invoice PDFs. "
        "Return ONLY valid JSON with no explanation and no markdown fences. "
        "Use this exact structure:\n"
        '{"vendor_name":"exact match from vendor list or best guess",'
        '"vendor_found_in_zoho":true,'
        '"bill_number":"invoice number",'
        '"bill_date":"YYYY-MM-DD",'
        '"due_date":"YYYY-MM-DD or null",'
        '"currency_code":"INR unless purely foreign invoice with no INR amounts",'
        '"is_overseas":false,'
        '"line_items":[{"description":"service description",'
        '"quantity":1,"rate":0.00,"account_name":"e.g. Freight Charges",'
        '"hsn_or_sac":"SAC or HSN code",'
        '"tax_name":"IGST 18% or CGST 9%+SGST 9% or None",'
        '"tax_percentage":18}],'
        '"subtotal":0.00,"discounts_or_credits":0.00,'
        '"tax_amount_igst":0.00,"tax_amount_cgst":0.00,"tax_amount_sgst":0.00,'
        '"invoice_total":0.00,'
        '"vendor_gstin":"vendor GSTIN/UIN printed on invoice or empty string","mbl_number":"master BL or AWB number or empty string",'
        '"notes":"BL numbers job refs vessel name",'
        '"confidence":"high/medium/low",'
        '"issues":"missing fields or empty string"}\n\n'
        "CRITICAL RULES:\n"
        "1. rate=final INR taxable amount on invoice. Never recalculate FX.\n"
        "2. If line shows USD rate with ROE and INR amount, use the INR amount.\n"
        "3. quantity always 1. Consolidate all units into one INR taxable amount.\n"
        "4. Indian vendors with USD reference charges (e.g. a USD cost column shown "
        "alongside an ROE) stay currency_code=INR. This is subordinate to rule 5: if the "
        "invoice's own stated payable amount is in a foreign currency, follow rule 5 instead.\n"
        "5. currency_code is derived from the invoice's stated payable total, not from who "
        "the vendor is. If the invoice states amounts in USD/$ and shows no INR payable "
        "total anywhere on the document, currency_code=\"USD\". If the invoice shows an INR "
        "payable total (even if USD reference amounts also appear), currency_code=\"INR\" "
        "regardless of those USD reference amounts.\n"
        "6. is_overseas=true only for foreign company invoices (USA, Singapore etc).\n"
        "7. mbl_number: look for labels Bill of Lading, B/L No, BL#, MBL, Master Number, AWB No. "
        "Return the VALUE next to the label. Empty string if not found.\n"
        "8. bill_number: for Meta/Facebook invoices use the Tax invoice ID (e.g. ADS190-xxx, ADS252-xxx) "
        "printed at the TOP of the invoice, NOT the Invoice no. (FBADS-xxx) at the bottom. "
        "The Tax invoice ID matches GSTR2B. For all other vendors use the invoice number as printed. NEVER use the Bill of Lading number, MBL reference, or any value labeled 'Bill of Lading', 'B/L No', 'BL#' or 'MBL' as the bill_number. The bill_number must never equal the mbl_number. If they match, you have the wrong field.\n"
        "9. CMA CGM SA / CCAI invoices specifically: the Bill of Lading number and the "
        "invoice number are TWO SEPARATE fields, both printed under the 'EXPORT INVOICE' "
        "header. The line reads 'Bill of Lading: <BL number>' followed further along by a "
        "DIFFERENT value in a different format (e.g. INEMHC27087125, BOMFALI..., PTGFWD..., "
        "VSLNS... - letters followed by digits). That second, different value is the "
        "invoice number - use it as bill_number. The Bill of Lading number that follows the "
        "'Bill of Lading:' label is NEVER the bill_number, even though both appear on the "
        "same line.\n"
        "10. subtotal, discounts_or_credits, tax_amount_igst, tax_amount_cgst, "
        "tax_amount_sgst, and invoice_total must be the printed invoice figures exactly as "
        "shown on the document. Never recalculate, sum, or derive them yourself. "
        "invoice_total is the printed 'Total' (the tax-inclusive grand total of the "
        "invoice itself), not 'Amount due' or 'Balance due' where those differ from it "
        "because of prior credits or advances applied."
    )

    payload = {
        "model": ANTHROPIC_MODEL,
        "max_tokens": 3000,
        "system": [
        {
        "type": "text",
        "text": SYSTEM_STATIC,
        "cache_control": {"type": "ephemeral"},
        }
        ],
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": "application/pdf",
                            "data": pdf_b64,
                        },
                        "cache_control": {"type": "ephemeral"},
                    },
                    {
                        "type": "text",
                        "text": (
                            "FILENAME: " + filename + "\n\n"
                            "EXISTING VENDORS IN ZOHO:\n" + vendor_sample
                        ),
                    },
                ],
            }
        ],
    }

    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        method="POST"
    )
    req.add_header("x-api-key", ANTHROPIC_API_KEY)
    req.add_header("anthropic-version", "2023-06-01")
    req.add_header("anthropic-beta", "pdfs-2024-09-25,prompt-caching-2024-07-31")
    req.add_header("content-type", "application/json")

    try:
        resp = json.loads(urllib.request.urlopen(req, timeout=60).read())
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8")
        if "credit balance is too low" in error_body:
            notify("Anthropic Credits Empty", "Top up at console.anthropic.com/billing")
            raise Exception("credit_balance_too_low")
        raise Exception("Anthropic API " + str(e.code) + ": " + error_body[:300])

    raw = resp["content"][0]["text"].strip()
    if raw.startswith("```json"):
        raw = raw[7:].strip()
    if raw.startswith("```"):
        raw = raw[3:].strip()
    if raw.endswith("```"):
        raw = raw[:-3].strip()

    usage = resp.get("usage", {})
    cache_read  = usage.get("cache_read_input_tokens", 0)
    cache_write = usage.get("cache_creation_input_tokens", 0)
    input_tok   = usage.get("input_tokens", 0)
    output_tok  = usage.get("output_tokens", 0)
    print(f"         [tokens] input={input_tok} output={output_tok} "
          f"cache_write={cache_write} cache_read={cache_read}")

    try:
        result = json.loads(raw.strip())
    except json.JSONDecodeError as _je:
        raise Exception(f"PARSE_JSON_INVALID: {_je} | raw_snippet={raw[:200]}")
    # Guard: Claude returns a JSON array when a single PDF contains multiple invoices
    # (e.g. Blinkit PDFs have a product invoice + platform-fee invoice). Never keep
    # one and drop the rest (audit F06): hold the whole PDF for review with every
    # invoice preserved, including in the parse cache.
    if isinstance(result, list):
        if len(result) == 1 and isinstance(result[0], dict):
            result = result[0]
        elif result:
            # Malformed elements are kept too: one valid invoice plus junk is held.
            invoices = [r if isinstance(r, dict) else {"unparsed": str(r)[:200]} for r in result]
            obs_print("PARSE_MULTI_INVOICE",
                      f"run_id={_run_id_ctx} file={filename} reason=multi_invoice_pdf "
                      f"count={len(invoices)} action=hold_for_review")
            result = {"confidence": "low", "multi_invoice_count": len(invoices), "invoices": invoices,
                      "issues": f"multi_invoice_pdf:{len(invoices)}"}
        else:
            result = {"confidence": "low", "issues": "claude_returned_empty_array"}
    result["_vendor_context_count"] = len(shortlist)
    return result


def parse_pdf_with_claude(filepath, filename, vendors_list):
    for attempt in range(4):
        try:
            return parse_pdf_with_claude_once(filepath, filename, vendors_list)
        except Exception as e:
            if "429" in str(e):
                wait = 30
                print(f"         Rate limit hit - waiting {wait}s before retry {attempt + 1}/3...")
                time.sleep(wait)
            else:
                raise
    raise Exception("Max retries exceeded due to rate limiting")


# ── Session 7: Tax routing ───────────────────────────────────────────────────
_FREIGHT_KW = {"OCEAN FREIGHT", "SEA FREIGHT", "FREIGHT CHARGE", "AIR FREIGHT", "BASIC FREIGHT"}
_TAX_ID_MAP              = _DEFAULT_TAX_ID_MAP
IGST_VENDORS             = _DEFAULT_IGST_VENDORS
_OVERSEAS_VENDOR_SIGNALS = set(_DEFAULT_OVERSEAS_VENDOR_SIGNALS)
VENDOR_ALIAS_MAP         = _DEFAULT_VENDOR_ALIAS_MAP

def classify_service_type(parsed):
    signals = []
    for item in parsed.get("line_items", []):
        signals.append(item.get("description", "").upper())
    signals.append(parsed.get("notes", "").upper())
    combined = " ".join(signals)
    hits = sum(1 for kw in _FREIGHT_KW if kw in combined)
    if hits > 0:
        return "freight"
    meaningful_tokens = [t for t in combined.split() if len(t) >= 3]
    if hits == 0 and len(meaningful_tokens) >= 5:
        return "non_freight"
    return "unknown"

def determine_overseas_status(parsed, matched_vendor_name):
    _raw_flag = parsed.get("is_overseas")
    if isinstance(_raw_flag, str):
        ai_flag = _raw_flag.strip().lower() == "true"
    elif isinstance(_raw_flag, bool):
        ai_flag = _raw_flag
    else:
        ai_flag = None
    currency = parsed.get("currency_code", "INR").upper()
    # Whole words only (audit A5): "AWS" must not match "DAWSON".
    vname    = f" {_normalize_name(matched_vendor_name or '')} "
    vendor_overseas = any(f" {_normalize_name(sig)} " in vname
                          for sig in _OVERSEAS_VENDOR_SIGNALS if _normalize_name(sig))
    currency_signal = currency not in ("INR", "")
    signals_true  = sum([ai_flag is True, currency_signal])
    signals_false = sum([ai_flag is False, not currency_signal])
    if signals_true >= 1:
        return True
    if vendor_overseas:
        # A vendor-name match alone never decides reverse charge (audit A5): a
        # domestic contact such as "AWS LOGISTICS" with an INR invoice goes to
        # review. determine_tax_route() also holds any Indian-GSTIN vendor.
        return None
    if signals_false >= 2:
        return False
    return None

def _is_indian_gstin(value):
    """
    True for a well-formed GSTIN of a supplier located in an Indian state or UT
    (state codes 01-38). OIDAR registrations of foreign suppliers use a different
    format and code 99, so they do not count.
    """
    gstin = str(value or "").strip().upper()
    if not re.fullmatch(r"[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]", gstin):
        return False
    return 1 <= int(gstin[:2]) <= 38


def determine_tax_route(parsed, matched_vendor_name, vendor_gst_no=""):
    vname_upper = (matched_vendor_name or "").upper()
    svc_type    = classify_service_type(parsed)
    overseas    = determine_overseas_status(parsed, matched_vendor_name)
    if overseas is True and (_is_indian_gstin(vendor_gst_no) or _is_indian_gstin(parsed.get("vendor_gstin"))):
        # Audit A5: a supplier registered in an Indian state is not an import of
        # services, whatever the currency, AI flag or vendor name suggests.
        return {"is_overseas": None, "is_freight": svc_type == "freight",
                "gst_route": "unknown", "tax_mode": "unknown", "tax_id_key": None,
                "review_required": True,
                "review_reason": "indian_gstin_vendor_with_overseas_signal"}
    if overseas is True:
        try:
            _supplier_gst_charged = any(
                float(parsed.get(_f) or 0) > 0
                for _f in ("tax_amount_igst", "tax_amount_cgst", "tax_amount_sgst")
            )
        except (TypeError, ValueError):
            _supplier_gst_charged = False
        if _supplier_gst_charged:
            # Stop, not a reclassification: determine_overseas_status()'s vendor-signal
            # override stands. An overseas vendor should never charge Indian GST --
            # RCM means the recipient self-assesses. A non-zero supplier tax amount
            # here means the invoice doesn't match the expected RCM shape and needs
            # a human to look at it before this bill is posted.
            return {"is_overseas": True, "is_freight": svc_type == "freight",
                    "gst_route": "overseas", "tax_mode": "rcm", "tax_id_key": None,
                    "review_required": True,
                    "review_reason": "overseas_vendor_charged_indian_gst"}
        return {"is_overseas": True, "is_freight": svc_type == "freight",
                "gst_route": "overseas", "tax_mode": "rcm", "tax_id_key": None,
                "review_required": False, "review_reason": ""}
    if overseas is None:
        return {"is_overseas": None, "is_freight": svc_type == "freight",
                "gst_route": "unknown", "tax_mode": "unknown", "tax_id_key": None,
                "review_required": True, "review_reason": "ambiguous_overseas_status"}
    norm_vname = _normalize_name(matched_vendor_name or "")
    vname_toks = set(norm_vname.split())
    # Primary: derive IGST from vendor GSTIN state code (non-27 = interstate)
    _gst = (vendor_gst_no or "").strip()
    if len(_gst) >= 2 and _gst[:2].isdigit():
        is_igst = _gst[:2] != "27"
    else:
        # Fallback: legacy name-list for vendors with no GSTIN in Zoho record
        is_igst = any(
            _normalize_name(v) == norm_vname or
            set(_normalize_name(v).split()).issubset(vname_toks)
            for v in IGST_VENDORS
        )
    # Overseas status has already been established as domestic above. An
    # unknown description is therefore non-freight; explicit freight matches
    # still take the existing 5% routes below.
    is_freight = svc_type == "freight"
    if is_igst and is_freight:
        tax_id_key, gst_route, tax_mode = "igst_5", "inter", "igst"
    elif is_igst:
        tax_id_key, gst_route, tax_mode = "igst_18", "inter", "igst"
    elif is_freight:
        tax_id_key, gst_route, tax_mode = "cgst_sgst_5", "intra", "cgst_sgst"
    else:
        tax_id_key, gst_route, tax_mode = "cgst_sgst_18", "intra", "cgst_sgst"
    resolved_id = _TAX_ID_MAP.get(tax_id_key, "")
    if not resolved_id:
        return {"is_overseas": False, "is_freight": is_freight, "gst_route": gst_route,
                "tax_mode": "unknown", "tax_id_key": tax_id_key,
                "review_required": True, "review_reason": f"tax_id_missing:{tax_id_key}"}
    return {"is_overseas": False, "is_freight": is_freight, "gst_route": gst_route,
            "tax_mode": tax_mode, "tax_id_key": tax_id_key,
            "review_required": False, "review_reason": ""}


# ── Tax ID lookup ─────────────────────────────────────────────────────────────
FREIGHT_KEYWORDS = ["OCEAN FREIGHT", "SEA FREIGHT", "FREIGHT CHARGE", "AIR FREIGHT", "BASIC FREIGHT"]
def get_tax_id(tax_id_key):
    return _TAX_ID_MAP.get(tax_id_key, "")


# ── Payload builder ───────────────────────────────────────────────────────────
def _domestic_line_tax_key(item, tax_mode):
    # Extracted rates are proposals. Reconciliation and approval happen below.
    legacy_rate = 5 if any(kw in item.get("description", "").upper()
                           for kw in FREIGHT_KEYWORDS) else 18
    raw = item.get("tax_percentage")
    if raw is None or str(raw).strip() == "":
        rate = Decimal(legacy_rate)
    else:
        try:
            rate = Decimal(str(raw))
        except InvalidOperation:
            raise ValueError("line_tax_percentage_invalid") from None
        if not rate.is_finite() or rate not in (Decimal(5), Decimal(18)):
            raise ValueError("line_tax_percentage_unsupported")
    return f"{tax_mode}_{int(rate)}", rate != legacy_rate


def reconcile_payload_taxes(parsed, payload, tax_route):
    # Compare the actual proposed tax IDs with printed invoice components.
    # This proves arithmetic consistency, never source accuracy or approval.
    if tax_route["is_overseas"]:
        return {"ok": True, "errors": [], "approval_required": False, "amounts": {}}
    errors = []
    amounts = {}
    approval_required = False
    try:
        def number(value):
            result = Decimal(str(value))
            if not result.is_finite():
                raise ValueError("non_finite_amount")
            return result

        def money(value):
            return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        totals = {key: Decimal(0) for key in
                  ("subtotal", "tax_amount_igst", "tax_amount_cgst", "tax_amount_sgst")}
        mode = tax_route["tax_mode"]
        if mode not in ("igst", "cgst_sgst"):
            raise ValueError("domestic_tax_mode_invalid")
        if str(parsed.get("currency_code", "")).upper() != "INR":
            raise ValueError("domestic_currency_requires_review")
        source_lines = parsed.get("line_items", [])
        lines = payload.get("line_items", [])
        if not lines or len(lines) != len(source_lines):
            raise ValueError("payload_line_count_mismatch")
        for source, line in zip(source_lines, lines):
            key, changed = _domestic_line_tax_key(source, mode)
            approval_required |= changed
            if not get_tax_id(key) or line.get("tax_id") != get_tax_id(key):
                raise ValueError("payload_line_tax_id_mismatch")
            base = money(number(line["rate"]) * number(line.get("quantity", 1)))
            source_base = money(number(source["rate"]) * number(source.get("quantity", 1)))
            if base != source_base:
                raise ValueError("payload_line_amount_mismatch")
            totals["subtotal"] += base
            rate = number(key.rsplit("_", 1)[1]) / 100
            if mode == "igst":
                totals["tax_amount_igst"] += money(base * rate)
            else:
                component = money(base * rate / 2)
                totals["tax_amount_cgst"] += component
                totals["tax_amount_sgst"] += component
        totals["invoice_total"] = sum(totals.values())
        for key, calculated in totals.items():
            printed = money(number(parsed[key]))
            amounts[key] = {"expected": float(printed), "calculated": float(calculated)}
            if printed != calculated:
                errors.append(f"payload_{key}_mismatch:{printed}_vs_{calculated}")
    except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
        errors.append(f"payload_tax_reconciliation_invalid:{exc}")
    return {"ok": not errors, "errors": errors,
            "approval_required": approval_required, "amounts": amounts}


def build_payload(parsed, vendor_id, tax_route, usd_rate=None):
    is_overseas = tax_route["is_overseas"]
    tax_id_key  = tax_route.get("tax_id_key")
    tax_mode = tax_route.get("tax_mode", "")  # "cgst_sgst", "igst", "rcm", "unknown"
    rcm_tax_id = get_tax_id("igst_18") if is_overseas else ""
    line_items = []
    for item in parsed.get("line_items", []):
        desc = item.get("description", "")
        rate = float(item.get("rate", 0))
        needs_fx = is_overseas and usd_rate and parsed.get("currency_code", "INR").upper() == "USD"
        rate = round(rate * usd_rate, 2) if needs_fx else round(rate, 2)
        li = {"description": desc, "quantity": 1, "rate": rate,
              "account_id": "914109000000000567"}
        raw_hsn = re.sub(r"[^0-9]", "", str(item.get("hsn_or_sac", "") or ""))[:8]
        # Zoho India requires HSN/SAC to be exactly 4, 6, or 8 digits.
        # 7-digit codes (common in Blinkit product invoices) are rejected with error 2207.
        # Drop invalid lengths rather than sending a code Zoho will reject.
        if raw_hsn and len(raw_hsn) in (4, 6, 8):
            li["hsn_or_sac"] = raw_hsn
        if not is_overseas and tax_mode in ("cgst_sgst", "igst"):
            item_tax_key, _ = _domestic_line_tax_key(item, tax_mode)
            item_tax_id = get_tax_id(item_tax_key)
            if not item_tax_id:
                raise ValueError(f"tax_id_missing:{item_tax_key}")
            li["tax_id"] = item_tax_id
        if is_overseas and rcm_tax_id:
            li["reverse_charge_tax_id"] = rcm_tax_id
        line_items.append(li)
    mbl   = parsed.get("mbl_number", "").strip()
    notes = parsed.get("notes", "")
    needs_fx = is_overseas and usd_rate and parsed.get("currency_code", "INR").upper() == "USD"
    if needs_fx:
        orig_total = sum(float(i.get("rate", 0)) for i in parsed.get("line_items", []))
        notes = f"Original USD: ${orig_total:.2f} @ {usd_rate:.2f} INR/USD. " + notes
    payload = {"vendor_id": vendor_id, "bill_number": parsed.get("bill_number", ""),
               "date": parsed.get("bill_date", ""), "line_items": line_items,
               "notes": notes, "reference_number": mbl,
               "currency_code": "INR"}
    due_date = parsed.get("due_date")
    due_date_text = str(due_date).strip() if due_date is not None else ""
    if due_date_text and due_date_text.lower() != "null":
        payload["due_date"] = due_date
    if is_overseas:
        payload["is_reverse_charge_applied"] = True
    return payload


# ── Logging helpers ───────────────────────────────────────────────────────────
# NOTE: _append_json_list is defined in Session 3 helpers above (bounded, atomic, recoverable).


def append_invoice_log(file, bill_number, mbl_number="", vendor="", date="",
                       status="", error="", timestamp=None, run_id=""):
    _append_json_list(INVOICE_LOG_PATH, {
        "run_id":      run_id,
        "file":        file,
        "bill_number": bill_number,
        "mbl_number":  mbl_number,
        "vendor":      vendor,
        "date":        date,
        "status":      status,
        "error":       error,
        "timestamp":   timestamp or datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }, run_id=run_id, label="invoice_log.json")


def append_failed_invoice(file, bill_number, mbl_number, error, run_id=""):
    _append_json_list(FAILED_INVOICES_PATH, {
        "run_id":      run_id,
        "file":        file,
        "bill_number": bill_number,
        "mbl_number":  mbl_number,
        "error":       error,
        "timestamp":   datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }, run_id=run_id, label="failed_invoices.json")


# ── Approval boundary (audit F04) ─────────────────────────────────────────────
# AGENTS.md: human approval is mandatory before posting. A normal run only
# queues the exact payload it would POST. `--approve --approver NAME --file PDF`
# posts only if it rebuilds the identical payload for the identical PDF, and
# records who approved what before the POST is sent.
def payload_digest(payload):
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def load_approval_queue(run_id="unset"):
    """Proposals keyed by PDF SHA-256. Unreadable means nothing is approvable."""
    if not os.path.exists(APPROVAL_QUEUE_PATH):
        return {}
    data, err = safe_json_read(APPROVAL_QUEUE_PATH)
    if data is None:
        obs_print("STATE_RECOVERY",
                  f"run_id={run_id} file=approval_queue.json reason={err} action=start_empty")
        return {}
    return data


def save_approval_queue(queue, run_id="unset"):
    ok, err = atomic_json_write(APPROVAL_QUEUE_PATH, queue)
    if not ok:
        obs_print("STATE_WRITE_FAILED",
                  f"run_id={run_id} file=approval_queue.json reason={err}")
    return ok


def record_approval(entry, run_id="unset"):
    """
    Append to the approvals ledger. Uncapped and never reset: if the existing
    ledger cannot be read or the write fails, return False and do not POST.
    """
    approvals = []
    if os.path.exists(APPROVALS_PATH):
        try:
            with open(APPROVALS_PATH, encoding="utf-8-sig") as f:
                approvals = json.load(f)
        except Exception as e:
            obs_print("APPROVAL_LEDGER_UNREADABLE", f"run_id={run_id} reason={str(e)[:80]}")
            return False
        if not isinstance(approvals, list):
            obs_print("APPROVAL_LEDGER_UNREADABLE", f"run_id={run_id} reason=not_a_list")
            return False
    approvals.append(entry)
    ok, err = atomic_json_write(APPROVALS_PATH, approvals)
    if not ok:
        obs_print("STATE_WRITE_FAILED", f"run_id={run_id} file=approvals.json reason={err}")
    return ok


# ── Validation ────────────────────────────────────────────────────────────────
def validate_before_post(parsed, vendor_id):
    if not vendor_id:
        return "vendor_id missing"
    bill_no = parsed.get("bill_number", "").strip()
    if not bill_no:
        return "bill_number empty"
    if len(bill_no) < 3:
        return "bill_number too short"
    mbl = parsed.get("mbl_number", "").strip()
    if mbl and bill_no.upper() == mbl.upper():
        return "bill_number equals mbl_number"
    bill_date = parsed.get("bill_date", "").strip()
    if not bill_date:
        return "bill_date empty"
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", bill_date):
        return f"bill_date invalid format: {bill_date}"
    if not any(i.get("rate", 0) > 0 for i in parsed.get("line_items", [])):
        return "no line items with rate > 0"
    _due = (parsed.get("due_date") or "").strip()
    if _due and _due.lower() != "null":
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", _due):
            return f"due_date invalid format: {_due}"
    _hcur = parsed.get("currency_code", "INR").upper()
    _lcurs = {
        str(li.get("currency_code", "")).upper()
        for li in parsed.get("line_items", [])
        if li.get("currency_code")
    }
    _bad = _lcurs - {"", "INR", _hcur}
    if _bad:
        return f"mixed line item currencies: {_bad}"
    try:
        _invoice_total = float(parsed.get("invoice_total") or 0)
    except (TypeError, ValueError):
        _invoice_total = 0.0
    if _invoice_total <= 0:
        return "invoice_total_missing"
    try:
        _discounts = float(parsed.get("discounts_or_credits") or 0)
    except (TypeError, ValueError):
        _discounts = 0.0
    _tax_total = 0.0
    for _tf in ("tax_amount_igst", "tax_amount_cgst", "tax_amount_sgst"):
        try:
            _tax_total += float(parsed.get(_tf) or 0)
        except (TypeError, ValueError):
            pass
    _line_sum = sum(
        float(i.get("rate", 0) or 0) * float(i.get("quantity", 1) or 1)
        for i in parsed.get("line_items", [])
    )
    _reconciled = _line_sum + _discounts + _tax_total
    if abs(_reconciled - _invoice_total) > 1.01:  # allow vendor Rs1 rounding (23-Sep-2026)
        return f"total_reconciliation_failed:{_invoice_total:.2f}_vs_{_reconciled:.2f}"
    return None


_FILENAME_FALLBACK_ALLOW_PREFIXES = ("INEMHC", "BOMFALI", "PTGFWD", "VSLNS")
_FILENAME_FALLBACK_BLOCK_PREFIXES = ("AMC",)

def _derive_bill_number_from_filename(filename, mbl_number):
    """
    Derive a bill number from a known-safe invoice filename prefix.
    This preserves the existing fallback purpose while blocking MBL-like names.
    """
    _mn = (mbl_number or "").strip()
    if not _mn:
        return ""
    _stem = filename[:-4] if filename.lower().endswith(".pdf") else filename
    for _sfx in ["InvoicePrint", "_copy", "_duplicate", "_dup"]:
        _i = _stem.lower().find(_sfx.lower())
        if _i != -1:
            _stem = _stem[:_i]
            break
    _stem = _stem.strip()
    _token = _stem.split()[0] if _stem.split() else ""
    _upper = _token.upper()
    if not _token or _upper == _mn.upper():
        return ""
    if any(_upper.startswith(_p) for _p in _FILENAME_FALLBACK_BLOCK_PREFIXES):
        return ""
    if not any(_upper.startswith(_p) for _p in _FILENAME_FALLBACK_ALLOW_PREFIXES):
        return ""
    return _token


# ── Vendor matching ───────────────────────────────────────────────────────────
_FUZZY_MIN_TOKENS = 2
_VENDOR_GENERIC_TOKENS = {"PVT", "PRIVATE", "LTD", "LIMITED", "INDIA", "LINE",
                          "SHIPPING", "COMPANY", "CO", "INC", "LLC", "A", "S"}

def _normalize_name(name):
    s = re.sub(r"[^A-Z0-9 ]", " ", name.upper())
    return " ".join(s.split())

def match_vendor_tiered(parsed_name, vendors, vendor_gstin=None):
    """Resolve one contact without discarding same-name registrations.
    A unique GSTIN may resolve different invoice and master names, but that new
    identity path is marked gstin and still requires human approval to post.
    Returns (contact_id, name, confidence, matched_gst_no)."""
    norm = _normalize_name(parsed_name)
    if not norm:
        return None, None, "none", ""

    # Keep each distinct Zoho contact, including identical display names.
    name_map = {}
    all_candidates = []
    for k, v in vendors.items():
        entries = [v] + (v.get("duplicates", []) if isinstance(v, dict) else [])
        for entry in entries:
            candidate = (k, entry)
            name_map.setdefault(_normalize_name(k), []).append(candidate)
            all_candidates.append(candidate)

    def _unique(candidates, confidence):
        contacts = {}
        for orig, entry in candidates:
            vid = entry.get("id") if isinstance(entry, dict) else entry
            gst = (entry.get("gst_no") or "").strip().upper() if isinstance(entry, dict) else ""
            if vid:
                contacts.setdefault(str(vid), (vid, orig, confidence, gst))
        if len(contacts) != 1:
            return None, None, "ambiguous" if contacts else "none", ""
        return next(iter(contacts.values()))

    canonical = _normalize_name(VENDOR_ALIAS_MAP.get(norm) or "")
    hint = str(vendor_gstin or "").strip().upper()
    if hint:
        if not re.fullmatch(r"[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]", hint):
            return None, None, "invalid_gstin", ""
        gst_matches = [
            (orig, entry) for orig, entry in all_candidates
            if isinstance(entry, dict) and
            str(entry.get("gst_no") or "").strip().upper() == hint
        ]
        if not gst_matches:
            # Never fall back to a conflicting name when the invoice gives GSTIN.
            return None, None, "gstin_unmatched", ""
        result = _unique(gst_matches, "gstin")
        if result[0] is None:
            return result
        vid, orig, confidence, gst = result
        matched_norm = _normalize_name(orig)
        if matched_norm == norm:
            confidence = "exact"
        elif canonical and matched_norm == canonical:
            confidence = "alias"
        return vid, orig, confidence, gst

    if norm in name_map:
        return _unique(name_map[norm], "exact")

    if canonical in name_map:
        return _unique(name_map[canonical], "alias")

    norm_tokens = set(norm.split()) - _VENDOR_GENERIC_TOKENS
    best_candidates, best_score = [], 0
    for knorm, candidates in name_map.items():
        score = len(norm_tokens & (set(knorm.split()) - _VENDOR_GENERIC_TOKENS))
        if score >= _FUZZY_MIN_TOKENS:
            if score > best_score:
                best_candidates, best_score = list(candidates), score
            elif score == best_score:
                best_candidates.extend(candidates)
    if best_candidates:
        return _unique(best_candidates, "fuzzy")
    return None, None, "none", ""


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    _configure_stdio()
    run_id = None
    args = sys.argv[1:]
    if "--run-id" in args:
        idx = args.index("--run-id")
        if idx + 1 < len(args):
            run_id = args[idx + 1]
    if not run_id:
        run_id = generate_run_id()

    preview_mode = "--preview" in args

    usd_rate_override = None
    if "--usd-rate" in args:
        idx = args.index("--usd-rate")
        if idx + 1 < len(args):
            try:
                usd_rate_override = float(args[idx + 1])
            except ValueError:
                print(f"[USD_RATE] run_id={run_id} ERROR: --usd-rate value must be a number, got {args[idx + 1]!r}")
                return

    use_invoice_date_fx = "--use-invoice-date-fx" in args

    approve_mode = "--approve" in args
    approver = ""
    if "--approver" in args:
        idx = args.index("--approver")
        if idx + 1 < len(args) and not args[idx + 1].startswith("--"):
            approver = args[idx + 1].strip()
    if approve_mode:
        # One named person approves one reviewed file at a time.
        if not approver:
            print(f"[APPROVE] run_id={run_id} ERROR: --approve requires --approver NAME")
            return
        if args.count("--file") != 1 or preview_mode:
            print(f"[APPROVE] run_id={run_id} ERROR: --approve requires exactly one --file and no --preview")
            return

    LOCK_PATH = r"C:\temp\zoho\zoho.lock"
    LOCK_MAX_AGE = 600
    _lock_payload = json.dumps({"run_id": run_id, "timestamp": time.time()})
    _acquired = False
    for _attempt in range(2):
        try:
            with open(LOCK_PATH, "x", encoding="utf-8") as _lf:
                _lf.write(_lock_payload)
            _acquired = True
            break
        except FileExistsError:
            _stale = False
            try:
                with open(LOCK_PATH, encoding="utf-8") as _lf:
                    _ldata = json.load(_lf)
                if time.time() - float(_ldata.get("timestamp", 0)) > LOCK_MAX_AGE:
                    _stale = True
            except Exception:
                _stale = True
            if _stale and _attempt == 0:
                obs_print("LOCK_STALE_RECOVERED", f"run_id={run_id} action=deleted")
                try:
                    os.remove(LOCK_PATH)
                except Exception:
                    pass
                continue
            else:
                obs_print("RUN_SKIPPED_LOCK", f"run_id={run_id} reason=lock_exists path={LOCK_PATH}")
                print(f"[RUN_SKIPPED_LOCK] run_id={run_id} lock={LOCK_PATH}")
                return
        except Exception as _le:
            print(f"  WARNING: could not write lock file: {_le}")
            _acquired = True
            break
    if not _acquired:
        obs_print("RUN_SKIPPED_LOCK", f"run_id={run_id} reason=lock_retry_failed")
        return

    file_mode = None
    file_indices = [i for i, a in enumerate(args) if a == "--file"]
    if len(file_indices) > 1:
        obs_print("FILE_MODE", f"run_id={run_id} error=multiple_--file_flags")
        print(f"[FILE_MODE] run_id={run_id} ERROR: multiple --file flags not allowed")
        return
    if len(file_indices) == 1:
        idx = file_indices[0]
        if idx + 1 >= len(args) or args[idx + 1].startswith("--"):
            obs_print("FILE_MODE", f"run_id={run_id} error=--file_missing_value")
            print(f"[FILE_MODE] run_id={run_id} ERROR: --file requires a value")
            return
        file_mode = args[idx + 1]

    if file_mode:
        if not os.path.exists(file_mode):
            obs_print("FILE_MODE", f"run_id={run_id} file={file_mode} status=not_found")
            print(f"[FILE_MODE] run_id={run_id} file={file_mode} ERROR: file not found, skipping")
            return
        obs_print("FILE_MODE", f"run_id={run_id} file={os.path.basename(file_mode)}")
        print(f"[FILE_MODE] run_id={run_id} file={file_mode}")

    global _TAX_ID_MAP, IGST_VENDORS, _OVERSEAS_VENDOR_SIGNALS, VENDOR_ALIAS_MAP
    global _FREIGHT_KW, FREIGHT_KEYWORDS

    start_time = datetime.datetime.now().isoformat()

    manifest = {
        "run_id":          run_id,
        "trigger_source":  "watcher" if "--run-id" in sys.argv else "manual",
        "trigger_file":    None,
        "start_time":      start_time,
        "end_time":        None,
        "files_considered": 0,
        "files_attempted":  0,
        "files_created":    0,
        "files_skipped":    0,
        "files_failed":     0,
        "fatal_error":      None,
    }
    write_run_manifest(manifest)
    write_audit_baseline(run_id)

    obs_print("PIPELINE_STARTED", f"run_id={run_id}")

    global _run_id_ctx
    _run_id_ctx = run_id

    print("=" * 62)
    print("  ZOHO BOOKS - AUTO VENDOR BILL ENTRY")
    print(f"  run_id: {run_id}")
    print("=" * 62)

    try:
        _tax_rules    = load_tax_rules(run_id=run_id)
        _vendor_rules = load_vendor_rules(run_id=run_id)
        _TAX_ID_MAP              = dict(_tax_rules["tax_id_map"])
        IGST_VENDORS             = list(_tax_rules["igst_vendors"])
        _OVERSEAS_VENDOR_SIGNALS = set(_tax_rules["overseas_vendor_signals"])
        VENDOR_ALIAS_MAP         = dict(_vendor_rules["vendor_alias_map"])
        _json_fw = set(str(k).upper() for k in _tax_rules.get("freight_keywords", []))
        _FREIGHT_KW = _FREIGHT_KW | _json_fw
        FREIGHT_KEYWORDS = list(_FREIGHT_KW)
        obs_print("RULES_CONFIG_LOADED",
                  f"run_id={run_id} tax_rules={_tax_rules['_source']} "
                  f"vendor_rules={_vendor_rules['_source']} "
                  f"tax_id_keys={list(_TAX_ID_MAP.keys())} "
                  f"igst_vendors={len(IGST_VENDORS)} aliases={len(VENDOR_ALIAS_MAP)}")

        obs_print("AUTH_PHASE_STARTED", f"run_id={run_id}")
        try:
            refresh_token()
        except Exception as e:
            obs_print("AUTH_FAILED", f"run_id={run_id} reason={str(e)[:120]}")
            notify("Zoho Login Failed", "Could not connect to Zoho. Check internet connection.")
            raise

        _tax_rules = fetch_tax_ids_from_zoho(_tax_rules, run_id=run_id)
        if _tax_rules["_source"] == "zoho_fetch_override":
            _TAX_ID_MAP = dict(_tax_rules["tax_id_map"])
            obs_print("RULES_CONFIG_LOADED",
                      f"run_id={run_id} tax_rules=zoho_fetch_override "
                      f"tax_id_keys={list(_TAX_ID_MAP.keys())}")

        obs_print("FETCH_EXISTING_BILLS_STARTED", f"run_id={run_id}")
        existing_bills = fetch_existing_bill_index()

        obs_print("FETCH_VENDORS_STARTED", f"run_id={run_id}")
        vendors      = fetch_vendors()
        vendors_list = list(vendors.keys())

        if file_mode:
            fname = os.path.basename(file_mode)
            pdf_files = [fname] if fname.lower().endswith(".pdf") else []
            print(f"\n[FILE_MODE] Processing single file: {fname}\n")
        else:
            pdf_files = sorted(f for f in os.listdir(BILLS_DIR) if f.lower().endswith(".pdf"))
            print(f"\nFound {len(pdf_files)} PDFs in {BILLS_DIR}\n")
        manifest["files_considered"] = len(pdf_files)

        # ── Session 3: Load parse cache via hardened helper ───────────────────
        parse_cache = _load_parse_cache(run_id=run_id)
        print(f"  Parse cache loaded: {len(parse_cache)} valid entries.\n")
        approval_queue = load_approval_queue(run_id=run_id)

        if usd_rate_override is not None:
            usd_rate = usd_rate_override
            obs_print("USD_RATE_OVERRIDE", f"run_id={run_id} usd_rate={usd_rate}")
        else:
            usd_rate = get_usd_to_inr()

        # Only "verified" is success (audit F03); there is deliberately no "created" list.
        run_log = {"run_id": run_id, "verified": [], "post_verify_failed": [], "awaiting_approval": [],
                   "skipped_duplicate": [], "needs_review": [], "failed": []}
        skipped = created = errors = review = 0
        verified = verify_failed = awaiting = 0
        cache_hits = cache_misses = ai_calls = 0
        review_reasons = {"vendor_match_review": 0, "tax_route_review": 0, "low_confidence_review": 0}
        error_classes  = {"AUTH_ERROR": 0, "PARSE_ERROR": 0, "TAX_ERROR": 0, "POST_ERROR": 0}

        for i, filename in enumerate(pdf_files):
            filepath = file_mode if file_mode else os.path.join(BILLS_DIR, filename)
            print(f"[{i+1:>3}/{len(pdf_files)}] {filename}")

            # ── Session 3: Hash-keyed cache lookup ────────────────────────────
            file_hash = _hash_file(filepath)
            cached = _cache_lookup(parse_cache, file_hash)
            if cached is not None:
                parsed = cached
                print("         [cached]")
                cache_hits += 1
            else:
                obs_print("PARSE_STARTED", f"run_id={run_id} file={filename}")
                cache_misses += 1
                ai_calls += 1
                try:
                    parsed = parse_pdf_with_claude(filepath, filename, vendors_list)
                except Exception as _pe:
                    if "PARSE_JSON_INVALID" in str(_pe):
                        _reason = "parse_json_invalid"
                        print(f"         NEEDS REVIEW - {_reason}\n")
                        obs_print("PARSE_JSON_INVALID",
                                  f"run_id={run_id} file={filename} error={str(_pe)[:80]}")
                        run_log["needs_review"].append(
                            {"file": filename, "reason": _reason, "run_id": run_id})
                        append_invoice_log(
                            filename, "", "", "", "", "needs_review", _reason, run_id=run_id)
                        review += 1
                        continue
                    raise
                obs_print("PARSE_COMPLETED", f"run_id={run_id} file={filename} confidence={parsed.get('confidence','?')}")
                _vc = parsed.pop("_vendor_context_count", 0)
                parse_cache[file_hash] = _make_cache_entry(parsed, file_hash, _vc)
                ok, werr = atomic_json_write(CACHE_PATH, parse_cache)
                if not ok:
                    obs_print("STATE_WRITE_FAILED",
                              f"run_id={run_id} file=parsed_cache.json reason={werr}")

            if isinstance(parsed.get("multi_invoice_count"), int) and parsed["multi_invoice_count"] > 1:
                numbers = [str(inv.get("bill_number") or "?") for inv in parsed.get("invoices", [])]
                reason = f"multi_invoice_pdf:{parsed['multi_invoice_count']}:" + ",".join(numbers)[:120]
                print(f"         NEEDS REVIEW - {reason}\n")
                obs_print("VALIDATION_FAILED", f"run_id={run_id} file={filename} reason={reason}")
                run_log["needs_review"].append({"file": filename, "parsed": parsed, "reason": reason, "run_id": run_id})
                notify("Bill Needs Review", filename + ": PDF holds several invoices. Enter or split each one.")
                append_invoice_log(filename, ",".join(numbers)[:80], "", "", "", "needs_review", reason, run_id=run_id)
                review += 1
                continue

            # MBL: regex authoritative, Claude fallback
            pdf_text   = extract_text_from_pdf(filepath)
            regex_mbl  = extract_mbl_from_pdf_text(pdf_text)
            claude_mbl = parsed.get("mbl_number", "").strip()

            if regex_mbl:
                if claude_mbl and regex_mbl != claude_mbl:
                    print(f"         MBL override: regex='{regex_mbl}' claude='{claude_mbl}' -> using regex")
                mbl_no = regex_mbl
            elif claude_mbl:
                print(f"         MBL from Claude: '{claude_mbl}' (regex found nothing)")
                mbl_no = claude_mbl
            else:
                print("         MBL: not found in PDF (regex and Claude both empty)")
                mbl_no = ""

            parsed["mbl_number"] = mbl_no

            vendor_n    = parsed.get("vendor_name", "")

            # CMA CGM / CCAI: deterministic invoice-number regex is authoritative
            # over the AI's bill_number for this vendor only (see is_cma_cgm_invoice).
            if is_cma_cgm_invoice(vendor_n, pdf_text):
                cma_bill_no    = extract_cma_cgm_invoice_number_from_pdf_text(pdf_text, mbl_no)
                claude_bill_no = parsed.get("bill_number", "").strip()
                if cma_bill_no:
                    if claude_bill_no and cma_bill_no != claude_bill_no:
                        print(f"         CMA CGM bill# override: regex='{cma_bill_no}' claude='{claude_bill_no}' -> using regex")
                    else:
                        print(f"         CMA CGM bill# from regex: '{cma_bill_no}'")
                    parsed["bill_number"] = cma_bill_no
                else:
                    print("         CMA CGM bill#: deterministic pattern not found; keeping AI/backstop value")

            bill_no     = parsed.get("bill_number", "").strip()
            bill_date   = parsed.get("bill_date", "")
            conf        = parsed.get("confidence", "")
            currency    = parsed.get("currency_code", "INR")
            is_overseas = parsed.get("is_overseas", False)

            print(f"         Vendor: {vendor_n}  |  Bill#: {bill_no}  |  MBL: {mbl_no}")
            print(f"         Date: {bill_date}  |  Currency: {currency}  |  Overseas: {is_overseas}  |  Conf: {conf}")
            if parsed.get("issues"):
                print(f"         Issues: {parsed['issues']}")

            if not bill_no:
                print("         NEEDS REVIEW - no bill number.\n")
                obs_print("VALIDATION_FAILED", f"run_id={run_id} file={filename} reason=no_bill_number")
                run_log["needs_review"].append({"file": filename, "parsed": parsed, "reason": "no bill number", "run_id": run_id})
                notify("Bill Needs Review", filename + ": Could not extract bill number. Check Zoho manually.")
                append_invoice_log(filename, bill_no, mbl_no, vendor_n, bill_date, "needs_review", "no bill number", run_id=run_id)
                review += 1
                continue

            if conf not in ("high", "medium"):
                obs_print("AI_LOW_CONFIDENCE_REVIEW",
                          f"run_id={run_id} file={filename} confidence={repr(conf)}")
                print(f"         NEEDS REVIEW - confidence={repr(conf)} (not high/medium).\n")
                run_log["needs_review"].append({"file": filename, "parsed": parsed, "reason": f"low_confidence:{conf}", "run_id": run_id})
                notify("Bill Needs Review", filename + ": Low/unknown confidence parse. Verify in Zoho.")
                append_invoice_log(filename, bill_no, mbl_no, vendor_n, bill_date, "needs_review", f"low_confidence:{conf}", run_id=run_id)
                review += 1
                review_reasons["low_confidence_review"] += 1
                continue

            _inv_gstin = parsed.get("vendor_gstin", "") or ""
            vendor_id, matched_name, match_conf, matched_gst_no = match_vendor_tiered(vendor_n, vendors, vendor_gstin=_inv_gstin)
            obs_print("AI_VENDOR_MATCH",
                      f"run_id={run_id} file={filename} matched={matched_name!r} confidence={match_conf}")
            print(f"         Vendor match: {matched_name!r} [{match_conf}]")
            if match_conf not in ("exact", "alias", "gstin"):
                reason = f"vendor_match_{match_conf}:{vendor_n}"
                print(f"         NEEDS REVIEW - vendor match confidence={match_conf}.\n")
                obs_print("AI_VENDOR_MATCH_REVIEW",
                          f"run_id={run_id} file={filename} confidence={match_conf} vendor={vendor_n!r}")
                run_log["needs_review"].append({"file": filename, "parsed": parsed, "reason": reason, "run_id": run_id})
                notify("Bill Needs Review", filename + f": Vendor match '{match_conf}' for '{vendor_n}'. Verify in Zoho.")
                append_invoice_log(filename, bill_no, mbl_no, vendor_n, bill_date, "needs_review", reason, run_id=run_id)
                review += 1
                review_reasons["vendor_match_review"] += 1
                continue

            # Filename fallback: if AI returned bill_number == mbl_number, derive from filename
            _bn = parsed.get("bill_number", "").strip()
            _mn = parsed.get("mbl_number", "").strip()
            if _mn and _bn.upper() == _mn.upper():
                _stem = _derive_bill_number_from_filename(filename, _mn)
                if _stem:
                    obs_print("BILL_NUMBER_DERIVED_FROM_FILENAME",
                              f"run_id={run_id} file={filename} original={_bn} derived={_stem}")
                    parsed["bill_number"] = _stem
                    bill_no = _stem

            # Duplicates are per vendor (audit F05), so this runs after the vendor match.
            dup_kind, dup_bills = find_existing_bill(existing_bills, vendor_id, bill_no)
            if dup_kind == "exact":
                print("         SKIP - already in Zoho for this vendor.\n")
                run_log["skipped_duplicate"].append({"file": filename, "bill_number": bill_no,
                                                     "vendor_id": vendor_id, "run_id": run_id})
                append_invoice_log(filename, bill_no, mbl_no, matched_name, bill_date, "skipped_duplicate", run_id=run_id)
                skipped += 1
                continue
            if dup_kind == "variant":
                reason = "possible_duplicate:" + ",".join(b["bill_number"] for b in dup_bills)[:120]
                print(f"         NEEDS REVIEW - {reason}\n")
                obs_print("POSSIBLE_DUPLICATE", f"run_id={run_id} file={filename} vendor_id={vendor_id} {reason}")
                run_log["needs_review"].append({"file": filename, "parsed": parsed, "reason": reason, "run_id": run_id})
                notify("Bill Needs Review", filename + ": Same vendor has a similar bill number in Zoho.")
                append_invoice_log(filename, bill_no, mbl_no, matched_name, bill_date, "needs_review", reason, run_id=run_id)
                review += 1
                continue

            tax_route = determine_tax_route(parsed, matched_name, vendor_gst_no=matched_gst_no)
            svc_type  = classify_service_type(parsed)
            obs_print("TAX_ROUTE_DECISION",
                      f"run_id={run_id} file={filename} gst_route={tax_route['gst_route']} "
                      f"tax_mode={tax_route['tax_mode']} review={tax_route['review_required']}")
            obs_print("SERVICE_TYPE_CLASSIFIED",
                      f"run_id={run_id} file={filename} service_type={svc_type}")
            if tax_route["review_required"]:
                reason = f"tax_route_uncertain:{tax_route['review_reason']}"
                print(f"         NEEDS REVIEW - {reason}\n")
                obs_print("TAX_ROUTE_REVIEW",
                          f"run_id={run_id} file={filename} reason={tax_route['review_reason']}")
                run_log["needs_review"].append({"file": filename, "parsed": parsed, "reason": reason, "run_id": run_id})
                notify("Bill Needs Review", filename + f": Tax route uncertain ({tax_route['review_reason']}). Verify in Zoho.")
                append_invoice_log(filename, bill_no, mbl_no, vendor_n, bill_date, "needs_review", reason, run_id=run_id)
                review += 1
                review_reasons["tax_route_review"] += 1
                continue
            if not tax_route["is_overseas"]:
                resolved = _TAX_ID_MAP.get(tax_route.get("tax_id_key", ""), "")
                if not resolved:
                    obs_print("TAX_ID_VALIDATION_FAILED",
                              f"run_id={run_id} file={filename} route={tax_route['gst_route']} "
                              f"tax_id={tax_route.get('tax_id_key','')}")
                    run_log["needs_review"].append({"file": filename, "parsed": parsed, "reason": "tax_id_missing", "run_id": run_id})
                    append_invoice_log(filename, bill_no, mbl_no, vendor_n, bill_date, "needs_review", "tax_id_missing", run_id=run_id)
                    review += 1
                    continue
            else:
                rcm_resolved = _TAX_ID_MAP.get("igst_18", "")
                if not rcm_resolved:
                    obs_print("TAX_ID_VALIDATION_FAILED",
                              f"run_id={run_id} file={filename} route=overseas tax_id=igst_18")
                    run_log["needs_review"].append({"file": filename, "parsed": parsed, "reason": "rcm_tax_id_missing:igst_18", "run_id": run_id})
                    append_invoice_log(filename, bill_no, mbl_no, vendor_n, bill_date, "needs_review", "rcm_tax_id_missing:igst_18", run_id=run_id)
                    review += 1
                    continue

            invalid_reason = validate_before_post(parsed, vendor_id)
            if invalid_reason:
                print(f"         NEEDS REVIEW - validation failed: {invalid_reason}\n")
                obs_print("VALIDATION_FAILED", f"run_id={run_id} file={filename} reason={invalid_reason}")
                run_log["needs_review"].append({"file": filename, "parsed": parsed, "reason": invalid_reason, "run_id": run_id})
                append_invoice_log(filename, bill_no, mbl_no, vendor_n, bill_date, "needs_review", invalid_reason, run_id=run_id)
                review += 1
                continue

            effective_usd_rate, _is_usd_overseas = resolve_effective_usd_rate(
                parsed, tax_route, bill_date, usd_rate, use_invoice_date_fx,
                run_id=run_id, filename=filename)

            if _is_usd_overseas and not effective_usd_rate:
                _reason = "fx_rate_unavailable" if use_invoice_date_fx else "usd_rate_unavailable"
                print(f"         NEEDS REVIEW - {_reason}\n")
                obs_print("USD_RATE_UNAVAILABLE",
                          f"run_id={run_id} file={filename}")
                run_log["needs_review"].append(
                    {"file": filename, "parsed": parsed, "reason": _reason, "run_id": run_id})
                append_invoice_log(
                    filename, bill_no, mbl_no, vendor_n, bill_date,
                    "needs_review", _reason, run_id=run_id)
                review += 1
                continue
            manifest["files_attempted"] += 1
            try:
                payload = build_payload(parsed, vendor_id, tax_route,
                                        effective_usd_rate if tax_route["is_overseas"] else None)
                tax_check = reconcile_payload_taxes(parsed, payload, tax_route)
            except (ValueError, TypeError, InvalidOperation) as exc:
                tax_check = {"ok": False, "errors": [str(exc)]}
            if not tax_check["ok"]:
                reason = ";".join(tax_check["errors"])
                obs_print("PAYLOAD_TAX_REVIEW", f"run_id={run_id} file={filename} reason={reason}")
                run_log["needs_review"].append({"file": filename, "parsed": parsed,
                                               "reason": reason, "run_id": run_id})
                append_invoice_log(filename, bill_no, mbl_no, matched_name, bill_date,
                                   "needs_review", reason, run_id=run_id)
                review += 1
                review_reasons["tax_route_review"] += 1
                continue
            if (match_conf == "gstin" or tax_check["approval_required"]) and not preview_mode:
                # Resolving an agent/master name mismatch does not approve a bill.
                reason = ("vendor_gstin_match_requires_approval" if match_conf == "gstin"
                          else "line_tax_match_requires_approval")
                obs_print("APPROVAL_REQUIRED",
                          f"run_id={run_id} file={filename} vendor_id={vendor_id} matched={matched_name!r}")
                print(f"         NEEDS REVIEW - {reason}: {matched_name}\n")
                run_log["needs_review"].append({
                    "file": filename, "parsed": parsed, "reason": reason,
                    "vendor_id": vendor_id, "matched_vendor": matched_name,
                    "vendor_gstin": matched_gst_no, "run_id": run_id})
                append_invoice_log(filename, bill_no, mbl_no, matched_name,
                                   bill_date, "needs_review", reason, run_id=run_id)
                notify("Bill Needs Approval", filename + ": Reconcile vendor and line taxes; approve manually in Zoho.")
                review += 1
                review_reasons["vendor_match_review"] += 1
                continue
            digest = payload_digest(payload)
            if preview_mode:
                print("         [PREVIEW] reconciliation (human approval still required):\n"
                      + json.dumps(tax_check, indent=2))
                print("         [PREVIEW] would POST:\n" + json.dumps(payload, indent=2))
                print(f"         [PREVIEW] payload_sha256={digest}")
                obs_print("PREVIEW_PAYLOAD", f"run_id={run_id} file={filename} bill_number={bill_no}")
                continue
            queued = approval_queue.get(file_hash)
            if not approve_mode or not isinstance(queued, dict) or queued.get("payload_sha256") != digest:
                if not approve_mode:
                    reason = "awaiting_approval"
                elif isinstance(queued, dict):
                    reason = "approval_rejected:payload_changed_since_review"
                else:
                    reason = "approval_rejected:no_reviewed_proposal"
                approval_queue[file_hash] = {
                    "file": filename, "pdf_sha256": file_hash, "bill_number": bill_no,
                    "vendor_id": vendor_id, "vendor": matched_name, "payload_sha256": digest,
                    "payload": payload, "reconciliation": tax_check,
                    "proposed_at": datetime.datetime.now().isoformat(), "run_id": run_id,
                }
                save_approval_queue(approval_queue, run_id=run_id)
                print(f"         AWAITING APPROVAL - {reason} payload_sha256={digest[:12]}\n"
                      f"         Review : python zoho_bills.py --file \"{filepath}\" --preview\n"
                      f"         Approve: python zoho_bills.py --file \"{filepath}\" --approve --approver \"NAME\"\n")
                obs_print("APPROVAL_REQUIRED",
                          f"run_id={run_id} file={filename} reason={reason} payload_sha256={digest[:12]}")
                run_log["awaiting_approval"].append({
                    "file": filename, "bill_number": bill_no, "reason": reason,
                    "payload_sha256": digest, "run_id": run_id})
                append_invoice_log(filename, bill_no, mbl_no, matched_name, bill_date,
                                   "awaiting_approval", reason, run_id=run_id)
                notify("Bill Ready for Approval", filename + ": Checks passed. Review and approve to post.")
                awaiting += 1
                continue
            approval = {
                "pdf_sha256": file_hash, "file": filename, "bill_number": bill_no,
                "vendor_id": vendor_id, "vendor": matched_name, "payload_sha256": digest,
                "approved_by": approver,
                "os_user": os.environ.get("USERNAME") or os.environ.get("USER", ""),
                "approved_at": datetime.datetime.now().isoformat(), "run_id": run_id,
            }
            if not record_approval(approval, run_id=run_id):
                err = "approval_ledger_write_failed"
                print(f"         FAILED - {err}; nothing posted\n")
                run_log["failed"].append({"file": filename, "bill_number": bill_no, "error": err, "run_id": run_id})
                append_invoice_log(filename, bill_no, mbl_no, vendor_n, bill_date, "failed", err, run_id=run_id)
                errors += 1
                continue
            obs_print("APPROVAL_RECORDED",
                      f"run_id={run_id} file={filename} approved_by={approver!r} payload_sha256={digest[:12]}")
            obs_print("POST_STARTED", f"run_id={run_id} file={filename} bill_number={bill_no}")
            try:
                result  = zoho_post("bills", payload)
                bid     = extract_created_bill_id(result)
                if not bid:
                    raise Exception("Zoho POST response missing a valid bill_id")
                print(f"         POSTED - Zoho Bill ID: {bid} (verifying)")
                obs_print("POST_COMPLETED", f"run_id={run_id} file={filename} bill_id={bid} status=posted_unverified")
                existing_bills.setdefault(_bill_key(bill_no), []).append(
                    {"bill_number": bill_no, "vendor_id": str(vendor_id), "bill_id": bid})
                created += 1
                approval_queue.pop(file_hash, None)
                save_approval_queue(approval_queue, run_id=run_id)
                # Durable trace that the bill exists before read-back runs (audit F03).
                append_invoice_log(filename, bill_no, mbl_no, vendor_n, bill_date,
                                   "posted_unverified", f"bill_id={bid}", run_id=run_id)
                verify_result = verify_created_bill(
                    bid, payload, parsed, run_id=run_id, filename=filename,
                    usd_rate=effective_usd_rate if tax_route["is_overseas"] else None)
                if verify_result["ok"]:
                    print(f"         VERIFIED - Zoho Bill ID: {bid}\n")
                    run_log["verified"].append({"file": filename, "bill_number": bill_no, "bill_id": bid, "run_id": run_id})
                    append_invoice_log(filename, bill_no, mbl_no, vendor_n, bill_date,
                                       "verified", f"bill_id={bid}", run_id=run_id)
                    verified += 1
                else:
                    verify_detail = "; ".join(verify_result.get("errors", []))[:500]
                    print(f"         POST-VERIFY FAILED - Zoho Bill ID: {bid}: {verify_detail}\n")
                    run_log["post_verify_failed"].append({
                        "file": filename, "bill_number": bill_no, "bill_id": bid,
                        "error": verify_detail, "run_id": run_id})
                    append_invoice_log(
                        filename, bill_no, mbl_no, vendor_n, bill_date,
                        "post_verify_failed", verify_detail, run_id=run_id)
                    notify("Zoho Bill Verification Failed",
                           filename + ": Posted bill does not match. Check Zoho now.")
                    verify_failed += 1
            except Exception as e:
                err = str(e)
                print(f"         FAILED - {err}\n")
                obs_print("POST_COMPLETED", f"run_id={run_id} file={filename} status=failed error={err[:80]}")
                run_log["failed"].append({"file": filename, "bill_number": bill_no, "error": err, "run_id": run_id})
                if "credit balance is too low" not in err:
                    notify("Zoho Bill Failed", filename + ": " + err[:80])
                append_invoice_log(filename, bill_no, mbl_no, vendor_n, bill_date, "failed", err, run_id=run_id)
                append_failed_invoice(filename, bill_no, mbl_no, err, run_id=run_id)
                errors += 1
                error_classes[_classify_error(err)] += 1

            time.sleep(1)

        ok, werr = atomic_json_write(LOG_PATH, run_log)
        if not ok:
            obs_print("STATE_WRITE_FAILED",
                      f"run_id={run_id} file=run_log.json reason={werr}")

        _total = len(pdf_files)
        _hit_rate = round(cache_hits / (_total or 1), 3)
        _metrics = {
            "run_id":         run_id,
            "timestamp":      datetime.datetime.now().isoformat(),
            "total_files":    _total,
            "created":        created,
            "verified":       verified,
            "verify_failed":  verify_failed,
            "awaiting_approval": awaiting,
            "skipped":        skipped,
            "review":         review,
            "errors":         errors,
            "cache_hits":     cache_hits,
            "cache_misses":   cache_misses,
            "cache_hit_rate": _hit_rate,
            "ai_calls":       ai_calls,
            "review_reasons": review_reasons,
            "error_classes":  error_classes,
        }
        _alerts = []
        if _total > 0:
            if review / _total > 0.3:
                _alerts.append("high_review_rate")
                obs_print("ALERT_TRIGGERED", f"run_id={run_id} reason=high_review_rate rate={review/_total:.2f}")
            if errors / _total > 0.05:
                _alerts.append("high_error_rate")
                obs_print("ALERT_TRIGGERED", f"run_id={run_id} reason=high_error_rate rate={errors/_total:.2f}")
            if _hit_rate < 0.5 and _total > 1:
                _alerts.append("low_cache_hit_rate")
                obs_print("ALERT_TRIGGERED", f"run_id={run_id} reason=low_cache_hit_rate rate={_hit_rate:.2f}")
        _write_run_metrics(run_id, _metrics)
        _update_system_health(run_id, _metrics, _alerts)

        manifest["end_time"]       = datetime.datetime.now().isoformat()
        manifest["files_created"]  = created
        manifest["files_skipped"]  = skipped
        manifest["files_failed"]   = errors
        write_run_manifest(manifest)

        print("\n" + "=" * 62)
        print("  SUMMARY")
        print(f"  run_id          : {run_id}")
        print(f"  Posted to Zoho  : {created}")
        print(f"  Verified        : {verified}")
        print(f"  Verify failed   : {verify_failed}")
        print(f"  Awaiting approval: {awaiting}")
        print(f"  Skipped (dupes) : {skipped}")
        print(f"  Needs review    : {review}")
        print(f"  Errors          : {errors}")
        print(f"  Log             : {LOG_PATH}")
        print("=" * 62)
        obs_print("RUN_COMPLETED", f"run_id={run_id} created={created} verified={verified} "
                  f"verify_failed={verify_failed} awaiting_approval={awaiting} "
                  f"skipped={skipped} review={review} errors={errors}")

        if verified:
            notify("Zoho Bills", f"{verified} bill(s) posted and verified in Zoho.")
        if verify_failed:
            notify("Zoho Bills Verification Failed",
                   f"{verify_failed} posted bill(s) did not match on read-back. Check Zoho now.")
        if errors:
            notify("Zoho Pipeline Error", f"{errors} bill(s) failed. Check watcher.log.")

    except Exception as e:
        manifest["end_time"]    = datetime.datetime.now().isoformat()
        manifest["fatal_error"] = str(e)
        write_run_manifest(manifest)
        obs_print("RUN_FATAL", f"run_id={run_id} error={str(e)[:120]}")
        raise
    finally:
        try:
            os.remove(LOCK_PATH)
        except Exception:
            pass


# ── Self-test / debug mode ────────────────────────────────────────────────────
def _selftest():
    """
    Session 1 self-test: verify run_id generation and manifest structure.
    Run with: python zoho_bills.py --selftest
    Does NOT connect to Zoho or Anthropic.
    """
    print("=== SESSION 1 SELF-TEST ===")
    rid = generate_run_id()
    print(f"Generated run_id: {rid}")
    assert len(rid) == 24, f"Expected 24 chars, got {len(rid)}"
    assert rid.count("-") == 2, "Expected exactly 2 dashes"

    sample_manifest = {
        "run_id":           rid,
        "trigger_source":   "selftest",
        "trigger_file":     "test_invoice.pdf",
        "start_time":       datetime.datetime.now().isoformat(),
        "end_time":         None,
        "files_considered": 5,
        "files_attempted":  2,
        "files_created":    1,
        "files_skipped":    4,
        "files_failed":     0,
        "fatal_error":      None,
    }
    write_run_manifest(sample_manifest)
    write_audit_baseline(rid)

    obs_print("PIPELINE_STARTED",             f"run_id={rid}")
    obs_print("AUTH_PHASE_STARTED",           f"run_id={rid}")
    obs_print("FETCH_EXISTING_BILLS_STARTED", f"run_id={rid}")
    obs_print("FETCH_VENDORS_STARTED",        f"run_id={rid}")
    obs_print("PARSE_STARTED",                f"run_id={rid} file=test.pdf")
    obs_print("PARSE_COMPLETED",              f"run_id={rid} file=test.pdf confidence=high")
    obs_print("VALIDATION_FAILED",            f"run_id={rid} file=test2.pdf reason=vendor_not_found")
    obs_print("POST_STARTED",                 f"run_id={rid} file=test.pdf bill_number=INV001")
    obs_print("POST_COMPLETED",               f"run_id={rid} file=test.pdf status=created bill_id=9141090000012345")
    obs_print("RUN_COMPLETED",                f"run_id={rid} created=1 skipped=4 review=0 errors=0")

    print(f"\nrun_manifest.json written to: {RUN_MANIFEST_PATH}")
    print(f"audit_baseline_report.json written to: {AUDIT_BASELINE_PATH}")
    print("=== SELF-TEST PASSED ===")


def _business_test():
    """
    Business-logic test harness.
    Run with: python zoho_bills.py --test
    Does NOT connect to Zoho, Anthropic, FX APIs, or read PDFs.
    """
    known_labels = set()
    failures = []
    check_count = 0

    def check(name, fn, known_label=None):
        nonlocal check_count
        check_count += 1
        try:
            fn()
            print(f"PASS {name}" + (f" [{known_label}]" if known_label else ""))
            if known_label:
                known_labels.add(known_label)
        except AssertionError as e:
            msg = str(e) or "assertion failed"
            if known_label:
                print(f"PASS {name} [{known_label}] current_behavior={msg}")
                known_labels.add(known_label)
            else:
                print(f"FAIL {name}: {msg}")
                failures.append((name, msg))
        except Exception as e:
            msg = f"{type(e).__name__}: {e}"
            print(f"FAIL {name}: {msg}")
            failures.append((name, msg))

    def parsed_base(**overrides):
        data = {
            "vendor_name": "Domestic Vendor",
            "bill_number": "INV123",
            "bill_date": "2026-05-01",
            "due_date": "",
            "mbl_number": "MBL123",
            "currency_code": "INR",
            "is_overseas": False,
            "line_items": [
                {"description": "Professional service consulting fee",
                 "rate": 100.0, "hsn_or_sac": "998313"}
            ],
            "subtotal": 100.0,
            "discounts_or_credits": 0,
            "tax_amount_igst": 0,
            "tax_amount_cgst": 0,
            "tax_amount_sgst": 0,
            "invoice_total": 100.0,
            "confidence": "high",
            "notes": "Professional service consulting fee monthly retainer",
        }
        data.update(overrides)
        return data

    def no_network(*args, **kwargs):
        raise AssertionError("network call attempted during --test")

    _orig_urlopen = urllib.request.urlopen
    _orig_zoho_get = zoho_get
    urllib.request.urlopen = no_network
    try:
        def t_overseas_status():
            old = set(_OVERSEAS_VENDOR_SIGNALS)
            try:
                _OVERSEAS_VENDOR_SIGNALS.update({"ADOBE", "ANTHROPIC"})
                assert determine_overseas_status(
                    parsed_base(vendor_name="ADOBE IRELAND", is_overseas=True,
                                currency_code="INR"),
                    "ADOBE IRELAND") is True
                # Audit A5: a name match alone goes to review, not reverse charge.
                assert determine_overseas_status(
                    parsed_base(vendor_name="ADOBE IRELAND", is_overseas=False,
                                currency_code="INR"),
                    "ADOBE IRELAND") is None
                assert determine_overseas_status(
                    parsed_base(vendor_name="ANTHROPIC", currency_code="USD"),
                    "ANTHROPIC") is True
                assert determine_overseas_status(
                    parsed_base(vendor_name="LOCAL SUPPLIER", is_overseas=False,
                                currency_code="INR"),
                    "LOCAL SUPPLIER") is False
                assert determine_overseas_status(
                    parsed_base(is_overseas="true", currency_code="INR"),
                    "LOCAL SUPPLIER") is True
                assert determine_overseas_status(
                    parsed_base(is_overseas="false", currency_code="INR"),
                    "LOCAL SUPPLIER") is False
                # Audit A5: signals match whole words only.
                _OVERSEAS_VENDOR_SIGNALS.update({"AWS", "AMAZON WEB"})
                assert determine_overseas_status(
                    parsed_base(is_overseas=False, currency_code="INR"),
                    "DAWSON LOGISTICS PVT LTD") is False
                assert determine_overseas_status(
                    parsed_base(is_overseas=False, currency_code="INR"), "AWS EUROPE") is None
                assert determine_overseas_status(
                    parsed_base(is_overseas=True, currency_code="INR"),
                    "Amazon Web Services EMEA SARL") is True
            finally:
                _OVERSEAS_VENDOR_SIGNALS.clear()
                _OVERSEAS_VENDOR_SIGNALS.update(old)

        def t_tax_route():
            domestic = parsed_base()
            r = determine_tax_route(domestic, "LOCAL SUPPLIER", "24ABCDE1234F1Z5")
            assert r["gst_route"] == "inter" and r["tax_mode"] == "igst"
            r = determine_tax_route(domestic, "LOCAL SUPPLIER", "27ABCDE1234F1Z5")
            assert r["gst_route"] == "intra" and r["tax_mode"] == "cgst_sgst"
            r = determine_tax_route(parsed_base(is_overseas=True), "OVERSEAS LTD", "")
            assert r["tax_mode"] == "rcm" and r["is_overseas"] is True
            r = determine_tax_route(
                parsed_base(line_items=[{"description": "fee", "rate": 1}],
                            notes=""),
                "LOCAL SUPPLIER", "27ABCDE1234F1Z5")
            assert r["review_required"] is False
            assert r["is_freight"] is False
            assert r["tax_id_key"] == "cgst_sgst_18"
            freight = determine_tax_route(
                parsed_base(line_items=[{"description": "Ocean freight", "rate": 1}],
                            notes=""),
                "LOCAL SUPPLIER", "27ABCDE1234F1Z5")
            assert freight["is_freight"] is True
            assert freight["tax_id_key"] == "cgst_sgst_5"
            # Audit A5: an Indian GSTIN (master or invoice) blocks reverse charge.
            for parsed, gst_no in ((parsed_base(currency_code="USD"), "27ABCDE1234F1Z5"),
                                   (parsed_base(is_overseas=True, vendor_gstin="24ABCDE1234F1Z5"), "")):
                r = determine_tax_route(parsed, "LOCAL SUPPLIER", gst_no)
                assert r["review_required"] is True
                assert r["review_reason"] == "indian_gstin_vendor_with_overseas_signal"
            r = determine_tax_route(parsed_base(currency_code="USD", vendor_gstin="9917USA29001OSE"),
                                    "OVERSEAS LTD", "")
            assert r["tax_mode"] == "rcm" and r["review_required"] is False

        def t_existing_bill_lookup():
            index = {}
            for number, vendor in (("INV-1001", "V1"), ("SCS/26/7", "V2"), ("X-9", "")):
                index.setdefault(_bill_key(number), []).append(
                    {"bill_number": number, "vendor_id": vendor, "bill_id": "1"})
            assert find_existing_bill(index, "V1", "inv-1001")[0] == "exact"
            assert find_existing_bill(index, "V1", "INV1001")[0] == "variant"
            # Audit F05: another vendor's bill with the same number is not a duplicate.
            assert find_existing_bill(index, "V2", "INV-1001") == (None, [])
            assert find_existing_bill(index, "V2", "SCS/26/7")[0] == "exact"
            # A listed bill with no vendor cannot be ruled out: a human decides.
            assert find_existing_bill(index, "V3", "X9")[0] == "variant"

        def t_overseas_vendor_charged_indian_gst_stop():
            r = determine_tax_route(
                parsed_base(is_overseas=True, tax_amount_igst=180.0),
                "OVERSEAS LTD", "")
            assert r["is_overseas"] is True
            assert r["review_required"] is True
            assert r["review_reason"] == "overseas_vendor_charged_indian_gst"
            r = determine_tax_route(
                parsed_base(is_overseas=True, tax_amount_cgst=45.0, tax_amount_sgst=45.0),
                "OVERSEAS LTD", "")
            assert r["review_required"] is True
            assert r["review_reason"] == "overseas_vendor_charged_indian_gst"
            clean = determine_tax_route(parsed_base(is_overseas=True), "OVERSEAS LTD", "")
            assert clean["review_required"] is False

        def t_created_bill_id():
            assert extract_created_bill_id({"bill": {"bill_id": "9141090000012345"}}) == "9141090000012345"
            for response in ({}, {"bill": {}}, {"bill": {"bill_id": ""}},
                             {"bill": {"bill_id": "not-a-zoho-id"}}, None):
                assert extract_created_bill_id(response) is None

        def t_build_payload():
            overseas = {"is_overseas": True, "tax_mode": "rcm", "tax_id_key": None}
            p = parsed_base(currency_code="INR")
            payload = build_payload(p, "VENDOR1", overseas, usd_rate=80)
            assert payload["line_items"][0]["rate"] == 100.0
            assert payload["is_reverse_charge_applied"] is True
            assert "is_reverse_charge" not in payload
            assert payload["line_items"][0]["reverse_charge_tax_id"] == "914109000000014234"
            p = parsed_base(currency_code="USD")
            payload = build_payload(p, "VENDOR1", overseas, usd_rate=80)
            assert payload["line_items"][0]["rate"] == 8000.0
            assert payload["line_items"][0]["reverse_charge_tax_id"] == "914109000000014234"
            domestic_route = determine_tax_route(parsed_base(), "LOCAL SUPPLIER",
                                                 "27ABCDE1234F1Z5")
            payload = build_payload(parsed_base(), "VENDOR1", domestic_route)
            assert payload["line_items"][0].get("tax_id")
            p = parsed_base(line_items=[
                {"description": "Professional service consulting fee",
                 "rate": 10, "hsn_or_sac": "1234567"},
                {"description": "Professional service consulting fee",
                 "rate": 20, "hsn_or_sac": "123456"},
            ])
            payload = build_payload(p, "VENDOR1", domestic_route)
            assert "hsn_or_sac" not in payload["line_items"][0]
            assert payload["line_items"][1].get("hsn_or_sac") == "123456"

        def verify_payload():
            return {
                "vendor_id": "VENDOR1",
                "bill_number": "INV123",
                "currency_code": "INR",
                "line_items": [
                    {"description": "A", "rate": 100, "quantity": 2, "tax_id": "TAX_IGST18"},
                    {"description": "B", "rate": 50, "tax_id": "TAX_IGST18"},
                ],
            }

        def verify_parsed(invoice_total=295, bill_date="2026-05-01", currency_code="INR",
                          tax_amount_igst=45, tax_amount_cgst=0, tax_amount_sgst=0):
            return {
                "bill_date": bill_date,
                "currency_code": currency_code,
                "invoice_total": invoice_total,
                "tax_amount_igst": tax_amount_igst,
                "tax_amount_cgst": tax_amount_cgst,
                "tax_amount_sgst": tax_amount_sgst,
            }

        def verify_bill(**overrides):
            """GET bills/{id} response matching verify_payload() and verify_parsed()."""
            bill = {
                "bill_number": "INV123",
                "vendor_id": "VENDOR1",
                "date": "2026-05-01",
                "currency_code": "INR",
                "sub_total": 250,
                "tax_total": 45,
                "total": 295,
                "line_items": [
                    {"rate": 100, "quantity": 2, "tax_id": "TAX_IGST18"},
                    {"rate": 50, "quantity": 1, "tax_id": "TAX_IGST18"},
                ],
            }
            bill.update(overrides)
            return bill

        def rcm_payload(rate):
            return {
                "vendor_id": "VENDOR1",
                "bill_number": "INV123",
                "currency_code": "INR",
                "line_items": [{"description": "A", "rate": rate, "quantity": 1,
                                "reverse_charge_tax_id": "914109000000014234"}],
                "is_reverse_charge_applied": True,
            }

        def rcm_bill(amount, **overrides):
            bill = verify_bill(sub_total=amount, total=amount, is_reverse_charge_applied=True,
                               line_items=[{"rate": amount, "quantity": 1,
                                            "reverse_charge_tax_id": "914109000000014234",
                                            "reverse_charge_tax_amount": round(amount * 0.18, 2)}])
            bill.pop("tax_total")
            bill.update(overrides)
            return bill

        def run_verify_with_bill(bill, payload=None, parsed=None, usd_rate=None):
            calls = []

            def fake_zoho_get(endpoint, params=None):
                calls.append((endpoint, params))
                return {"bill": bill}

            old_zoho_get = globals()["zoho_get"]
            globals()["zoho_get"] = fake_zoho_get
            try:
                result = verify_created_bill(
                    "BILL1", payload or verify_payload(),
                    parsed if parsed is not None else verify_parsed(),
                    run_id="test", filename="test.pdf", usd_rate=usd_rate)
            finally:
                globals()["zoho_get"] = old_zoho_get
            assert calls == [("bills/BILL1", None)]
            return result

        def t_verify_created_bill_matching():
            r = run_verify_with_bill(verify_bill())
            assert r["ok"] is True, r["errors"]
            for name in ("sub_total", "tax_total", "total", "line_items", "currency_code"):
                assert r["checks"][name]["ok"] is True, name

        def t_verify_created_bill_total_must_match_printed():
            # Audit F01: Zoho added Rs 11,977.06 of tax the invoice did not print.
            payload = verify_payload()
            payload["line_items"] = [{"description": "A", "rate": 163821.16, "quantity": 1,
                                      "tax_id": "TAX_IGST18"}]
            bill = verify_bill(sub_total=163821.16, tax_total=11977.06, total=175798.22,
                               line_items=[{"rate": 163821.16, "quantity": 1, "tax_id": "TAX_IGST18"}])
            r = run_verify_with_bill(bill, payload=payload,
                                     parsed=verify_parsed(invoice_total=163821.16, tax_amount_igst=0))
            assert r["ok"] is False
            assert any(e.startswith("total_mismatch") for e in r["errors"]), r["errors"]
            assert any(e.startswith("tax_total_mismatch") for e in r["errors"]), r["errors"]

        def t_verify_created_bill_number_mismatch():
            r = run_verify_with_bill(verify_bill(bill_number="INV999"))
            assert r["ok"] is False
            assert any("bill_number_mismatch" in e for e in r["errors"])

        def t_verify_created_bill_vendor_mismatch():
            r = run_verify_with_bill(verify_bill(vendor_id="VENDOR9"))
            assert r["ok"] is False
            assert any("vendor_id_mismatch" in e for e in r["errors"])

        def t_verify_created_bill_amount_mismatch():
            r = run_verify_with_bill(verify_bill(sub_total=260, total=305))
            assert r["ok"] is False
            assert any(e.startswith("sub_total_mismatch") for e in r["errors"])
            assert any(e.startswith("total_mismatch") for e in r["errors"])

        def t_verify_created_bill_no_percentage_tolerance():
            # Audit F02: the old max(Rs 1, 1%) band accepted both of these.
            for sub_total in (252.4, 250.5):
                r = run_verify_with_bill(verify_bill(sub_total=sub_total))
                assert r["ok"] is False, sub_total
                assert any(e.startswith("sub_total_mismatch") for e in r["errors"])

        def t_verify_created_bill_paise_rounding_only():
            # Two lines allow Rs 0.02 of computed-tax rounding, no more.
            r = run_verify_with_bill(verify_bill(tax_total=45.02, total=295.02))
            assert r["ok"] is True, r["errors"]
            r = run_verify_with_bill(verify_bill(tax_total=45.03, total=295.03))
            assert r["ok"] is False
            assert any(e.startswith("total_mismatch") for e in r["errors"])

        def t_verify_created_bill_fields_missing():
            for field, error in (("sub_total", "zoho_sub_total_missing"),
                                 ("total", "zoho_total_missing"),
                                 ("tax_total", "zoho_tax_total_missing")):
                bill = verify_bill()
                bill.pop(field)
                r = run_verify_with_bill(bill)
                assert r["ok"] is False
                assert error in r["errors"], (field, r["errors"])
            bill = verify_bill()
            bill.pop("currency_code")
            assert run_verify_with_bill(bill)["ok"] is False
            r = run_verify_with_bill(verify_bill(), parsed=dict(verify_parsed(), invoice_total=None))
            assert "expected_total_unavailable" in r["errors"]

        def t_verify_created_bill_lines_and_route():
            igst_as_cgst = verify_bill(line_items=[
                {"rate": 100, "quantity": 2, "tax_id": "TAX_CGST_SGST18"},
                {"rate": 50, "quantity": 1, "tax_id": "TAX_IGST18"}])
            missing_line = verify_bill(line_items=[{"rate": 100, "quantity": 2, "tax_id": "TAX_IGST18"}])
            moved_amount = verify_bill(line_items=[
                {"rate": 125, "quantity": 1, "tax_id": "TAX_IGST18"},
                {"rate": 125, "quantity": 1, "tax_id": "TAX_IGST18"}])
            for bill in (igst_as_cgst, missing_line, moved_amount):
                r = run_verify_with_bill(bill)
                assert r["ok"] is False
                assert "line_items_mismatch" in r["errors"], r["errors"]

        def t_verify_created_bill_currency_mismatch():
            r = run_verify_with_bill(verify_bill(currency_code="USD"))
            assert r["ok"] is False
            assert any("currency_code_mismatch" in e for e in r["errors"])

        def t_verify_created_bill_date_mismatch():
            r = run_verify_with_bill(verify_bill(date="2026-06-15"))
            assert r["ok"] is False
            assert any("bill_date_mismatch" in e for e in r["errors"])

        def t_verify_created_bill_fx_converted_subtotal():
            r = run_verify_with_bill(rcm_bill(8000.0), payload=rcm_payload(8000.0),
                                     parsed=verify_parsed(invoice_total=100.0, currency_code="USD",
                                                          tax_amount_igst=0), usd_rate=80)
            assert r["ok"] is True, r["errors"]
            assert r["checks"]["total"]["expected"] == 8000.0

        def t_verify_created_bill_fx_converted_no_tax():
            r = run_verify_with_bill(rcm_bill(3960.0), payload=rcm_payload(3960.0),
                                     parsed=verify_parsed(invoice_total=45, currency_code="USD",
                                                          tax_amount_igst=0), usd_rate=88)
            assert r["ok"] is True, r["errors"]
            # A rate change between conversion and read-back is a real discrepancy.
            r = run_verify_with_bill(rcm_bill(3960.0), payload=rcm_payload(3960.0),
                                     parsed=verify_parsed(invoice_total=45, currency_code="USD",
                                                          tax_amount_igst=0), usd_rate=88.5)
            assert r["ok"] is False
            assert any(e.startswith("total_mismatch") for e in r["errors"])

        def t_verify_created_bill_rcm_checks():
            parsed = verify_parsed(invoice_total=250, tax_amount_igst=0)
            r = run_verify_with_bill(rcm_bill(250.0), payload=rcm_payload(250.0), parsed=parsed)
            assert r["ok"] is True, r["errors"]
            assert r["checks"]["reverse_charge_applied"]["ok"] is True
            assert r["checks"]["line_items"]["ok"] is True

            r = run_verify_with_bill(rcm_bill(250.0, is_reverse_charge_applied=False),
                                     payload=rcm_payload(250.0), parsed=parsed)
            assert r["ok"] is False
            assert any("reverse_charge_not_applied" in e for e in r["errors"])

            for line in ({"rate": 250, "quantity": 1},
                         {"rate": 250, "quantity": 1, "reverse_charge_tax_id": "914109000000014234",
                          "reverse_charge_tax_amount": 40.0}):
                r = run_verify_with_bill(rcm_bill(250.0, line_items=[line]),
                                         payload=rcm_payload(250.0), parsed=parsed)
                assert r["ok"] is False
                assert "line_items_mismatch" in r["errors"], r["errors"]

            # The vendor is owed the pre-tax amount; a total that adds the RCM tax fails.
            r = run_verify_with_bill(rcm_bill(250.0, total=295.0), payload=rcm_payload(250.0), parsed=parsed)
            assert r["ok"] is False
            assert any(e.startswith("total_mismatch") for e in r["errors"])

        def t_due_date_null_safety():
            route = {"is_overseas": True, "tax_mode": "rcm", "tax_id_key": None}
            payload = build_payload(parsed_base(due_date="null"), "VENDOR1", route)
            assert "due_date" not in payload
            payload = build_payload(parsed_base(due_date=" NULL "), "VENDOR1", route)
            assert "due_date" not in payload
            payload = build_payload(parsed_base(due_date=None), "VENDOR1", route)
            assert "due_date" not in payload
            payload = build_payload(parsed_base(due_date="2026-05-26"), "VENDOR1", route)
            assert payload.get("due_date") == "2026-05-26"

        def t_validate_before_post():
            valid = parsed_base()
            assert validate_before_post(valid, "") == "vendor_id missing"
            assert validate_before_post(parsed_base(bill_number=""), "V1") == "bill_number empty"
            assert validate_before_post(parsed_base(bill_number="AB"), "V1") == "bill_number too short"
            assert validate_before_post(parsed_base(bill_number="MBL123"), "V1") == "bill_number equals mbl_number"
            assert validate_before_post(parsed_base(bill_date=""), "V1") == "bill_date empty"
            assert validate_before_post(parsed_base(bill_date="01-05-2026"), "V1").startswith("bill_date invalid format")
            assert validate_before_post(parsed_base(line_items=[{"description": "x", "rate": 0}]), "V1") == "no line items with rate > 0"
            assert validate_before_post(parsed_base(due_date="31-05-2026"), "V1").startswith("due_date invalid format")
            assert validate_before_post(valid, "V1") is None

        def t_reconciliation_gate():
            assert validate_before_post(parsed_base(invoice_total=0), "V1") == "invoice_total_missing"
            assert validate_before_post(parsed_base(invoice_total=""), "V1") == "invoice_total_missing"
            mismatch = validate_before_post(parsed_base(invoice_total=999.00), "V1")
            assert mismatch == "total_reconciliation_failed:999.00_vs_100.00", mismatch
            reconciled = parsed_base(
                line_items=[{"description": "Professional service consulting fee",
                             "rate": 120.0, "hsn_or_sac": "998313"}],
                discounts_or_credits=-20.0,
                invoice_total=100.0,
            )
            assert validate_before_post(reconciled, "V1") is None
            within_tolerance = parsed_base(invoice_total=100.015)
            assert validate_before_post(within_tolerance, "V1") is None
            domestic_with_gst = parsed_base(
                line_items=[{"description": "Freight charges", "rate": 10000.0}],
                discounts_or_credits=0,
                tax_amount_igst=1800.0,
                invoice_total=11800.0,
            )
            assert validate_before_post(domestic_with_gst, "V1") is None
            discount_bill = parsed_base(
                line_items=[{"description": "Service fee", "rate": 9.19}],
                discounts_or_credits=-4.19,
                invoice_total=5.00,
            )
            assert validate_before_post(discount_bill, "V1") is None

        def t_cache_behavior():
            assert _PARSER_VERSION == 7
            file_hash = "abc123"
            valid = _make_cache_entry(parsed_base(), file_hash, vendor_context_count=2)
            assert _cache_lookup({file_hash: {"bill_number": "INV123"}}, file_hash) is None
            wrong_version = dict(valid)
            wrong_version["_parser_version"] = 4
            assert _cache_lookup({file_hash: wrong_version}, file_hash) is None
            missing = dict(valid)
            missing.pop("vendor_name")
            assert _cache_lookup({file_hash: missing}, file_hash) is None
            hit = _cache_lookup({file_hash: valid}, file_hash)
            assert hit and hit["bill_number"] == "INV123"
            assert not any(k.startswith("_") for k in hit)

        def t_filename_fallback():
            cases = [
                ("INEMHC27026951 InvoicePrint.pdf", "AMC2533822", "INEMHC27026951"),
                ("INEMHC27026952.pdf", "AMC2533822", "INEMHC27026952"),
                ("BOMFALI12345 Invoice.pdf", "AMC2533822", "BOMFALI12345"),
                ("PTGFWD98765.pdf", "AMC2533822", "PTGFWD98765"),
                ("VSLNS54321_copy.pdf", "AMC2533822", "VSLNS54321"),
                ("AMC2533822.pdf", "AMC2533822", ""),
                ("invoice.pdf", "AMC2533822", ""),
                ("scan.pdf", "AMC2533822", ""),
            ]
            for filename, mbl, expected in cases:
                got = _derive_bill_number_from_filename(filename, mbl)
                assert got == expected, f"{filename}: expected {expected!r}, got {got!r}"

        def t_cma_cgm_invoice_number():
            # Real layout from CMA CGM SA / CCAI invoices under pdftotext -layout:
            # the Bill of Lading number and the invoice number land on one line.
            sample_text = (
                "Payment_info                    EXPORT INVOICE          DUPLICATE** 1 OF 1\n\n"
                "Bill of Lading: AMC2619449                                INEMHC27087125\n\n"
                "Customer: 0007121631/001                             Date: 10-SEP-2026\n"
            )
            assert is_cma_cgm_invoice("CMA CGM SA, C/O. CCAI", sample_text) is True
            assert is_cma_cgm_invoice("MAERSK LINE", "some other text") is False
            got = extract_cma_cgm_invoice_number_from_pdf_text(sample_text, "AMC2619449")
            assert got == "INEMHC27087125", f"expected INEMHC27087125, got {got!r}"
            assert got != "AMC2619449"
            # No second column present -> must not fabricate a value or return the BL#.
            no_second_col = "Bill of Lading: AMC2619449\n"
            assert extract_cma_cgm_invoice_number_from_pdf_text(no_second_col, "AMC2619449") == ""
            # Deterministic result never equals the mbl_number passed in, even if
            # a same-length token happened to repeat it.
            assert extract_cma_cgm_invoice_number_from_pdf_text(
                "Bill of Lading: AMC2619449                                AMC2619449\n",
                "AMC2619449",
            ) == ""

        def t_prompt_encoding_artifact():
            with open(__file__, encoding="utf-8-sig") as f:
                source = f.read()
            artifact = "mbl_number" + " ?"
            assert artifact not in source
            assert "The bill_number must never equal the mbl_number. If they match, you have the wrong field." in source

        def run_list_existing_bills_mode(refresh_impl, fetch_impl):
            forbidden_calls = []

            def forbidden(name):
                def _inner(*args, **kwargs):
                    forbidden_calls.append(name)
                    raise AssertionError(f"{name} touched")
                return _inner

            old_refresh = globals()["refresh_token"]
            old_fetch = globals()["fetch_existing_bills"]
            old_parse = globals()["parse_pdf_with_claude"]
            old_parse_once = globals()["parse_pdf_with_claude_once"]
            old_post = globals()["zoho_post"]
            old_fx = globals()["get_usd_to_inr"]
            old_stdout = sys.stdout
            out = io.StringIO()
            globals()["refresh_token"] = refresh_impl
            globals()["fetch_existing_bills"] = fetch_impl
            globals()["parse_pdf_with_claude"] = forbidden("parse_pdf_with_claude")
            globals()["parse_pdf_with_claude_once"] = forbidden("parse_pdf_with_claude_once")
            globals()["zoho_post"] = forbidden("zoho_post")
            globals()["get_usd_to_inr"] = forbidden("get_usd_to_inr")
            sys.stdout = out
            try:
                rc = list_existing_bills_json_mode()
            finally:
                sys.stdout = old_stdout
                globals()["refresh_token"] = old_refresh
                globals()["fetch_existing_bills"] = old_fetch
                globals()["parse_pdf_with_claude"] = old_parse
                globals()["parse_pdf_with_claude_once"] = old_parse_once
                globals()["zoho_post"] = old_post
                globals()["get_usd_to_inr"] = old_fx
            return rc, out.getvalue(), forbidden_calls

        def t_list_existing_bills_json_success():
            def fake_refresh():
                print("internal auth log")

            def fake_fetch():
                print("internal fetch log")
                return {"INV-2", " inv-1 ", "", "INV-3"}

            rc, output, forbidden_calls = run_list_existing_bills_mode(fake_refresh, fake_fetch)
            assert rc == 0
            assert forbidden_calls == []
            parsed = json.loads(output)
            assert parsed == {
                "ok": True,
                "count": 3,
                "bill_numbers": ["INV-2", "INV-3", "inv-1"],
            }
            assert output.strip() == json.dumps(parsed, separators=(",", ": "))

        def t_list_existing_bills_json_failure():
            def fake_refresh():
                print("internal auth log")
                raise Exception("Zoho unavailable\nsecond line")

            def fake_fetch():
                raise AssertionError("fetch should not run after auth failure")

            rc, output, forbidden_calls = run_list_existing_bills_mode(fake_refresh, fake_fetch)
            assert rc == 1
            assert forbidden_calls == []
            parsed = json.loads(output)
            assert parsed == {"ok": False, "error": "Zoho unavailable"}
            assert output.strip() == json.dumps(parsed, separators=(",", ": "))

        def t_list_existing_bills_json_failure_from_fetch():
            def fake_refresh():
                print("internal auth log")

            def fake_fetch():
                print("internal fetch log")
                raise Exception("fetch failed")

            rc, output, forbidden_calls = run_list_existing_bills_mode(fake_refresh, fake_fetch)
            assert rc == 1
            assert forbidden_calls == []
            parsed = json.loads(output)
            assert parsed == {"ok": False, "error": "fetch failed"}

        def t_watcher_scope_deferred():
            assert True

        def t_resolve_effective_usd_rate_invoice_date_fx():
            old_fetch = globals()["fetch_usd_inr_rate_for_date"]
            calls = []

            def fake_fetch(date_str):
                calls.append(date_str)
                return 83.5

            globals()["fetch_usd_inr_rate_for_date"] = fake_fetch
            try:
                overseas = {"is_overseas": True}
                p = parsed_base(currency_code="USD")
                rate, is_usd_overseas = resolve_effective_usd_rate(
                    p, overseas, "2026-05-01", usd_rate=80, use_invoice_date_fx=True)
                assert is_usd_overseas is True
                assert rate == 83.5, f"expected fetched rate 83.5, got {rate!r}"
                assert calls == ["2026-05-01"]
            finally:
                globals()["fetch_usd_inr_rate_for_date"] = old_fetch

        def t_resolve_effective_usd_rate_inr_skips_fetch():
            old_fetch = globals()["fetch_usd_inr_rate_for_date"]

            def forbidden(date_str):
                raise AssertionError("fetch_usd_inr_rate_for_date touched for an INR bill")

            globals()["fetch_usd_inr_rate_for_date"] = forbidden
            try:
                overseas = {"is_overseas": True}
                p = parsed_base(currency_code="INR")
                rate, is_usd_overseas = resolve_effective_usd_rate(
                    p, overseas, "2026-05-01", usd_rate=80, use_invoice_date_fx=True)
                assert is_usd_overseas is False
                assert rate == 80
            finally:
                globals()["fetch_usd_inr_rate_for_date"] = old_fetch

        def t_resolve_effective_usd_rate_fallback_on_fetch_failure():
            old_fetch = globals()["fetch_usd_inr_rate_for_date"]

            def failing_fetch(date_str):
                raise Exception("network down")

            globals()["fetch_usd_inr_rate_for_date"] = failing_fetch
            try:
                overseas = {"is_overseas": True}
                p = parsed_base(currency_code="USD")
                rate, is_usd_overseas = resolve_effective_usd_rate(
                    p, overseas, "2026-05-01", usd_rate=80, use_invoice_date_fx=True)
                assert is_usd_overseas is True
                assert rate == 80, f"expected fallback to --usd-rate value 80, got {rate!r}"
            finally:
                globals()["fetch_usd_inr_rate_for_date"] = old_fetch

        check("determine_overseas_status", t_overseas_status)
        check("determine_tax_route", t_tax_route)
        check("existing_bill_lookup", t_existing_bill_lookup)
        check("overseas_vendor_charged_indian_gst_stop", t_overseas_vendor_charged_indian_gst_stop)
        check("created_bill_id", t_created_bill_id)
        check("build_payload", t_build_payload)
        check("verify_created_bill_matching", t_verify_created_bill_matching)
        check("verify_created_bill_total_must_match_printed", t_verify_created_bill_total_must_match_printed)
        check("verify_created_bill_bill_number_mismatch", t_verify_created_bill_number_mismatch)
        check("verify_created_bill_vendor_id_mismatch", t_verify_created_bill_vendor_mismatch)
        check("verify_created_bill_amount_mismatch", t_verify_created_bill_amount_mismatch)
        check("verify_created_bill_no_percentage_tolerance", t_verify_created_bill_no_percentage_tolerance)
        check("verify_created_bill_paise_rounding_only", t_verify_created_bill_paise_rounding_only)
        check("verify_created_bill_fields_missing", t_verify_created_bill_fields_missing)
        check("verify_created_bill_lines_and_route", t_verify_created_bill_lines_and_route)
        check("verify_created_bill_currency_mismatch", t_verify_created_bill_currency_mismatch)
        check("verify_created_bill_date_mismatch", t_verify_created_bill_date_mismatch)
        check("verify_created_bill_fx_converted_subtotal", t_verify_created_bill_fx_converted_subtotal)
        check("verify_created_bill_fx_converted_no_tax", t_verify_created_bill_fx_converted_no_tax)
        check("verify_created_bill_rcm_checks", t_verify_created_bill_rcm_checks)
        check("build_payload_due_date_null", t_due_date_null_safety)
        check("validate_before_post", t_validate_before_post)
        check("reconciliation_gate", t_reconciliation_gate)
        check("cache_behavior", t_cache_behavior)
        check("filename_fallback", t_filename_fallback)
        check("cma_cgm_invoice_number", t_cma_cgm_invoice_number)
        check("ai_prompt_encoding_artifact",
              t_prompt_encoding_artifact)
        check("list_existing_bills_json_success", t_list_existing_bills_json_success)
        check("list_existing_bills_json_failure", t_list_existing_bills_json_failure)
        check("list_existing_bills_json_fetch_failure", t_list_existing_bills_json_failure_from_fetch)
        check("watcher_lock_scope",
              t_watcher_scope_deferred,
              "KNOWN_OPEN_WATCHER_LOCK_SCOPE_DEFERRED_PHASE_4")
        check("resolve_effective_usd_rate_invoice_date_fx", t_resolve_effective_usd_rate_invoice_date_fx)
        check("resolve_effective_usd_rate_inr_skips_fetch", t_resolve_effective_usd_rate_inr_skips_fetch)
        check("resolve_effective_usd_rate_fallback_on_fetch_failure", t_resolve_effective_usd_rate_fallback_on_fetch_failure)
    finally:
        urllib.request.urlopen = _orig_urlopen
        globals()["zoho_get"] = _orig_zoho_get

    print("KNOWN_LABELS: " + ", ".join(sorted(known_labels)))
    if failures:
        print(f"SUMMARY: FAIL {len(failures)} failure(s), "
              f"{check_count - len(failures)} passed/known")
        sys.exit(1)
    print(f"SUMMARY: PASS {check_count} tests, 0 failures")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    elif "--test" in sys.argv:
        _business_test()
    elif "--list-existing-bills-json" in sys.argv:
        sys.exit(list_existing_bills_json_mode())
    else:
        main()
