# Academic Advisor AI — Complete Guide

**An intelligent course recommendation system powered by Google Gemini AI that analyzes student transcripts and university catalogs to provide personalized degree completion plans.**

> Built with **Python**, **Tkinter GUI**, and **Google GenAI API** (Gemini Flash)

---

## 📋 Table of Contents
- [Features](#features)
- [Quick Start](#quick-start)
- [Project Structure](#project-structure)
- [Usage Guide](#usage-guide)
- [JSON Output Formats](#json-output-formats)
- [How It Works](#how-it-works)
- [Accuracy Improvements](#accuracy-improvements)
- [Performance & Scalability](#performance--scalability)
- [Future Roadmap](#future-roadmap)

---

## ✨ Features

### 🔧 Setup Tab
- **API Key Management**: Paste API keys comma-separated, or set environment variables (`GEMINI_KEY_1`, `GEMINI_KEY_2`, etc.)
- **Auto Key Rotation**: Keys rotate automatically on quota/rate-limit (429) errors
- **Drag & Drop**: Drop catalog PDF or Excel files directly onto input fields (requires `tkinterdnd2`)
- **Smart Caching**: Catalog uploaded once, cached in `catalog_cache.json`. URI verified on every run
- **File Format Support**: Automatic detection for `.xls` (xlrd) and `.xlsx` (openpyxl)

### 🔍 Student Search Tab
- **Multi-Query Search**: Search by student ID, English name, or Arabic name
- **Comma-Separated Queries**: `"123, Ahmed, محمد"` finds all three in one go
- **Multi-Select Results**: Ctrl+click to select multiple students, then:
  - **Recommend Selected** — only highlighted students
  - **Recommend All Matches** — every search result
- **Flexible Output**:
  - **Summary JSON**: Course titles only
  - **Detailed JSON**: Full justification + catalog proof

### 📊 Batch All Students Tab
- **Sequential Processing**: Every sheet processed with 1.5s delays (respects Gemini rate limits)
- **Live Progress**: Real-time progress bar and per-student detailed logging
- **Auto-Save**: Results automatically saved as `batch_summary.json` and `batch_detailed.json`
- **Error Resilience**: Continues processing even if individual students fail

### 🤖 AI Analysis (3-Step Chain-of-Thought)
1. **Step 1**: Transcript analysis → prerequisites, GPA, credit limits
2. **Step 2**: Catalog lookup → filter by prerequisites, availability, credits
3. **Step 3**: JSON generation → structured course recommendations

---

## 🚀 Quick Start

### Prerequisites
- Python 3.8+
- Google Gemini API key(s) — get one at [ai.google.dev](https://ai.google.dev)

### Installation

```bash
pip install -r requirements.txt
# Optional: drag-and-drop support
pip install tkinterdnd2
```

### Configuration

**Option 1: Environment Variables** (recommended)
```bash
export GEMINI_KEY_1="your_first_key"
export GEMINI_KEY_2="your_second_key"
python advisor_gui.py
```

**Option 2: .env File** (auto-created by app)
```bash
python advisor_gui.py
# Enter keys in the Setup tab; they'll be saved to .env
```

**Option 3: Runtime Paste**
```bash
python advisor_gui.py
# Paste comma-separated keys in Setup tab
```

### Run

```bash
python advisor_gui.py
```

A Tkinter window will open with three tabs: **Setup**, **Search**, and **Batch**.

---

## 📁 Project Structure

| File | Purpose |
|---|---|
| `advisor_core.py` | **All business logic** — API keys, catalog upload, Excel parsing, AI calls, batch processing, JSON validation |
| `advisor_gui.py` | **Tkinter GUI only** — imports `advisor_core`, no business logic |
| `catalog_cache.json` | Auto-created; stores catalog file name and URI to skip re-upload |
| `catalog_summary.txt` | Optional; compressed catalog text summary (reduces token usage) |
| `.env` | Auto-created; stores API keys as `GEMINI_KEY_1`, `GEMINI_KEY_2`, etc. |
| `requirements.txt` | Python dependencies |
| `batch_summary.json` | Auto-saved; course titles only for all students |
| `batch_detailed.json` | Auto-saved; full recommendations with justifications |

---

## 📖 Usage Guide

### Step 1: Upload Catalog

1. Go to **Setup** tab
2. Click **Browse Catalog** or **drag-and-drop** a PDF
3. Click **Verify Catalog**
   - First time: uploads to Gemini Files API (~10–30s depending on PDF size)
   - Subsequent runs: uses cached URI if still ACTIVE
4. If the catalog expires, the app re-uploads automatically

### Step 2: Load Student Excel

1. Click **Browse Student Data** or **drag-and-drop** an Excel file
2. Click **Load File**
3. All sheets are auto-detected; choose which to process

### Step 3: Search or Batch

#### Option A: Search Individual Students
1. Go to **Search** tab
2. Enter query: `123` (ID), `Ahmed` (English), or `محمد` (Arabic)
3. Click **Search**
4. Multi-select results with Ctrl+click
5. Click **Recommend Selected** or **Recommend All Matches**
6. Choose output: **Summary** or **Detailed**

#### Option B: Batch All Students
1. Go to **Batch** tab
2. Select all sheets to process
3. Click **Start Batch**
4. Watch the progress bar and log
5. Results auto-saved when complete

---

## 💾 JSON Output Formats

### Summary Format
Minimal—titles only, suitable for quick reports:

```json
[
  {
    "student_id": "12345",
    "name_english": "Ahmed Ali",
    "name_arabic": "أحمد علي",
    "sheet": "Fall2025",
    "term": "Fall 2025",
    "recommended_courses": [
      {
        "course_code": "MATH201",
        "course_title": "Calculus II",
        "course_title_in_arabic": "حساب التفاضل والتكامل ٢",
        "credits": 3,
        "justification": "Prerequisites: MATH101 ✓ (grade A, Spring 2024)",
        "catalog_availability_proof": "MATH201 offered Fall and Spring, as per catalog p.47"
      }
    ]
  }
]
```

### Detailed Format
Full justification and proof—ideal for advisor review:

```json
[
  {
    "student_id": "12345",
    "name_english": "Ahmed Ali",
    "name_arabic": "أحمد علي",
    "sheet": "Fall2025",
    "term": "Fall 2025",
    "recommended_courses": [
      {
        "course_code": "MATH201",
        "course_title": "Calculus II",
        "course_title_in_arabic": "حساب التفاضل والتكامل ٢",
        "credits": 3,
        "justification": "Student completed MATH101 with grade A in Spring 2024. No failed attempts. Prerequisites fully satisfied.",
        "catalog_availability_proof": "Study plan table (page 47): 'MATH201 (3 cr) — offered Fall & Spring'"
      },
      {
        "course_code": "PHYS102",
        "course_title": "Physics II (Electromagnetism)",
        "course_title_in_arabic": "فيزياء ٢ (الكهرومغناطيسية)",
        "credits": 4,
        "justification": "PHYS101 passed with B (Fall 2023). No co-requisites required. Student has 7 credits capacity left.",
        "catalog_availability_proof": "Study plan table (page 48): 'PHYS102 (4 cr) — offered Fall, Spring & Summer'"
      }
    ]
  }
]
```

---

## 🧠 How It Works

### Architecture: Core + GUI Separation
- **`advisor_core.py`**: 920 lines of reusable, testable logic
  - File I/O, environment variable management
  - Catalog upload & caching (Gemini Files API)
  - Excel parsing (pandas, xlrd, openpyxl)
  - Arabic text normalization (NFC, diacritic-aware)
  - Chain-of-thought prompting (3-step)
  - JSON validation & error handling
  
- **`advisor_gui.py`**: Tkinter UI (no business logic)
  - Imports `advisor_core` functions
  - Handles button clicks, file selection, text display

### AI Analysis Flow

#### Step 1: Transcript Analysis
The model:
1. Extracts student ID, names (English/Arabic), major, level
2. Consolidates passed courses (uses best grade if repeated)
3. Lists failed courses and non-credit requirements
4. Calculates current CGPA (using catalog grading scale)
5. Determines credit limit for the target term
6. Flags special considerations (near graduation? blocking prerequisites?)

**Input**: Student transcript (markdown table)  
**Output**: Structured analysis (plain English text, no JSON yet)

#### Step 2: Catalog Filtering
The model:
1. For each remaining required course:
   - Checks prerequisites in catalog
   - Verifies student has passed all prerequisites
   - Confirms course is offered in target term
   - Cites exact catalog page/text
2. Outputs a list of eligible courses with proof

**Input**: Step 1 analysis + catalog PDF  
**Output**: Filtered course list with catalog citations

#### Step 3: JSON Generation
The model:
1. Applies credit limits from Step 1
2. Prioritizes mandatory courses first, then sequence order
3. Outputs strict JSON with:
   - `course_code`, `course_title`, `course_title_in_arabic`
   - `credits`, `justification`, `catalog_availability_proof`

**Input**: Step 1 + Step 2 + JSON schema requirement  
**Output**: Valid JSON array (parsed by Python)

### Error Handling & Robustness

- **Key Rotation**: On 429 (rate limit) or auth error, rotates to the next key
- **Catalog Re-upload**: If file permission denied, re-uploads catalog with current key
- **Arabic Normalization**: Handles variations in Arabic script (ا/أ/إ/آ, ي/ى/ئ) + diacritics
- **Excel Engine Auto-Detect**: Uses `xlrd` for `.xls`, `openpyxl` for `.xlsx`
- **JSON Validation**: Type-checks all fields; raises RuntimeError with clear message if invalid
- **Retry Logic**: Attempts up to (key_count + 1) times per step

---

## 🎯 Accuracy Improvements

### Quick Wins (Phase 1)
1. **Few-Shot Prompts** — Add 1–2 worked examples in the prompt. Biggest accuracy boost.
2. **Chain-of-Thought** — 3-step reasoning (Step 1: analyze, Step 2: filter, Step 3: format) reduces errors significantly.
3. **Two-Pass Validation** — After Step 3, send: "Review your recommendations against catalog rules and flag violations."
4. **Cite Page Numbers** — Force model to quote exact catalog pages/sections.
5. **Response Schema Enforcement** — Use Gemini's `response_schema` parameter (JSON Schema object) instead of just MIME type.

### Advanced (Phase 2–3)
6. **RAG (Retrieval-Augmented Generation)**
   - Chunk catalog PDF into sections (prerequisites, study plans, rules, etc.)
   - Embed with text model, store in ChromaDB or Pinecone
   - Send only top-k relevant chunks per student (reduces noise & token usage)

7. **Transcript Normalization**
   - Normalize grades: A, 4.0, "Pass", "ممتاز" → unified scale
   - Normalize course codes: 'MATH-101', 'MATH 101', 'MATH101' → 'MATH101'
   - Inconsistent formats are a leading cause of model errors

8. **Prerequisite Graph Validation**
   - Parse catalog prerequisite table once (offline) into Python dict
   - After model responds, re-check each course_code against dict as hard filter

---

## 📊 Performance & Scalability

### Timing Breakdown

| Operation | Time | Notes |
|---|---|---|
| Load Excel (100 rows) | ~100ms | depends on engine (xlrd vs openpyxl) |
| Search (100 columns) | ~50ms | normalized substring match |
| **Single Student Recommendation** | **45–60s** | 3 Gemini API calls (~15–20s each) + network latency |
| Batch (10 students) | ~7–8 min | (45s × 10) + (1.5s × 9 delays) |
| **Batch (100 students)** | **~76–90 min** | (45s × 100) + (1.5s × 99 delays) — **requires async for faster processing** |
| Catalog upload (10MB PDF) | ~10–30s | depends on file size + Gemini load |
| Catalog caching (on hit) | ~0ms | instant URI lookup |

### Scalability Notes
- **Sequential processing** is reliable but slow for large batches
- **Future optimization** (Phase 2): Async batch with `asyncio.Semaphore` (3–5 concurrent requests) would reduce 100-student batch to ~15–20 minutes
- **Current recommendation**: For >50 students, consider running overnight or in batches of 25–30

---

## 📚 Dependencies

See `requirements.txt`:

```
google-genai       # Google Gemini API
pandas             # Excel parsing
openpyxl           # .xlsx support
xlrd               # .xls support
tabulate           # Pretty table display
tkinterdnd2        # Drag-and-drop (optional)
```

### Library Versions & Documentation
- **google-genai** ([docs](https://ai.google.dev/api/python)): Latest Python SDK for Gemini
- **pandas** ([docs](https://pandas.pydata.org/)): DataFrame manipulation, Excel I/O
- **openpyxl** ([docs](https://openpyxl.readthedocs.io/)): `.xlsx` read/write
- **xlrd** ([docs](https://xlrd.readthedocs.io/)): Legacy `.xls` support
- **tkinterdnd2** ([repo](https://github.com/pmgagne/tkinterdnd2)): Drag-and-drop for Tkinter (optional)

---

## 🛣️ Future Roadmap

### Phase 1 — Stability (now → 1 month)
- [ ] Two-pass validation prompt (review recommendations)
- [ ] `response_schema` enforcement (strict JSON typing)
- [ ] Unit tests for `advisor_core.py`
- [ ] `config.yaml` for all constants (MODEL_ID, delays, paths)

### Phase 2 — Scale (1 → 3 months)
- [ ] Local web app (FastAPI + React/Vue frontend)
- [ ] RAG with ChromaDB (chunk catalog, embed, query)
- [ ] Async batch with `asyncio` + `asyncio.Semaphore` (3–5 concurrent requests per key)
- [ ] SQLite recommendation history (track changes, re-run, query past results)
- [ ] Prerequisite graph visualization (NetworkX + matplotlib)

### Phase 3 — Deployment (3 → 6 months)
- [ ] Docker container (for easy deployment)
- [ ] Role-based access control (advisor vs. student)
- [ ] Student Information System (SIS) API integration
- [ ] Automated email delivery with PDF reports
- [ ] Scheduled batch runs (e.g., cron job for end-of-term processing)

### Phase 4 — Intelligence (6+ months)
- [ ] Fine-tune smaller model on past advisor decisions
- [ ] Graduation timeline prediction
- [ ] At-risk student detection (falling behind on required courses)
- [ ] Arabic/English UI toggle
- [ ] Course load warnings & conflict detection
- [ ] Waitlist awareness (historically full courses)

---

## 🎓 Other Useful Features (Backlog)

| Feature | Value |
|---|---|
| Prerequisite waiver tracking | Record and honor waived prerequisites |
| Transfer credit mapping | Map transferred courses to local equivalents |
| Course load warnings | Alert if schedule is unusually heavy |
| Waitlist awareness | Flag courses that historically fill up early |
| Notes/comments field | Advisors annotate recommendations before saving |
| Diff view | Show what changed between two runs for the same student |
| Dark/light mode toggle | GUI preference |
| Keyboard shortcuts | Enter to search, Ctrl+R to recommend, Ctrl+B for batch |

---

## 🔐 API Key Management Best Practices

1. **Use Environment Variables**: Safest approach; keys never stored in code
2. **Rotate Keys**: The app auto-rotates on quota errors; add backups in `GEMINI_KEY_2`, etc.
3. **Monitor Usage**: Check [Google AI Studio](https://aistudio.google.com/) for quota usage
4. **Regenerate Keys**: If a key is exposed, regenerate immediately
5. **.env in .gitignore**: Never commit `.env` to version control

---

## 🐛 Troubleshooting

| Issue | Solution |
|---|---|
| "Invalid API key" | Check `GEMINI_KEY_1` in environment or `.env`; regenerate if needed |
| "Rate limit exceeded" | App auto-rotates keys; add more keys to `GEMINI_KEY_2`, `GEMINI_KEY_3`, etc. |
| "Catalog upload fails" | Check PDF is not corrupted; try with a smaller PDF first |
| "Excel file not found" | Use absolute path or place file in same directory as `advisor_gui.py` |
| "Arabic names not matching" | Enable normalization (default on); check Excel column names contain "arabic" or "عربي" |
| "Drag-and-drop doesn't work" | Install `tkinterdnd2`: `pip install tkinterdnd2` |
| "Batch takes too long" | Normal behavior (45–60s per student). For faster processing, upgrade to Phase 2 (async) |

---

## 📄 License

This project is provided as-is for educational and internal use.

---

## 💡 Questions or Contributions?

For bug reports, feature requests, or improvements, open a GitHub issue or submit a pull request.

See [CONTRIBUTING.md](CONTRIBUTING.md) for development guidelines and [ARCHITECTURE.md](ARCHITECTURE.md) for technical details.

**Happy advising! 🎓**
