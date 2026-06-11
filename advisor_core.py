"""
advisor_core.py — All business logic.
No GUI code here — keeps things testable and reusable.

Features:
  - .env file read/write for API key persistence
  - Auto-detect catalog PDF and student Excel in the working directory
  - Catalog cache management: list, delete, force re-upload
  - Richer step-by-step log messages throughout
  - Universal Chain-of-Thought prompts (English, no hard-coded university rules)
  - Arabic search normalisation (NFC + substitution) – fast, robust
  - Optional catalog summarisation (Step 0) to reduce token usage
  - Optional post-recommendation validation against catalog codes
  - Thread-safe KeyPool with reentrant lock
  - Step 1.5: structured JSON extraction of CGPA, credit limit, passed courses
  - Python-enforced credit cap and passed-course deduplication (trust-but-verify)
  - Retry logic on catalog summarisation
  - Word-boundary catalog code validation
  - Progress callback wired through batch_all
"""

import os
import time
import json
import re
import glob
import unicodedata
import threading
import pandas as pd
from google import genai
from google.genai import types

# ── Constants ─────────────────────────────────────────────────────────────────
MODEL_ID           = "gemini-flash-latest"
CATALOG_CACHE_FILE = "catalog_cache.json"
CATALOG_SUMMARY_CACHE = "catalog_summary.txt"
ENV_FILE           = ".env"
REQUEST_DELAY      = 1.5   # seconds between batch requests

# ── .env helpers ──────────────────────────────────────────────────────────────

def _clean_env_value(value: str) -> str:
    if not value:
        return ""
    value = value.strip()
    if "#" in value:
        value = value.split("#", 1)[0].strip()
    return value.strip().strip('"').strip("'")

