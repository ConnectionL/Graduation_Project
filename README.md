# Academic Advisor AI — Complete Guide

## Quick Start

```bash
pip install google-genai pandas openpyxl xlrd tabulate
pip install tkinterdnd2   # optional — drag-and-drop support

export GEMINI_KEY_1="your_first_key"
export GEMINI_KEY_2="your_second_key"

python advisor_gui.py
```

---

## Project Structure

| File | Purpose |
|---|---|
| `advisor_core.py` | All logic: API keys, upload, Excel parsing, AI calls, batch, JSON |
| `advisor_gui.py` | Tkinter GUI only — imports core, no business logic |
| `catalog_cache.json` | Auto-created; stores catalog URI to skip re-upload |
| `requirements.txt` | Python dependencies |

---

## Features

### Setup Tab
- Paste API keys comma-separated, or set env vars `GEMINI_KEY_1`, `GEMINI_KEY_2`, etc. Keys rotate automatically on quota/429 errors.
- Drag & drop catalog PDF or Excel onto the input fields (requires `tkinterdnd2`).
- Catalog uploaded once, cached in `catalog_cache.json`. URI is verified on every run before re-uploading.
- Supports `.xls` (xlrd) and `.xlsx` (openpyxl) automatically.

### Student Search Tab
- Search by student ID, English name, or Arabic name.
- Comma-separate multiple queries: `"123, Ahmed, محمد"` finds all three at once.
- Multi-select results list (Ctrl+click). Then either:
  - **Recommend Selected** — only highlighted students
  - **Recommend All Matches** — every result
- Save as Summary JSON (titles only) or Detailed JSON (with justification + proof).

### Batch All Students Tab
- Every sheet processed independently, 1.5 s apart to respect rate limits.
- Live progress bar and per-student log.
- Auto-saves `batch_summary.json` and `batch_detailed.json`.

---

## JSON Output Formats

### Summary
```json
[
  {
    "student_id": "12345",
    "name_english": "Ahmed Ali",
    "name_arabic": "أحمد علي",
    "sheet": "Sheet1",
    "recommended_courses": ["Calculus II", "Physics I"]
  }
]
```

### Detailed
```json
[
  {
    "student_id": "12345",
    "name_english": "Ahmed Ali",
    "name_arabic": "أحمد علي",
    "sheet": "Sheet1",
    "term": "Fall 2025",
    "recommended_courses": [
      {
        "course_code": "MATH201",
        "course_title": "Calculus II",
        "course_title_in_arabic": "حساب التفاضل والتكامل ٢",
        "credits": 3,
        "justification": "Student completed MATH101 with grade A in Spring 2024.",
        "catalog_availability_proof": "MATH201 is offered every Fall and Spring — catalog p.47."
      }
    ]
  }
]
```

---

## Model Accuracy Improvements

1. **Few-shot examples** — Add 1-2 worked examples directly in the prompt. The single biggest accuracy boost.
2. **Chain-of-thought** — Add a `"reasoning"` field to the JSON schema so the model must write its logic before committing. Structured thinking reduces errors.
3. **Two-pass validation** — After the first response, send a second prompt: "Review your recommendations against the catalog rules and flag any violations." Filter the final list using pass 2.
4. **Cite page numbers** — Ask the model to cite the exact catalog page for every rule. Forces it to locate the text rather than guess.
5. **`response_schema`** — Use Gemini's schema enforcement parameter (a JSON Schema object) instead of just `response_mime_type`. Enforces field types at the API level.
6. **RAG (Retrieval-Augmented Generation)** — Chunk the catalog, embed with a text model, store in ChromaDB. Send only the top-k relevant chunks per student instead of the whole PDF. Reduces noise dramatically.
7. **Transcript normalization** — Normalize grade representations (A, 4.0, "Pass", "ممتاز") into a unified format before sending. Inconsistent grades are a leading cause of model errors.
8. **Python prerequisite validator** — Parse the catalog prerequisite table once (offline) into a dict. After the model responds, re-check each recommendation against this dict as a hard filter.

---

## Project Improvements

1. **Web interface** — Replace Tkinter with FastAPI + a lightweight frontend. Easier to style, mobile-friendly, supports multi-user access.
2. **SQLite history** — Store every recommendation session. Track changes, re-run without re-uploading, query past results.
3. **Async batch** — Replace `time.sleep` + sequential with `asyncio` + `asyncio.Semaphore`. 3-5 concurrent requests (one per key) cuts batch time proportionally.
4. **PDF reports** — Generate a formatted PDF per student with `reportlab` or `weasyprint`. Optionally email via SMTP.
5. **Conflict detection** — If the Excel includes time slots, flag scheduling conflicts.
6. **GPA-aware filtering** — Filter recommendations by catalog GPA minimums automatically.
7. **Prerequisite graph** — Visualize the student's completed path and unlocked courses using NetworkX + matplotlib.
8. **Audit log** — Log every session with timestamp, catalog version, model ID, output hash. Useful for compliance.
9. **Config file** — Move all constants (MODEL_ID, delay, paths) to `config.yaml` so non-developers can tune the tool without editing Python.

---

## Other Useful Features

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

## Future Roadmap

**Phase 1 — Stability (now → 1 month)**
- Two-pass validation prompt
- `response_schema` enforcement
- Unit tests for `advisor_core.py`
- `config.yaml` for all constants

**Phase 2 — Scale (1 → 3 months)**
- Local web app (FastAPI + frontend)
- RAG with ChromaDB
- Async batch with per-key concurrency
- SQLite recommendation history
- Prerequisite graph visualization

**Phase 3 — Deployment (3 → 6 months)**
- Docker container
- Role-based access (advisor vs. student)
- SIS integration via API
- Automated email delivery
- Scheduled batch runs

**Phase 4 — Intelligence (6+ months)**
- Fine-tune a smaller model on past advisor decisions
- Predict graduation timeline
- Detect at-risk students (falling behind on required courses)
- Arabic / English UI toggle
