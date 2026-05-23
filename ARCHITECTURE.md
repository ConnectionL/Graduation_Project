# Architecture & Design Document

## Overview

Academic Advisor AI is a **client-server hybrid system** combining:
- **Local application**: Tkinter GUI + Python business logic
- **Cloud backend**: Google Gemini API (file storage + LLM inference)
- **Local caching**: Catalog metadata + Excel student data

---

## System Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                      advisor_gui.py                         │
│                    (Tkinter Frontend)                       │
│  ┌──────────┬──────────┬─────────┐                          │
│  │  Setup   │  Search  │  Batch  │  (3 Tabs)               │
│  └──────────┴──────────┴─────────┘                          │
│         ↓ (imports)                                         │
├─────────────────────────────────────────────────────────────┤
│                   advisor_core.py                           │
│                 (Business Logic)                            │
│  ┌─────────────────────────────────────────────────────────┐│
│  │  Config: MODEL_ID, CATALOG_CACHE_FILE, REQUEST_DELAY  ││
│  ├─────────────────────────────────────────────────────────┤│
│  │  Module 1: .env & API Key Management                   ││
│  │  ├─ load_env_file()                                    ││
│  │  ├─ save_env_file()                                    ││
│  │  ├─ _discover_keys()                                   ││
│  │  └─ KeyPool (rotate on quota/auth errors)              ││
│  ├─────────────────────────────────────────────────────────┤│
│  │  Module 2: File Auto-Detection                         ││
│  │  ├─ auto_detect_catalog()                              ││
│  │  └─ auto_detect_excel()                                ││
│  ├─────────────────────────────────────────────────────────┤│
│  │  Module 3: Catalog Cache Management                    ││
│  │  ├─ _load_cache()                                      ││
│  │  ├─ _save_cache()                                      ││
│  │  ├─ list_cached_catalogs()                             ││
│  │  ├─ delete_cached_catalog()                            ││
│  │  └─ get_or_upload_catalog()  ← Gemini Files API       ││
│  ├─────────────────────────────────────────────────────────┤│
│  │  Module 4: Excel Reading                               ││
│  │  ├─ _engine()         (detect xlrd vs openpyxl)        ││
│  │  └─ read_all_sheets()                                  ││
│  ├─────────────────────────────────────────────────────────┤│
│  │  Module 5: Arabic Text Normalization                   ││
│  │  ├─ _normalize_arabic()  (NFC + substitution)          ││
│  │  └─ _column_has_arabic()                               ││
│  ├─────────────────────────────────────────────────────────┤│
│  │  Module 6: Student Search                              ││
│  │  ├─ _looks_like_id()                                   ││
│  │  ├─ _column_category()     (heuristic classifier)      ││
│  │  ├─ _match_score()         (relevance scoring)         ││
│  │  ├─ _search_sheets()       (internal search)           ││
│  │  ├─ find_students()        (public API)                ││
│  │  ├─ extract_identity()     (get ID + names)            ││
│  │  └─ _student_label()       (human-readable name)       ││
│  ├─────────────────────────────────────────────────────────┤│
│  │  Module 7: Chain-of-Thought Prompting                  ││
│  │  ├─ _call_gemini()         (API wrapper)               ││
│  │  ├─ _prompt_step1()        (transcript analysis)       ││
│  │  ├─ _prompt_step2()        (catalog filtering)         ││
│  │  ├─ _prompt_step3()        (JSON generation)           ││
│  │  ├─ recommend()            (single student)            ││
│  │  └─ recommend_students()   (batch wrapper)             ││
│  ├─────────────────────────────────────────────────────────┤│
│  │  Module 8: JSON Validation                             ││
│  │  ├─ _normalize_text()                                  ││
│  │  ├─ _normalize_credits()                               ││
│  │  ├─ _validate_course_object()                          ││
│  │  ├─ _validate_course_list()                            ││
│  │  └─ _validate_against_catalog()                        ││
│  ├─────────────────────────────────────────────────────────┤│
│  │  Module 9: Error Handling & Retry Logic                ││
│  │  └─ _handle_api_error()    (quota/auth/other)          ││
│  ├─────────────────────────────────────────────────────────┤│
│  │  Module 10: Batch & Output                             ││
│  │  ├─ batch_all()                                        ││
│  │  ├─ compress_catalog()     (optional summarization)    ││
│  │  ├─ _save_json()                                       ││
│  │  └─ save_results()                                     ││
│  └─────────────────────────────────────────────────────────┘│
├─────────────────────────────────────────────────────────────┤
│  Persistent State (Local Filesystem)                        │
│  ├─ .env                    (API keys)                      │
│  ├─ catalog_cache.json      (catalog metadata)              │
│  ├─ catalog_summary.txt     (optional compressed summary)   │
│  ├─ batch_summary.json      (output: titles only)           │
│  └─ batch_detailed.json     (output: full details)          │
└─────────────────────────────────────────────────────────────┘
         ↓ (HTTP/gRPC)