def load_env_file() -> dict:
    result = {}
    if not os.path.exists(ENV_FILE):
        return result
    with open(ENV_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            result[k.strip()] = _clean_env_value(v)
    return result

def save_env_file(keys: list):
    existing = {}
    if os.path.exists(ENV_FILE):
        with open(ENV_FILE, encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if not stripped or stripped.startswith("#") or "=" not in stripped:
                    continue
                k, _, v = stripped.partition("=")
                k = k.strip()
                if not k.startswith("GEMINI_KEY_"):
                    existing[k] = v.strip()
    lines = ["# Academic Advisor AI — API keys (auto-saved by app)\n"]
    for k, v in existing.items():
        lines.append(f"{k}={v}\n")
    for i, key in enumerate(keys, 1):
        lines.append(f"GEMINI_KEY_{i}={key}\n")
    with open(ENV_FILE, "w", encoding="utf-8") as f:
        f.writelines(lines)

def _discover_keys() -> list:
    seen = set()
    keys = []
    def _add(k):
        k = _clean_env_value(k)
        if k and k not in seen and k != "YOUR_API_KEY_HERE":
            seen.add(k)
            keys.append(k)
    env_data = load_env_file()
    i = 1
    while True:
        v = env_data.get(f"GEMINI_KEY_{i}", "")
        if not v:
            break
        _add(v)
        i += 1
    i = 1
    while True:
        v = os.environ.get(f"GEMINI_KEY_{i}", "")
        if not v:
            break
        _add(v)
        i += 1
    for v in os.environ.get("GEMINI_API_KEYS", "").split(","):
        _add(v)
    _add(os.environ.get("GEMINI_API_KEY", ""))
    return keys or ["YOUR_API_KEY_HERE"]

# ── API Key Pool ──────────────────────────────────────────────────────────────

class KeyPool:
    """Thread-safe round-robin API key pool with bad-key tracking."""

    def __init__(self, keys: list):
        self._keys  = list(keys)
        self._index = 0
        self._bad   = set()
        self._lock  = threading.Lock()

    def current(self) -> str:
        with self._lock:
            return self._keys[self._index % len(self._keys)]

    def current_name(self) -> str:
        with self._lock:
            idx = self._index % len(self._keys)
            return f"GEMINI_KEY_{idx + 1}"

    def rotate(self, mark_bad: bool = False):
        with self._lock:
            if mark_bad:
                self._bad.add(self._index % len(self._keys))
            self._index = (self._index + 1) % len(self._keys)

    def get_client(self):
        with self._lock:
            for _ in range(len(self._keys)):
                idx = self._index % len(self._keys)
                if idx not in self._bad:
                    return genai.Client(api_key=self._keys[idx])
                self._index += 1
            # All keys marked bad — clear and try again
            self._bad.clear()
            return genai.Client(api_key=self._keys[self._index % len(self._keys)])

    def set_keys(self, keys: list, persist: bool = True):
        with self._lock:
            self._keys  = list(keys)
            self._index = 0
            self._bad   = set()
        if persist:
            save_env_file(keys)

    @property
    def keys(self) -> list:
        with self._lock:
            return list(self._keys)

    @property
    def key_count(self) -> int:
        with self._lock:
            return len(self._keys)

pool = KeyPool(_discover_keys())

# ── Auto-detect files ─────────────────────────────────────────────────────────

def auto_detect_catalog(search_dir: str = ".") -> str:
    pdfs = sorted(glob.glob(os.path.join(search_dir, "*.pdf")))
    return pdfs[0] if pdfs else ""

def auto_detect_excel(search_dir: str = ".") -> str:
    for pattern in ("*.xlsx", "*.xls"):
        hits = sorted(glob.glob(os.path.join(search_dir, pattern)))
        if hits:
            return hits[0]
    return ""

# ── Catalog cache management ──────────────────────────────────────────────────

def _load_cache() -> dict:
    if os.path.exists(CATALOG_CACHE_FILE):
        with open(CATALOG_CACHE_FILE) as f:
            return json.load(f)
    return {}

def _find_cached_path_by_uri(uri: str):
    cache = _load_cache()
    for path, info in cache.items():
        if info.get("uri") == uri or info.get("name") == uri:
            return path
    return None

def _save_cache(data: dict):
    with open(CATALOG_CACHE_FILE, "w") as f:
        json.dump(data, f, indent=2)

def _new_client():
    return pool.get_client()

def _close_client(client):
    try:
        client.close()
    except Exception:
        pass

def list_cached_catalogs() -> list:
    cache = _load_cache()
    result = []
    for path, info in cache.items():
        entry = {"path": path, **info, "active": False}
        client = _new_client()
        try:
            fobj = client.files.get(name=info["name"])
            entry["active"] = (fobj.state == "ACTIVE")
        except Exception:
            pass
        finally:
            _close_client(client)
        result.append(entry)
    return result

def delete_cached_catalog(path: str, log=print) -> bool:
    cache = _load_cache()
    key = os.path.abspath(path)
    if key not in cache:
        for k in list(cache.keys()):
            if os.path.basename(k) == os.path.basename(path):
                key = k
                break
        else:
            log(f"⚠  '{os.path.basename(path)}' not found in catalog cache.")
            return False
    info = cache[key]
    log(f"🗑  Removing catalog: '{os.path.basename(path)}'")
    client = _new_client()
    try:
        client.files.delete(name=info["name"])
        log(f"  ✓ Deleted from Gemini Files API ({info['name']})")
    except Exception as e:
        log(f"  ⚠  Could not delete from API (may have already expired): {e}")
    finally:
        _close_client(client)
    del cache[key]
    _save_cache(cache)
    log(f"  ✓ Removed from local cache.")
    return True

def get_or_upload_catalog(path: str, log=print, force: bool = False) -> tuple:
    key = os.path.abspath(path)
    cache = _load_cache()
    if not force and key in cache:
        cached = cache[key]
        log(f"📋 Found cached catalog '{os.path.basename(path)}' — verifying with Gemini…")
        client = _new_client()
        try:
            fobj = client.files.get(name=cached["name"])
            if fobj.state == "ACTIVE":
                log(f"  ✓ Still ACTIVE on Gemini. Skipping upload.")
                return cached["uri"], cached["mime_type"]
            log(f"  ⚠  No longer active (state={fobj.state}). Will re-upload.")
        except Exception as e:
            err = str(e).lower()
            if any(x in err for x in ["permission denied", "permission_denied",
                                      "do not have permission", "403"]):
                log(f"  ⚠  Current key cannot access cached catalog ({e}). Re-uploading with current key…")
            else:
                log(f"  ⚠  Verification failed ({e}). Will re-upload.")
        finally:
            _close_client(client)
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"Catalog PDF not found: {path}\n"
            f"Tip: Use Browse or drop the file onto the field.")
    size_mb = os.path.getsize(path) / (1024 * 1024)
    log(f"📤 Uploading '{os.path.basename(path)}' ({size_mb:.1f} MB) to Gemini…")
    log(f"  Large PDFs may take 10–30 seconds. Please wait…")
    upload_error = None
    max_attempts = max(2, pool.key_count + 1)
    for attempt in range(1, max_attempts + 1):
        client = _new_client()
        try:
            log(f"  Upload attempt {attempt}/{max_attempts} using {pool.current_name()}…")
            # Use a safe ASCII display name — the HTTP layer cannot encode
            # non-ASCII characters (e.g. Arabic) in headers/multipart metadata.
            safe_name = "catalog.pdf"
            with open(path, "rb") as _fh:
                fobj = client.files.upload(
                    file=_fh,
                    config=types.UploadFileConfig(
                        mime_type="application/pdf",
                        display_name=safe_name,
                    ),
                )
            upload_error = None
            break
        except Exception as e:
            upload_error = e
            err_text = str(e).lower()
            if "client has been closed" in err_text:
                log(f"  ⚠  Upload client closed unexpectedly. Retrying with a fresh client…")
                time.sleep(1)
                continue
            raise RuntimeError(
                f"Upload failed: {e}\n"
                f"Check your API key and internet connection.")
        finally:
            _close_client(client)
    if upload_error is not None:
        raise RuntimeError(
            f"Upload failed after {max_attempts} attempts: {upload_error}\n"
            f"Check your API key and internet connection.")
    waited = 0
    while fobj.state == "PROCESSING":
        log(f"  ⏳ Processing… ({waited}s elapsed)")
        time.sleep(2)
        waited += 2
        client = _new_client()
        try:
            fobj = client.files.get(name=fobj.name)
        except Exception as e:
            raise RuntimeError(f"Lost contact with Gemini during processing: {e}")
        finally:
            _close_client(client)
    if fobj.state != "ACTIVE":
        raise RuntimeError(
            f"Upload finished but file state is '{fobj.state}' (expected ACTIVE).\n"
            f"Try deleting the cache entry and re-uploading.")
    log(f"  ✓ Upload complete and ACTIVE.")
    log(f"  File name: {fobj.name}")
    log(f"  URI:       {fobj.uri}")
    cache[key] = {"name": fobj.name, "uri": fobj.uri, "mime_type": fobj.mime_type}
    _save_cache(cache)
    log(f"  ✓ Saved to local cache — won't need to upload again unless file expires.")
    return fobj.uri, fobj.mime_type

# ── Excel Reading ─────────────────────────────────────────────────────────────

def _engine(path: str) -> str:
    return "xlrd" if path.lower().endswith(".xls") else "openpyxl"

def read_all_sheets(path: str, log=print) -> dict:
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"Excel file not found: {path}\n"
            f"Tip: Use Browse or check the file path.")
    eng = _engine(path)
    log(f"📂 Opening '{os.path.basename(path)}' (engine: {eng})…")
    try:
        xl = pd.ExcelFile(path, engine=eng)
    except Exception as e:
        raise ValueError(
            f"Cannot open Excel file: {e}\n"
            f"Make sure the file is not open in another program and is not corrupted.")
    out = {}
    log(f"  Found {len(xl.sheet_names)} sheet(s): {xl.sheet_names}")
    for name in xl.sheet_names:
        try:
            df = pd.read_excel(xl, sheet_name=name, engine=eng).dropna(how="all")
            if df.empty:
                log(f"  ⚠  Sheet '{name}' is empty — skipping.")
                continue
            out[name] = df
            log(f"  ✓ '{name}': {len(df)} rows")
        except Exception as e:
            log(f"  ✗ Could not read sheet '{name}': {e}")
    log(f"  ✓ {len(out)} non-empty sheet(s) loaded.")
    return out

