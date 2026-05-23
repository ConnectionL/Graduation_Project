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
"""

import os, time, json, re, glob, unicodedata
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
    def __init__(self, keys: list):
        self._keys  = list(keys)
        self._index = 0
        self._bad   = set()
    def current(self) -> str:
        return self._keys[self._index % len(self._keys)]
    def current_name(self) -> str:
        idx = self._index % len(self._keys)
        return f"GEMINI_KEY_{idx + 1}"
    def rotate(self, mark_bad: bool = False):
        if mark_bad:
            self._bad.add(self._index % len(self._keys))
        self._index = (self._index + 1) % len(self._keys)
    def get_client(self):
        for _ in range(len(self._keys)):
            idx = self._index % len(self._keys)
            if idx not in self._bad:
                return genai.Client(api_key=self._keys[idx])
            self._index += 1
        self._bad.clear()
        return genai.Client(api_key=self.current())
    def set_keys(self, keys: list, persist: bool = True):
        self._keys  = list(keys)
        self._index = 0
        self._bad   = set()
        if persist:
            save_env_file(keys)
    @property
    def keys(self) -> list:
        return list(self._keys)
    @property
    def key_count(self) -> int:
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
            fobj = client.files.upload(file=path)
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

def _prompt_step2(term: str) -> str:
    return f"""Excellent. Now, using your analysis above and the catalog PDF (or its summary), perform the following for every remaining required course:

1. **Exclude Non‑Standard Courses Immediately**: If a course is listed in the “Non‑Credit & Odd‑Credit Requirements” section of Step 1, or if it is 0 credits, field training, summer‑only, or does not count toward the credit limit, **discard it now**. Do NOT include it in any list. Only consider standard credit‑bearing courses that can be taken in the requested term.
2. **Prerequisite Check**:
   - Locate the course in the catalog.
   - List its prerequisites.
   - Verify that ALL prerequisites appear in your "Passed Courses" list from Step 1. If any prerequisite is missing, discard this course.
3. **Already Passed Check**:
   - If the course (under any normalised form) appears in the "Passed Courses" list from Step 1, **discard it immediately** – the student has already satisfied it.
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

def _prompt_step3(term: str) -> str:
    return f"""Now you have:
- The list of passed courses and the credit limit from Step 1.
- The filtered list of eligible courses from Step 2 (all standard credit‑bearing and offered in {term}).

**Your final task:**
Convert the eligible courses into a JSON array. **Strict rules:**
- Do NOT add any course that did not appear in your Step 2 list.
- Exclude any course that is non‑credit, 0‑credit, field training, summer‑only, or does not count toward credit limit, as already removed in Steps 1‑2.
- Apply the credit limit from Step 1: if the total credits of eligible courses exceed the limit, prioritise mandatory courses first, then earlier in the plan, until the limit is reached.
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
    if not condensed_catalog_text or not courses:
        return courses
    catalog_text = condensed_catalog_text.lower()
    valid = []
    for c in courses:
        code = c.get("course_code", "").lower()
        if code and code in catalog_text:
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
) -> list:
    max_attempts = pool.key_count + 1
    reupload_tried = False

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

    log(f"  📋 Step 1/3 — Analysing transcript for {student_label}…")
    s1_user  = _prompt_step1(student_md, term)
    s1_reply = _call_with_retry([{"role": "user", "text": s1_user}], False, "Step1/Transcript")
    log(f"  ✓ Step 1 complete ({len(s1_reply)} chars).")

    log(f"  📖 Step 2/3 — Looking up eligible courses…")
    s2_user  = _prompt_step2(term)
    s2_reply = _call_with_retry([
        {"role": "user",  "text": s1_user},
        {"role": "model", "text": s1_reply},
        {"role": "user",  "text": s2_user},
    ], False, "Step2/Catalog")
    log(f"  ✓ Step 2 complete ({len(s2_reply)} chars).")

    log(f"  🎯 Step 3/3 — Generating JSON…")
    s3_user  = _prompt_step3(term)
    raw_json = _call_with_retry([
        {"role": "user",  "text": s1_user},
        {"role": "model", "text": s1_reply},
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
    labels=None, validate_course_codes=False,
) -> list:
    total = len(targets)
    if labels is not None and len(labels) != total:
        raise ValueError("labels list must have the same length as targets")
    results = []
    log(f"\n{'─'*52}")
    log(f"  Starting {total} student recommendation(s) | term: {term}")
    log(f"{'─'*52}")
    for i, (sheet, df) in enumerate(targets):
        label = labels[i] if labels else _student_label(df, sheet)
        log_label = f"{sheet} | {label}"
        log(f"\n[{i+1}/{total}] {log_label}")
        try:
            courses = recommend(catalog_uri, catalog_mime,
                                df.to_markdown(index=False), term, log, log_label,
                                condensed_catalog_text, catalog_path,
                                validate_course_codes=validate_course_codes)
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
        if i < total:
            log(f"  ⏸  Pausing {REQUEST_DELAY}s…")
            time.sleep(REQUEST_DELAY)
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
        validate_course_codes=validate_course_codes)
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
    client = _new_client()
    try:
        contents = [
            types.Content(role="user", parts=[
                types.Part.from_uri(file_uri=catalog_uri, mime_type=catalog_mime),
                types.Part.from_text(text=prompt),
            ])
        ]
        cfg = types.GenerateContentConfig(temperature=0.1, top_p=0.9)
        log("📝 Requesting detailed catalog summary from Gemini (may take a minute) …")
        resp = client.models.generate_content(model=MODEL_ID, contents=contents, config=cfg)
        summary = resp.text.strip()
    except Exception as e:
        raise RuntimeError(f"Failed to summarise catalog: {e}")
    finally:
        _close_client(client)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(summary)
    log(f"✓ Catalog summary saved to {output_path} ({len(summary)} chars)")
    return summary

def _save_json(data, path):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def save_results(data: list, path: str):
    _save_json(data, path)