┌─────────────────────────────────────────────────────────────┐
│           Google Gemini API (Cloud)                         │
│  ├─ Files API (catalog upload & storage)                    │
│  │  └─ Upload PDF → stored for up to 2 hours               │
│  │  └─ Reference by URI in requests                        │
│  └─ LLM API (generative_content)                            │
│     └─ Model: gemini-flash-latest                           │
│     └─ Temperature: 0.1 (deterministic)                     │
│     └─ Response MIME: application/json (Step 3 only)        │
└─────────────────────────────────────────────────────────────┘
```

---

## Data Flow Diagrams

### Upload & Verify Catalog
```
User: Browse PDF
  ↓
advisor_gui.py: _open_catalog_dialog()
  ↓
advisor_core.py: get_or_upload_catalog(path, force=False)
  ├─ Check local cache (catalog_cache.json)
  │  ├─ If cached & ACTIVE → return cached URI ✓
  │  └─ If cached & not active → skip to upload
  └─ Upload to Gemini Files API
     ├─ POST /upload → fobj (name, uri, mime_type)
     ├─ Poll until state == "ACTIVE" (up to 30s)
     ├─ Save to catalog_cache.json
     └─ Return (uri, mime_type) → GUI
  ↓
GUI: Display "✓ Catalog ready" + file size
```

### Search Student
```
User: Enter query ("123" or "Ahmed" or "محمد")
  ↓
GUI: _search_students_callback()
  ↓
advisor_core.py: find_students(sheets, query)
  └─ _search_sheets(sheets, query, use_normalisation=True)
     ├─ Detect query type: student_id / name_english / name_arabic
     ├─ For each sheet & column:
     │  ├─ Normalize column values (if Arabic)
     │  ├─ Normalize query (if Arabic)
     │  └─ Substring match + score by relevance
     └─ Return sorted list of (sheet, df) tuples
  ↓
GUI: Display results in listbox (multi-select enabled)
```

### Recommend (Single Student)
```
User: Click "Recommend Selected"
  ↓
GUI: recommend_callback()
  ├─ Extract selected dataframe
  └─ Call advisor_core.recommend(catalog_uri, catalog_mime, student_md, term)
     ↓
     Step 1: Transcript Analysis
     ├─ Create prompt: transcript + catalog reference
     ├─ Call Gemini with catalog_uri (PDF)
     └─ Receive: plain text analysis (prerequisites, GPA, credit limit)
     ↓
     Step 2: Catalog Filtering
     ├─ Create prompt: Step 1 output + catalog reference + "find eligible courses"
     ├─ Call Gemini with catalog_uri (PDF)
     └─ Receive: plain text list (course code, prerequisites met, proof quote)
     ↓
     Step 3: JSON Generation
     ├─ Create prompt: Steps 1–2 + "output as JSON"
     ├─ Call Gemini with response_mime_type="application/json"
     └─ Receive: JSON array [{ course_code, course_title, ..., justification, catalog_availability_proof }, ...]
     ↓
     Validation
     ├─ Parse JSON
     ├─ Validate each field (type, non-empty)
     ├─ Optional: check course codes against catalog summary
     └─ Return: list of course dicts
  ↓