# ── Arabic normalisation (robust) ─────────────────────────────────────────────

def _normalize_arabic(text) -> str:
    """Normalise Arabic text for flexible matching. Handles non‑string inputs safely."""
    if not isinstance(text, str):
        text = str(text)
    if not text:
        return ""
    text = unicodedata.normalize('NFC', text)
    text = re.sub(r'[أإآ]', 'ا', text)
    text = re.sub(r'[ؤ]', 'و', text)
    text = re.sub(r'[ىيئ]', 'ي', text)
    text = re.sub(r'[\u064B-\u0652]', '', text)  # diacritics
    text = text.replace('\u0640', '')             # kashida
    return text

def normalize_course_code(code: str) -> str:
    """Canonical form of a course code for deduplication: lowercase, no spaces/dashes/underscores.

    Examples:
        'MATH-101'  → 'math101'
        'MATH 101'  → 'math101'
        'math101'   → 'math101'
    """
    if not isinstance(code, str):
        code = str(code)
    return re.sub(r'[\s\-_]', '', code).lower()

def _extract_course_codes_from_df(df) -> list:
    """Best-effort Python scan of the student DataFrame to collect every course
    code visible in the transcript, regardless of grade.

    Heuristic: a column whose name contains keywords like 'code', 'course',
    'subject', 'رمز', 'مادة', or whose values look like typical course codes
    (2-4 alpha chars followed by digits, optionally separated by dash/space)
    is treated as a course-code column.

    Returns a deduplicated list of normalised course codes.
    """
    CODE_PATTERN = re.compile(r'^[A-Za-z]{2,6}[\s\-_]?\d{2,4}[A-Za-z]?$')
    CODE_COL_KEYWORDS = [
        "code", "course", "subject", "رمز", "مادة", "كود المقرر",
        "رمز المقرر", "رمز المادة",
    ]

    code_cols = []
    for col in df.columns:
        col_key = str(col).lower()
        if any(k in col_key for k in CODE_COL_KEYWORDS):
            code_cols.append(col)
            continue
        # Auto-detect: if ≥50 % of non-null values look like course codes
        sample = df[col].dropna().astype(str).head(20)
        if len(sample) == 0:
            continue
        matches = sum(1 for v in sample if CODE_PATTERN.match(v.strip()))
        if matches / len(sample) >= 0.5:
            code_cols.append(col)

    seen: set = set()
    codes: list = []
    for col in code_cols:
        for raw in df[col].dropna().astype(str):
            raw = raw.strip()
            if CODE_PATTERN.match(raw):
                norm = normalize_course_code(raw)
                if norm not in seen:
                    seen.add(norm)
                    codes.append(raw)  # keep original form for display
    return codes

# ── Student search (fast, robust) ────────────────────────────────────────────

def _looks_like_id(query: str) -> bool:
    query = str(query).strip()
    if not query:
        return False
    digits = sum(1 for c in query if c.isdigit())
    letters = sum(1 for c in query if c.isalpha())
    if digits and not letters and len(query) >= 4:
        return True
    if digits and letters and len(query) <= 16:
        return True
    return False

def _column_category(column_name: str) -> str:
    key = str(column_name).lower()
    if any(k in key for k in [
        "student_id", "student id", "student number", "id",
        "رقم", "كود", "رقم الطالب", "رقم القيد", "الرقم الجامعي", "الكود",
    ]):
        return "student_id"
    if any(k in key for k in [
        "arabic", "ar", "عربي", "بالعربية", "الاسم العربي", "الاسم بالعربي",
        "اسم الطالب", "الاسم",
    ]):
        return "name_arabic"
    if any(k in key for k in [
        "english", "name", "student", "first", "last", "fullname",
        "الاسم بالانجليزي", "الاسم الانجليزي"
    ]):
        return "name_english"
    return "other"

def _match_score(col_name: str, query_type: str) -> int:
    cat = _column_category(col_name)
    if query_type == "student_id":
        if cat == "student_id":
            return 80
        if cat in ("name_english", "name_arabic"):
            return 30
        return 10
    if query_type == "name_arabic":
        if cat == "name_arabic":
            return 70
        if cat == "name_english":
            return 40
        if cat == "student_id":
            return 10
        return 5
    if cat == "name_english":
        return 70
    if cat == "name_arabic":
        return 50
    if cat == "student_id":
        return 20
    return 5

def _column_has_arabic(col_values) -> bool:
    """Quick check: does any of the first 10 cells contain Arabic characters?
    Uses explicit str() conversion on every sample value to avoid float errors."""
    sample = col_values.head(min(10, len(col_values)))
    for s in sample:
        try:
            if re.search(r'[\u0621-\u064A]', str(s)):
                return True
        except Exception:
            pass  # non‑string value safely ignored
    return False

def _search_sheets(sheets: dict, query: str, use_normalisation: bool = True) -> list:
    """Internal: search across sheets, returning (score, sheet_name, df) list."""
    q = str(query).strip()
    if not q:
        return []
    q_lower = q.lower()
    query_is_arabic = bool(re.search(r'[\u0621-\u064A]', q_lower))
    query_type = "student_id" if _looks_like_id(q) else ("name_arabic" if query_is_arabic else "name_english")

    hits = []
    for name, df in sheets.items():
        score = 0
        for col in df.columns:
            col_values = df[col].astype(str)
            if query_is_arabic and use_normalisation:
                if not _column_has_arabic(col_values):
                    # skip normalisation for non‑Arabic columns
                    test_query = q_lower
                    if col_values.str.contains(test_query, na=False, regex=False).any():
                        score = max(score, _match_score(col, query_type))
                    continue
                # Normalise only columns that actually contain Arabic
                col_values = col_values.apply(_normalize_arabic)
                test_query = _normalize_arabic(q_lower)
            else:
                col_values = col_values.str.lower()
                test_query = q_lower

            if col_values.str.contains(test_query, na=False, regex=False).any():
                score = max(score, _match_score(col, query_type))
        if score:
            hits.append((score, name, df))

    return hits

def find_students(sheets: dict, query: str) -> list:
    """Case-insensitive substring match. Tries normalised Arabic first; falls back to raw match if nothing found."""
    hits = _search_sheets(sheets, query, use_normalisation=True)
    if not hits:
        hits = _search_sheets(sheets, query, use_normalisation=False)
    hits.sort(key=lambda item: (-item[0], item[1]))
    return [(name, df) for _, name, df in hits]

def _first_nonempty_value(df, column):
    for raw in df[column].astype(str):
        value = str(raw).strip()
        if not value or value.lower() == "nan":
            continue
        return value
    return ""

def extract_identity(df) -> tuple:
    sid = name_en = name_ar = "Unknown"
    id_cols, en_cols, ar_cols, other_cols = [], [], [], []
    for col in df.columns:
        category = _column_category(col)
        if category == "student_id":
            id_cols.append(col)
        elif category == "name_arabic":
            ar_cols.append(col)
        elif category == "name_english":
            en_cols.append(col)
        else:
            other_cols.append(col)
    for col in id_cols:
        if sid == "Unknown":
            val = _first_nonempty_value(df, col)
            if val:
                sid = val
    for col in en_cols:
        if name_en == "Unknown":
            val = _first_nonempty_value(df, col)
            if val:
                name_en = val
    for col in ar_cols:
        if name_ar == "Unknown":
            val = _first_nonempty_value(df, col)
            if val:
                name_ar = val
    if sid == "Unknown":
        for col in id_cols + other_cols:
            val = _first_nonempty_value(df, col)
            if val and any(ch.isdigit() for ch in val):
                sid = val
                break
    if name_en == "Unknown":
        for col in en_cols + other_cols:
            val = _first_nonempty_value(df, col)
            if val and not any(ch.isdigit() for ch in val):
                name_en = val
                break
    if name_ar == "Unknown":
        for col in ar_cols + other_cols:
            val = _first_nonempty_value(df, col)
            if val and re.search(r'[\u0621-\u064A]', val):
                name_ar = val
                break
    return sid, name_en, name_ar

def _student_label(df, sheet_name: str) -> str:
    sid, name_en, name_ar = extract_identity(df)
    if name_en != "Unknown":
        return f"{name_en} (ID: {sid})"
    if name_ar != "Unknown":
        return f"{name_ar} (ID: {sid})"
    if sid != "Unknown":
        return f"ID: {sid}"
    for col in df.columns:
        val = _first_nonempty_value(df, col)
        if val:
            return f"{sheet_name} / {val}"
    return sheet_name

# ── Chain-of-Thought Prompting (Universal, English) ───────────────────────────

def _call_gemini(client, catalog_uri, catalog_mime, conversation, expect_json=False, condensed_catalog_text=None):
    contents = []
    for i, msg in enumerate(conversation):
        role = msg["role"]
        text = msg["text"]
        if role == "user" and i == 0:
            parts = []
            if condensed_catalog_text:
                parts.append(types.Part.from_text(text=f"Catalog summary (contains all essential information):\n{condensed_catalog_text}"))
            else:
                parts.append(types.Part.from_uri(file_uri=catalog_uri, mime_type=catalog_mime))
            parts.append(types.Part.from_text(text=text))
            contents.append(types.Content(role="user", parts=parts))
        else:
            contents.append(types.Content(role=role, parts=[types.Part.from_text(text=text)]))
    cfg = types.GenerateContentConfig(
        response_mime_type="application/json" if expect_json else "text/plain",
        temperature=0.1,
        top_p=0.9,
    )
    resp = client.models.generate_content(model=MODEL_ID, contents=contents, config=cfg)
    return resp.text.strip()

def _prompt_step1(student_md: str, term: str) -> str:
    return f"""You are an expert academic advisor. Your task is to carefully analyse a student's transcript and the attached university catalog PDF.

**Instructions:**
1. **Student Identity**: Extract student ID, full name (English and Arabic if present), major, and current academic level.
2. **Passed Courses (consolidated)**: For each course taken multiple times, use the **best grade only**. List each passed course with: course code, title, grade, credit hours. A passing grade is defined by the catalog's grading rules (extract from PDF). Clearly state which grades are considered failing.
   - When comparing courses, treat variations like 'MATH-101', 'MATH 101', 'MATH101' as the **same course** and consolidate them using the best grade.
3. **Failed / Still Required Courses**: List all courses that have not been passed yet.
4. **Non‑Credit & Odd‑Credit Requirements**: Note any special requirements from the catalog that are **non‑credit**, **0‑credit**, **field training**, **internship**, **summer‑only**, or explicitly stated as “does not count toward GPA / credit limit”. These courses must NOT be recommended later. List them separately so they are excluded.
5. **CGPA Calculation**: Compute the current cumulative GPA using the grading scale and credit hours from the catalog. Provide the calculation.
6. **Credit Limit**: From the catalog, find rules regarding maximum allowed credits per semester (normal and summer). Determine the exact credit limit for the upcoming term: **{term}**.
7. **Special Notes**: Is the student near graduation (> 90% credits completed)? Are there critical prerequisites that block many courses? Are there any **odd rules** that might affect which courses can be taken?

**Important:** Do NOT suggest any courses yet. Present your analysis in plain English, structured with clear headings.
Transcript:
{student_md}
"""

def _prompt_step1_5() -> str:
    """Step 1.5 — extract hard facts as a small JSON object.

    Sent immediately after Step 1's free-text analysis. The model has just
    produced the analysis so it can accurately structure these numbers.
    """
    return """Based on your analysis above, output ONLY the following JSON object — no other text.

{
  "cgpa": <float, the student's current cumulative GPA>,
  "credit_limit": <int, the maximum credit hours the student may take next term>,
  "total_earned_credits": <int, credit hours the student has successfully completed so far>,
  "total_required_credits": <int, total credit hours required to graduate>,
  "passed_courses": ["<EXACT course code as it appears in the catalog>", ...]
}

Rules:
- passed_courses must list EVERY course the student has passed (best-grade rule applied), using the exact catalog course code.
- credit_limit must reflect the student's actual CGPA tier from the catalog (e.g. GPA < 2.0 → 12 hrs, GPA 2.0-2.99 → 15 hrs, GPA ≥ 3.0 → 18 hrs). Do NOT default to 18.
- Output only valid JSON, no markdown fences, no explanation.
"""