GUI: Display results in table or save to JSON file
```

### Batch All Students
```
User: Select sheets + click "Start Batch"
  ↓
GUI: _batch_all_callback()
  ↓
advisor_core.py: batch_all(catalog_uri, catalog_mime, sheets, term)
  ├─ Convert sheets dict to list of (sheet_name, df) tuples
  ├─ Call recommend_students() for all
  │  └─ For each (sheet, df):
  │     ├─ Call recommend() (same 3-step flow)
  │     ├─ Pause REQUEST_DELAY seconds (1.5s)
  │     └─ Accumulate results
  ├─ Create two output files:
  │  ├─ batch_summary.json   [{ sheet, recommended_courses: [titles_only] }, ...]
  │  └─ batch_detailed.json  [{ sheet, term, recommended_courses: [full_objects] }, ...]
  └─ Return (summary, detailed)
  ↓
GUI: Display completion message + file paths
```

---

## Data Structures

### KeyPool (API Key Rotation)
```python
class KeyPool:
    _keys: list       # ["key1", "key2", "key3"]
    _index: int       # Current pointer
    _bad: set         # Indices of exhausted keys
    
    def current() -> str:        # Return current key
    def rotate(mark_bad):        # Move to next, optionally mark as bad
    def get_client():            # Return genai.Client with next valid key
    def set_keys(keys, persist): # Update keys + save to .env