def _parse_student_facts(raw_json: str, student_label: str) -> dict:
    """Parse the Step 1.5 JSON into a student_facts dict.

    Returns a safe default dict if parsing fails so downstream steps still work.
    """
    defaults = {
        "cgpa": None,
        "credit_limit": None,
        "total_earned_credits": None,
        "total_required_credits": None,
        "passed_courses": [],
    }
    try:
        cleaned = re.sub(r'^```(?:json)?\s*', '', raw_json.strip())
        cleaned = re.sub(r'\s*```$', '', cleaned.strip())
        # Accept both object and the object wrapped in an array
        obj = json.loads(cleaned)
        if isinstance(obj, list) and obj:
            obj = obj[0]
        if not isinstance(obj, dict):
            return defaults
        facts = {**defaults, **obj}
        # Normalise types
        try:
            facts["cgpa"] = float(facts["cgpa"]) if facts["cgpa"] is not None else None
        except (TypeError, ValueError):
            facts["cgpa"] = None
        for int_key in ("credit_limit", "total_earned_credits", "total_required_credits"):
            try:
                facts[int_key] = int(facts[int_key]) if facts[int_key] is not None else None
            except (TypeError, ValueError):
                facts[int_key] = None
        if not isinstance(facts["passed_courses"], list):
            facts["passed_courses"] = []
        facts["passed_courses"] = [str(c).strip() for c in facts["passed_courses"] if c]
        return facts
    except Exception:
        return defaults

def _prompt_step2(term: str, student_facts: dict | None = None) -> str:
    """Build the Step 2 prompt, optionally injecting hard constraints from Step 1.5."""
    constraints = ""
    if student_facts:
        passed = student_facts.get("passed_courses", [])
        limit  = student_facts.get("credit_limit")
        cgpa   = student_facts.get("cgpa")
        earned = student_facts.get("total_earned_credits")
        total  = student_facts.get("total_required_credits")

        parts = []
        if cgpa is not None:
            parts.append(f"Student CGPA: **{cgpa:.2f}**")
        if limit is not None:
            parts.append(f"Maximum credit hours allowed next term: **{limit} hours** (non-negotiable — dictated by CGPA tier in the catalog)")
        if earned is not None and total is not None:
            parts.append(f"Credits completed: **{earned} / {total}**")
        if passed:
            passed_list = ", ".join(passed)
            parts.append(
                f"Confirmed passed courses (NEVER recommend any of these under any code variation): **{passed_list}**"
            )
        if parts:
            constraints = (
                "\n\n**⚠ HARD CONSTRAINTS FROM STEP 1.5 (THESE OVERRIDE EVERYTHING ELSE):**\n"
                + "\n".join(f"- {p}" for p in parts)
                + "\n"
            )

    return f"""Excellent. Now, using your analysis above and the catalog PDF (or its summary), perform the following for every remaining required course:{constraints}

1. **Exclude Non‑Standard Courses Immediately**: If a course is listed in the "Non‑Credit & Odd‑Credit Requirements" section of Step 1, or if it is 0 credits, field training, summer‑only, or does not count toward the credit limit, **discard it now**. Do NOT include it in any list. Only consider standard credit‑bearing courses that can be taken in the requested term.
2. **Already Passed Check**:
   - If the course (under ANY normalised form — 'MATH-101', 'MATH 101', 'MATH101' are the same) appears in the confirmed passed courses list above OR in your Step 1 Passed Courses, **discard it immediately and permanently**. The student has satisfied it.
3. **Prerequisite Check**:
   - Locate the course in the catalog.
   - List its prerequisites.
   - Verify that ALL prerequisites appear in your "Passed Courses" list from Step 1. If any prerequisite is missing, discard this course.
4. **Semester Availability Check**:
   - In the catalog's study plan (the recommended schedule table), find exactly which semester(s) this course is offered.
   - Quote the **exact text** from the table that confirms it is offered in **{term}**.
   - If the course is clearly marked for a different semester, discard it.
5. **Output a list of eligible candidates** in the following format for each:
   - Course: [Course Code] - [Course Title] ([Credit Hours] credits)
   - Prerequisites satisfied: [List]
   - Catalog proof: "[Direct quote from the study plan table]"

If no courses are eligible, explain why. Do not output JSON yet.
"""

def _prompt_step3(term: str, student_facts: dict | None = None) -> str:
    """Build the Step 3 prompt with the credit limit injected as a hard number."""
    credit_constraint = ""
    if student_facts:
        limit = student_facts.get("credit_limit")
        cgpa  = student_facts.get("cgpa")
        if limit is not None:
            cgpa_str = f" (CGPA {cgpa:.2f})" if cgpa is not None else ""
            credit_constraint = (
                f"\n\n**⚠ HARD CREDIT LIMIT: The student may enrol in AT MOST {limit} credit hours"
                f"{cgpa_str}. If the eligible courses total more than {limit} hours, remove"
                f" lower-priority courses (electives before mandatory, later semesters before earlier)"
                f" until the total is ≤ {limit}. This limit is absolute — do NOT exceed it.**\n"
            )

    return f"""Now you have:
- The list of passed courses and the credit limit from Step 1.
- The filtered list of eligible courses from Step 2 (all standard credit‑bearing and offered in {term}).{credit_constraint}

**Your final task:**
Convert the eligible courses into a JSON array. **Strict rules:**
- Do NOT add any course that did not appear in your Step 2 list.
- Do NOT include any course from the confirmed passed courses list.
- Exclude any course that is non‑credit, 0‑credit, field training, summer‑only, or does not count toward credit limit.
- The total credits of ALL recommended courses MUST NOT exceed the credit limit stated above.
- Output **only** the JSON, no additional text or markdown.

Format:
[
  {{
    "course_code": "...",
    "course_title": "...",
    "course_title_in_arabic": "...",
    "credits": number,
    "justification": "Prerequisites satisfied: ...",
    "catalog_availability_proof": "Exact quote from study plan table"
  }}
]
"""

def _strip_json(raw: str) -> str:
    raw = re.sub(r"^```(?:json)?\s*", "", raw.strip())
    raw = re.sub(r"\s*```$", "", raw.strip())
    start = raw.find("[")
    end   = raw.rfind("]")
    if start != -1 and end != -1:
        raw = raw[start:end + 1]
    return raw.strip()

def _normalize_text(value):
    if value is None: return ""
    if isinstance(value, str): return value.strip()
    return str(value).strip()

def _normalize_credits(value):
    if isinstance(value, (int, float)): return int(value)
    if isinstance(value, str):
        value = value.strip()
        if value.isdigit(): return int(value)
        try: return int(float(value))
        except ValueError: return None
    return None

def _validate_course_object(course, student_label: str):
    if not isinstance(course, dict):
        raise RuntimeError(f"Invalid course entry for {student_label}: expected object, got {type(course).__name__}.")
    required_fields = [
        "course_code", "course_title", "course_title_in_arabic",
        "credits", "justification", "catalog_availability_proof",
    ]
    missing = [f for f in required_fields if f not in course]
    if missing:
        raise RuntimeError(f"Missing required fields for {student_label}: {missing}.")
    course_code = _normalize_text(course.get("course_code"))
    if not course_code: raise RuntimeError(f"Invalid course_code for {student_label}.")
    title = _normalize_text(course.get("course_title"))
    if not title: raise RuntimeError(f"Invalid course_title for {student_label}.")
    title_ar = _normalize_text(course.get("course_title_in_arabic"))
    if not title_ar: raise RuntimeError(f"Invalid course_title_in_arabic for {student_label}.")
    credits = _normalize_credits(course.get("credits"))
    if credits is None or credits <= 0: raise RuntimeError(f"Invalid credits for {student_label}.")
    justification = _normalize_text(course.get("justification"))
    if not justification: raise RuntimeError(f"Invalid justification for {student_label}.")
    proof = _normalize_text(course.get("catalog_availability_proof"))
    if not proof: raise RuntimeError(f"Invalid catalog_availability_proof for {student_label}.")
    course["course_code"] = course_code
    course["course_title"] = title
    course["course_title_in_arabic"] = title_ar
    course["credits"] = credits
    return course

def _validate_course_list(courses, student_label: str):
    if not isinstance(courses, list):
        raise RuntimeError(f"Expected a list of courses for {student_label}, got {type(courses).__name__}.")
    return [_validate_course_object(course, student_label) for course in courses]

def _validate_against_catalog(courses, condensed_catalog_text: str, log=print) -> list:
    """Discard courses whose code is not found in the catalog summary.

    Uses word-boundary regex matching to prevent false positives such as
    'CS-1' matching 'CS-10' or 'CS-100'.
    """
    if not condensed_catalog_text or not courses:
        return courses
    catalog_text = condensed_catalog_text.lower()
    valid = []
    for c in courses:
        code = c.get("course_code", "").lower()
        if not code:
            log(f"  ⚠  Discarding course with empty code")
            continue
        # Word-boundary match: 'cs101' must not match 'cs1010'
        pattern = r'(?<![a-z0-9])' + re.escape(normalize_course_code(code)) + r'(?![a-z0-9])'
        normalized_catalog = normalize_course_code(catalog_text)
        if re.search(pattern, normalized_catalog):
            valid.append(c)
        else:
            log(f"  ⚠  Discarding {c.get('course_code','unknown')} — code not found in catalog summary")
    return valid

def _handle_api_error(e: Exception, key_name: str, attempt: int, log) -> str:
    err = str(e).lower()
    if any(x in err for x in ["quota", "429", "exhausted", "rate_exhausted", "resource_exhausted"]):
        log(f"  ⚠  Quota/rate-limit on {key_name} (attempt {attempt}). Rotating…")
        pool.rotate(mark_bad=True)
        time.sleep(3)
        return "quota"
    elif any(x in err for x in ["api key", "invalid", "unauthorized", "403"]):
        log(f"  ✗ Auth error on {key_name}: {e}. Rotating…")
        pool.rotate(mark_bad=True)
        time.sleep(1)
        return "auth"
    else:
        log(f"  ✗ Error on attempt {attempt} ({key_name}): {e}")
        pool.rotate(mark_bad=False)
        time.sleep(2)
        return "other"