```

### Catalog Cache (`catalog_cache.json`)
```json
{
  "/absolute/path/to/catalog.pdf": {
    "name": "files/d1b2c3d4e5f6g7h8",
    "uri": "https://generativelanguage.googleapis.com/upload_sessions/...",
    "mime_type": "application/pdf"
  }
}
```

### Course Recommendation Object
```python
{
  "course_code": "MATH201",
  "course_title": "Calculus II",
  "course_title_in_arabic": "حساب التفاضل والتكامل ٢",
  "credits": 3,
  "justification": "Prerequisites satisfied: MATH101 (grade A, Spring 2024)",
  "catalog_availability_proof": "Study plan table (p.47): 'MATH201 offered Fall & Spring'"
}
```

### Batch Summary (`batch_summary.json`)
```json
[
  {
    "sheet": "Fall2025",
    "recommended_courses": ["Calculus II", "Physics I", "Linear Algebra"]
  }
]
```

### Batch Detailed (`batch_detailed.json`)
```json
[
  {
    "student_id": "12345",
    "name_english": "Ahmed Ali",
    "name_arabic": "أحمد علي",
    "sheet": "Fall2025",
    "term": "Fall 2025",
    "recommended_courses": [
      { "course_code": "MATH201", "course_title": "Calculus II", ... },
      { ... }
    ]
  }
]
```

---

## Key Design Decisions

### 1. **Core + GUI Separation**
- **Why**: `advisor_core.py` is reusable (tests, CLI, future web app), while `advisor_gui.py` is Tkinter-specific
- **Benefit**: Easy to migrate to FastAPI without rewriting logic

### 2. **3-Step Chain-of-Thought**
- **Why**: Breaking into steps improves model accuracy (avoids hallucination at each stage)
- **Trade-off**: 3 API calls per student (vs. 1) but much higher quality

### 3. **KeyPool Rotation**
- **Why**: Gemini has per-key rate limits; multiple keys allow continuous operation
- **Benefit**: Auto-recovery from quota exhaustion without user intervention

### 4. **Catalog Caching**
- **Why**: Uploading large PDFs is slow (~10–30s); caching skips re-upload
- **Trade-off**: Catalog must be re-uploaded if URI expires (2 hours) or on permission error

### 5. **Arabic Normalization (NFC + Substitution)**
- **Why**: Arabic script has multiple representations (ا/أ/إ/آ, ي/ى/ئ); search must be flexible
- **Benefit**: One search finds all variations without separate logic per variant

### 6. **JSON Validation Before Return**
- **Why**: Gemini can produce malformed JSON; validating early catches errors
- **Benefit**: Guarantees downstream consumers always get valid, typed data

### 7. **Request Delay in Batch**
- **Why**: Gemini enforces rate limits even with multiple keys; 1.5s delay prevents 429 errors
- **Trade-off**: Batch of 100 students takes ~2.5 minutes (but reliable)

---

## Error Handling Strategy

### Tier 1: API Errors
```
genai.APIError
├─ 429 (Rate Limit)     → Rotate key, wait 3s, retry
├─ 403 (Permission)     → Re-upload catalog, retry
└─ 401 (Auth)           → Rotate key, wait 1s, retry
```

### Tier 2: Data Errors
```
json.JSONDecodeError    → Log + raise RuntimeError
ValueError (Excel)      → Log + skip sheet
TypeError (transcript)  → Log + skip student
```

### Tier 3: File Errors
```
FileNotFoundError       → Suggest Browse / drag-and-drop
IOError (.env read)     → Ignore; continue with env vars
```

---

## Performance Characteristics

| Operation | Time | Notes |
|---|---|---|
| Load Excel (100 rows) | ~100ms | depends on engine (xlrd vs openpyxl) |
| Search (100 columns) | ~50ms | normalized substring match |
| Recommend (single) | ~30–60s | 3 Gemini calls + waiting |
| Batch (100 students) | ~2.5min | 100 × 30s + 1.5s delays |
| Catalog upload (10MB PDF) | ~10–30s | depends on file size + Gemini load |
| Catalog caching (on hit) | ~0ms | instant URI lookup |

---

## Security & Privacy

### API Keys
- **Storage**: `.env` (local, excluded from git)
- **Transmission**: HTTPS only to Gemini API
- **Rotation**: Auto-rotate on error to minimize exposure of exhausted key

### Data
- **Transcripts**: Sent to Gemini (cloud) only for processing
- **Output**: Stored locally as JSON (not encrypted)
- **Catalog PDF**: Uploaded once, cached URI reused (expires after 2 hours)

### Recommendations
- [ ] Encrypt `.env` at rest
- [ ] Use short-lived API keys (rotate weekly)
- [ ] Audit Gemini audit logs for unauthorized access

---

## Testing Strategy

### Unit Tests
```python
# tests/test_advisor_core.py
- test_normalize_arabic()           → verify diacritic removal
- test_column_category()            → verify heuristic classifier
- test_find_students()              → verify search accuracy
- test_validate_course_object()     → verify JSON schema validation
```

### Integration Tests
```python
# tests/test_integration.py
- test_end_to_end_single_student()  → full recommend() flow
- test_batch_with_multiple_sheets() → batch_all() with error handling
- test_catalog_cache_hit_miss()     → cache verification flow
```

### Manual Tests (before release)
- [ ] Drag-and-drop PDF (macOS, Windows, Linux)
- [ ] Search with Arabic query (multiple variations)
- [ ] Batch all students (with key rotation)
- [ ] Verify JSON output format

---

## Future Improvements

### Short Term (Phase 1)
- [ ] Config file (`config.yaml`) externalize constants
- [ ] Response schema enforcement (strict JSON typing at API level)
- [ ] Unit test suite

### Medium Term (Phase 2)
- [ ] Web UI (FastAPI + React)
- [ ] RAG with ChromaDB (chunk catalog, embed, retrieve)
- [ ] SQLite history (track sessions, re-run, diff)
- [ ] Async batch with `asyncio.Semaphore` (3–5 concurrent)

### Long Term (Phase 3+)
- [ ] Fine-tuning on past advisor decisions
- [ ] Graduation timeline prediction
- [ ] At-risk student detection
- [ ] SIS API integration
- [ ] Automated email + PDF reports

---

## References

- [Google Gemini API Docs](https://ai.google.dev/api/python)
- [Gemini Files API](https://ai.google.dev/api/files)
- [Pandas Documentation](https://pandas.pydata.org/)
- [Tkinter Guide](https://docs.python.org/3/library/tkinter.html)