def recommend(
    catalog_uri, catalog_mime, student_md, term,
    log=print, student_label="student",
    condensed_catalog_text=None, catalog_path=None,
    validate_course_codes=False,
    df=None,
) -> list:
    """Run the full 4-step chain-of-thought recommendation pipeline for one student.

    Args:
        catalog_uri:            Gemini Files API URI of the uploaded catalog PDF.
        catalog_mime:           MIME type of the catalog file.
        student_md:             Student transcript rendered as a Markdown table string.
        term:                   Upcoming term label (e.g. "Fall 2025", "Summer").
        log:                    Callable used for progress messages (default: print).
        student_label:          Human-readable name/ID used in log messages.
        condensed_catalog_text: Optional pre-summarised catalog text (Step 0 output).
        catalog_path:           Local path to the catalog PDF (used for re-upload on 403).
        validate_course_codes:  If True, discard recommendations not found in catalog summary.
        df:                     Optional raw student DataFrame — used for Python-side
                                course-code pre-extraction (supplements Step 1.5).

    Returns:
        List of validated course dicts.
    """
    max_attempts = pool.key_count + 1
    reupload_tried = False

    # ── Python pre-extraction of course codes from the DataFrame ─────────────
    # This gives us a deterministic list of codes visible in the transcript,
    # which we merge with the model's Step 1.5 output.
    python_course_codes: list = _extract_course_codes_from_df(df) if df is not None else []
    if python_course_codes:
        log(f"  🔎 Pre-scanned {len(python_course_codes)} course code(s) from transcript.")

    def _call_with_retry(conversation, expect_json, step_name):
        nonlocal catalog_uri, catalog_mime, reupload_tried
        for attempt in range(1, max_attempts + 1):
            key_name = pool.current_name()
            log(f"    [{step_name}] attempt {attempt}/{max_attempts} — key: {key_name}…")
            client = _new_client()
            try:
                return _call_gemini(client, catalog_uri, catalog_mime,
                                    conversation, expect_json,
                                    condensed_catalog_text=condensed_catalog_text)
            except json.JSONDecodeError as e:
                log(f"    [{step_name}] Invalid JSON: {e}")
                pool.rotate(mark_bad=False)
                time.sleep(1)
            except Exception as e:
                err = str(e).lower()
                if not reupload_tried and catalog_path and (
                    "permission denied" in err
                    or "permission_denied" in err
                    or "do not have permission" in err
                    or ("403" in err and "file" in err)
                ):
                    log(f"  ⚠  File access denied for catalog. Re‑uploading local copy and retrying…")
                    try:
                        catalog_uri, catalog_mime = get_or_upload_catalog(
                            catalog_path, log=log, force=True)
                        reupload_tried = True
                        continue
                    except Exception as upload_exc:
                        log(f"  ✗ Re‑upload failed: {upload_exc}")
                _handle_api_error(e, key_name, attempt, log)
            finally:
                _close_client(client)
        raise RuntimeError(f"All {pool.key_count} key(s) failed on {step_name} for {student_label}.")

    # ── Step 1: Free-text transcript analysis ────────────────────────────────
    log(f"  📋 Step 1/4 — Analysing transcript for {student_label}…")
    s1_user  = _prompt_step1(student_md, term)
    s1_reply = _call_with_retry([{"role": "user", "text": s1_user}], False, "Step1/Transcript")
    log(f"  ✓ Step 1 complete ({len(s1_reply)} chars).")

    # ── Step 1.5: Structured JSON extraction (CGPA, credit limit, passed courses)
    log(f"  🔢 Step 1.5/4 — Extracting structured facts…")
    s1_5_user = _prompt_step1_5()
    # Token optimisation: only send the Step 1 exchange (not student_md again)
    # The model has just produced s1_reply so it has full context.
    raw_facts = _call_with_retry([
        {"role": "user",  "text": s1_user},
        {"role": "model", "text": s1_reply},
        {"role": "user",  "text": s1_5_user},
    ], True, "Step1.5/Facts")
    student_facts = _parse_student_facts(raw_facts, student_label)

    # Merge Python-extracted codes into the model's passed_courses list
    if python_course_codes:
        existing_norm = {normalize_course_code(c) for c in student_facts["passed_courses"]}
        for code in python_course_codes:
            if normalize_course_code(code) not in existing_norm:
                student_facts["passed_courses"].append(code)
                existing_norm.add(normalize_course_code(code))

    cgpa_str  = f"{student_facts['cgpa']:.2f}" if student_facts['cgpa'] is not None else "N/A"
    limit_str = str(student_facts['credit_limit']) if student_facts['credit_limit'] is not None else "N/A"
    log(f"  ✓ Step 1.5 complete — CGPA: {cgpa_str}, Credit limit: {limit_str} hrs, "
        f"Passed courses: {len(student_facts['passed_courses'])}.")

    # ── Step 2: Eligible course filtering ────────────────────────────────────
    log(f"  📖 Step 2/4 — Looking up eligible courses…")
    s2_user  = _prompt_step2(term, student_facts)
    # Token optimisation: pass a compact summary of Step 1 rather than full text.
    # We still include the raw s1_reply so the model can reference it, but we
    # do NOT re-send the full student_md (already embedded in s1_user at Step 1).
    s2_reply = _call_with_retry([
        {"role": "user",  "text": s1_user},
        {"role": "model", "text": s1_reply},
        {"role": "user",  "text": s1_5_user},
        {"role": "model", "text": raw_facts},
        {"role": "user",  "text": s2_user},
    ], False, "Step2/Catalog")
    log(f"  ✓ Step 2 complete ({len(s2_reply)} chars).")

    # ── Step 3: JSON output ───────────────────────────────────────────────────
    log(f"  🎯 Step 3/4 — Generating JSON…")
    s3_user  = _prompt_step3(term, student_facts)
    # Token optimisation: summarise Steps 1+1.5 into the facts JSON rather than
    # re-sending the full free-text analysis. Step 2's reply is still included
    # verbatim so the model knows exactly which courses are eligible.
    raw_json = _call_with_retry([
        {"role": "user",  "text": s1_5_user},
        {"role": "model", "text": raw_facts},
        {"role": "user",  "text": s2_user},
        {"role": "model", "text": s2_reply},
        {"role": "user",  "text": s3_user},
    ], True, "Step3/JSON")

    cleaned = _strip_json(raw_json)
    try:
        courses = json.loads(cleaned)
    except json.JSONDecodeError as e:
        log(f"  ✗ Step 3 invalid JSON: {e}")
        raise RuntimeError(f"Step 3 JSON parse failed for {student_label}: {e}")
    if not isinstance(courses, list):
        raise RuntimeError(f"Step 3 returned {type(courses).__name__} instead of a list.")
    courses = _validate_course_list(courses, student_label)

    # ── Layer 3: Python-enforced passed-course deduplication ─────────────────
    passed_norm = {normalize_course_code(c) for c in student_facts["passed_courses"]}
    if passed_norm:
        before_dedup = len(courses)
        courses = [
            c for c in courses
            if normalize_course_code(c["course_code"]) not in passed_norm
        ]
        removed = before_dedup - len(courses)
        if removed:
            log(f"  ✓ Python filter removed {removed} already-passed course(s) the model included.")

    # ── Layer 3: Python-enforced credit cap ──────────────────────────────────
    credit_limit = student_facts.get("credit_limit")
    if credit_limit is not None and credit_limit > 0:
        total = sum(c["credits"] for c in courses)
        if total > credit_limit:
            log(f"  ⚠  Model returned {total} hrs which exceeds limit of {credit_limit} hrs. Trimming…")
            # Sort: mandatory-looking courses first (heuristic: no "elective" in justification),
            # then by position in list (earlier = earlier in study plan = higher priority).
            def _priority(c):
                j = c.get("justification", "").lower()
                is_elective = any(w in j for w in ("elective", "optional", "اختياري"))
                return (1 if is_elective else 0)
            courses.sort(key=_priority)
            kept, running = [], 0
            for c in courses:
                if running + c["credits"] <= credit_limit:
                    kept.append(c)
                    running += c["credits"]
            log(f"  ✓ Trimmed to {len(kept)} course(s) totalling {running} hrs (limit: {credit_limit} hrs).")
            courses = kept

    if validate_course_codes and condensed_catalog_text:
        before = len(courses)
        courses = _validate_against_catalog(courses, condensed_catalog_text, log=log)
        after = len(courses)
        if before != after:
            log(f"  ✓ Removed {before - after} course(s) not found in catalog summary.")

    log(f"  ✓ {len(courses)} recommendation(s) for {student_label}.")
    return courses

def recommend_students(
    catalog_uri, catalog_mime, targets, term,
    log=print, condensed_catalog_text=None, catalog_path=None,
    labels=None, validate_course_codes=False, progress=None,
) -> list:
    total = len(targets)
    if labels is not None and len(labels) != total:
        raise ValueError("labels list must have the same length as targets")
    results = []
    log(f"\n{'─'*52}")
    log(f"  Starting {total} student recommendation(s) | term: {term}")
    log(f"{'─'*52}")
    for i, (sheet, df) in enumerate(targets):
        if progress:
            progress(i, total)
        label = labels[i] if labels else _student_label(df, sheet)
        log_label = f"{sheet} | {label}"
        log(f"\n[{i+1}/{total}] {log_label}")
        try:
            courses = recommend(catalog_uri, catalog_mime,
                                df.to_markdown(index=False), term, log, log_label,
                                condensed_catalog_text, catalog_path,
                                validate_course_codes=validate_course_codes,
                                df=df)
            if labels:
                results.append({
                    "identifier": f"{sheet} | {label}",
                    "sheet": sheet,
                    "term": term,
                    "recommended_courses": courses,
                })
            else:
                sid, name_en, name_ar = extract_identity(df)
                results.append({
                    "student_id": sid,
                    "name_english": name_en,
                    "name_arabic": name_ar,
                    "sheet": sheet,
                    "term": term,
                    "recommended_courses": courses,
                })
        except Exception as e:
            log(f"  ✗ FAILED: {e}")
            results.append({"sheet": sheet, "error": str(e)})
        if i < total - 1:
            log(f"  ⏸  Pausing {REQUEST_DELAY}s…")
            time.sleep(REQUEST_DELAY)
    if progress:
        progress(total, total)
    ok = sum(1 for r in results if "error" not in r)
    log(f"\n✓ {ok}/{total} succeeded.\n")
    return results

def batch_all(
    catalog_uri, catalog_mime, sheets, term,
    log=print, progress=None,
    summary_path="batch_summary.json", detailed_path="batch_detailed.json",
    condensed_catalog_text=None, catalog_path=None,
    validate_course_codes=False,
) -> tuple:
    targets = list(sheets.items())
    total   = len(targets)
    results = recommend_students(
        catalog_uri, catalog_mime, targets, term,
        log=log, condensed_catalog_text=condensed_catalog_text,
        catalog_path=catalog_path, labels=None,
        validate_course_codes=validate_course_codes,
        progress=progress)
    summary = []
    detailed = []
    for r in results:
        if "error" in r:
            summary.append(r)
            detailed.append(r)
        else:
            summary.append({
                "sheet": r["sheet"],
                "recommended_courses": [c.get("course_title", "?") for c in r["recommended_courses"]],
            })
            detailed.append(r)
    ok = sum(1 for r in summary if "error" not in r)
    log(f"\n{'═'*52}")
    log(f"  BATCH COMPLETE — ✓ {ok}/{total} succeeded")
    _save_json(summary,  summary_path)
    _save_json(detailed, detailed_path)
    log(f"  Summary  → {summary_path}")
    log(f"  Detailed → {detailed_path}")
    log(f"{'═'*52}\n")
    return summary, detailed

# ── Step 0: Catalog Summarisation ─────────────────────────────────────────────

def compress_catalog(catalog_uri, catalog_mime, log=print, output_path=CATALOG_SUMMARY_CACHE) -> str:
    prompt = """You are provided with a university catalog PDF. Your task is to create a VERY DETAILED, self‑contained text summary that includes EVERY piece of information needed for academic advising. Be exhaustive; length is not an issue.

Include:
- All grading rules (passing grades, GPA calculation, repeat policies, probation rules).
- Credit limits per semester (normal and summer).
- A COMPLETE list of ALL courses: code, title, credit hours, semester(s) offered, prerequisites (exact codes), and any special notes (e.g. “non‑credit”, “field training”, “0 credits”, “does not count toward GPA”, “summer only”).
- The recommended study plan by semester, exactly as it appears, with every course.
- Any non‑credit or odd‑credit requirements (internships, field training, orientation) and how they are treated.
- All other regulations that could affect course selection (maximum repeat limits, minimum credits, etc.).

Write in English, use structured sections, and be as detailed as necessary. Output as plain text, no markdown or JSON.
"""
    max_attempts = pool.key_count + 1
    for attempt in range(1, max_attempts + 1):
        key_name = pool.current_name()
        log(f"📝 Requesting detailed catalog summary from Gemini (attempt {attempt}/{max_attempts} — key: {key_name}) …")
        client = _new_client()
        try:
            contents = [
                types.Content(role="user", parts=[
                    types.Part.from_uri(file_uri=catalog_uri, mime_type=catalog_mime),
                    types.Part.from_text(text=prompt),
                ])
            ]
            cfg = types.GenerateContentConfig(temperature=0.1, top_p=0.9)
            resp = client.models.generate_content(model=MODEL_ID, contents=contents, config=cfg)
            summary = resp.text.strip()
            
            with open(output_path, "w", encoding="utf-8") as f:
                f.write(summary)
            log(f"✓ Catalog summary saved to {output_path} ({len(summary)} chars)")
            return summary
        except Exception as e:
            _handle_api_error(e, key_name, attempt, log)
        finally:
            _close_client(client)
            
    raise RuntimeError(f"Failed to summarise catalog after {max_attempts} attempts.")

def _save_json(data, path):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def save_results(data: list, path: str):
    _save_json(data, path)