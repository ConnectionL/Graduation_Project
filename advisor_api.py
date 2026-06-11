"""
advisor_api.py — FastAPI REST server for Academic Advisor AI.

Endpoints:
  POST /setup          — Upload catalog PDF + student Excel; builds catalog summary.
  GET  /setup/status   — Return current setup state (ready / processing / error).
  POST /student/recommend — Accept student_code; return recommended courses JSON.

Run:
  uvicorn advisor_api:app --host 0.0.0.0 --port 8001 --reload
"""

import os
import io
import json
import threading
import traceback
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, HTTPException, Form
from fastapi.responses import JSONResponse
from pydantic import BaseModel

import advisor_core as core

# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(
    title="Academic Advisor AI — API",
    description="Gemini-powered course recommendation engine",
    version="1.0.0",
)

# ── Shared state (in-process; single-worker is fine for local/dev use) ────────
_STATE_FILE   = "api_state.json"
_EXCEL_PATH   = "setup_student.xlsx"   # last uploaded Excel is saved here
_SUMMARY_PATH = "catalog_summary.txt"  # Gemini-generated compact catalog text

# status: "idle" | "processing" | "ready" | "error"
_state: dict = {
    "status":  "idle",
    "message": "No setup has been run yet.",
    "error":   None,
}
_state_lock = threading.Lock()


def _save_state():
    with open(_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(_state, f, ensure_ascii=False, indent=2)


def _load_state_from_disk():
    """Restore state from disk on startup (survives hot-reloads).

    If the file is corrupted or unreadable, silently reset to default state
    and delete the bad file so the next save creates a clean one.
    """
    global _state
    if os.path.exists(_STATE_FILE):
        try:
            with open(_STATE_FILE, encoding="utf-8", errors="replace") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                _state = loaded
        except Exception:
            # Corrupted file — remove it so the next _save_state() starts clean
            try:
                os.remove(_STATE_FILE)
            except OSError:
                pass


_load_state_from_disk()

# Also load excel sheets if already on disk
_sheets: dict = {}


def _load_excel_if_present():
    global _sheets
    if os.path.exists(_EXCEL_PATH):
        try:
            _sheets = core.read_all_sheets(_EXCEL_PATH, log=lambda m: None)
        except Exception:
            _sheets = {}


_load_excel_if_present()

# Load condensed catalog text if present
_condensed_text: str | None = None
if os.path.exists(_SUMMARY_PATH):
    with open(_SUMMARY_PATH, encoding="utf-8") as f:
        _condensed_text = f.read()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _set_state(status: str, message: str, error: str | None = None):
    global _state
    with _state_lock:
        _state["status"]  = status
        _state["message"] = message
        _state["error"]   = error
        _save_state()


def _ok(data=None, message: str = "OK") -> dict:
    return {"success": True, "message": message, "data": data}


def _err(message: str, status_code: int = 400):
    raise HTTPException(status_code=status_code, detail={"success": False, "message": message})


# ══════════════════════════════════════════════════════════════════════════════
#  POST /setup
# ══════════════════════════════════════════════════════════════════════════════

@app.post("/setup", summary="Upload catalog PDF + student Excel and build catalog summary")
async def setup(
    collage_list:    UploadFile = File(..., description="University catalog PDF"),
    student_formula: UploadFile = File(..., description="Student transcript Excel (.xlsx/.xls)"),
    term:            str        = Form(default="Next Term", description="Target academic term"),
):
    """
    Accepts the same two files as the Laravel admin/setup/import endpoint.
    - Saves the Excel locally.
    - Uploads the PDF to Gemini Files API and generates a compact catalog summary.
    - Processing runs in a background thread; poll GET /setup/status to track progress.
    """
    global _sheets, _condensed_text

    # ── Validate MIME types ──────────────────────────────────────────────────
    if collage_list.content_type not in ("application/pdf", "application/octet-stream"):
        ct = collage_list.content_type
        if not (collage_list.filename or "").lower().endswith(".pdf"):
            _err("collage_list must be a PDF file.")

    allowed_excel = (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.ms-excel",
        "application/octet-stream",
    )
    if student_formula.content_type not in allowed_excel:
        fname = (student_formula.filename or "").lower()
        if not (fname.endswith(".xlsx") or fname.endswith(".xls")):
            _err("student_formula must be an Excel file (.xlsx or .xls).")

    # ── Guard: don't start if already processing ─────────────────────────────
    if _state["status"] == "processing":
        _err("Setup is already in progress. Poll GET /setup/status to monitor.", 409)

    # ── Persist uploaded files ────────────────────────────────────────────────
    pdf_bytes   = await collage_list.read()
    excel_bytes = await student_formula.read()

    # Save PDF with its original filename so the cache key stays stable
    pdf_filename = collage_list.filename or "catalog.pdf"
    pdf_path     = os.path.abspath(pdf_filename)
    excel_ext    = Path(student_formula.filename or "students.xlsx").suffix or ".xlsx"
    excel_path   = os.path.abspath(f"setup_student{excel_ext}")

    with open(pdf_path,   "wb") as f:
        f.write(pdf_bytes)
    with open(excel_path, "wb") as f:
        f.write(excel_bytes)

    # Update global excel path constant so recommend() can find it
    global _EXCEL_PATH
    _EXCEL_PATH = excel_path

    _set_state("processing", "Loading Excel and building catalog summary…")

    # ── Background task ───────────────────────────────────────────────────────
    def _background():
        global _sheets, _condensed_text, _EXCEL_PATH

        logs = []

        def _log(msg: str):
            logs.append(msg)

        try:
            # 1. Read Excel sheets
            _log("📂 Reading Excel…")
            _sheets = core.read_all_sheets(excel_path, log=_log)
            _log(f"  ✓ {len(_sheets)} student sheet(s) loaded.")

            # 2. Build catalog summary (or use cached)
            summary_path = _SUMMARY_PATH
            if os.path.exists(summary_path):
                _log(f"📄 Loading cached catalog summary from {summary_path}…")
                with open(summary_path, encoding="utf-8") as f:
                    _condensed_text = f.read()
                _log("  ✓ Cached summary loaded.")
            else:
                _log("📤 Uploading PDF to Gemini…")
                uri, mime = core.get_or_upload_catalog(pdf_path, log=_log)
                _log("  ✓ Upload complete. Generating compact catalog summary…")
                _condensed_text = core.compress_catalog(
                    uri, mime, log=_log, output_path=summary_path
                )
                # Clean up the uploaded file from Gemini (we have the text now)
                try:
                    core.delete_cached_catalog(pdf_path, log=_log)
                except Exception:
                    pass

            _set_state(
                "ready",
                f"Setup complete — {len(_sheets)} student(s) loaded. Catalog summary ready.",
            )
            _log("✓ Setup finished successfully.")

        except Exception as e:
            tb = traceback.format_exc()
            _set_state("error", "Setup failed. See error field.", error=str(e))
            _log(f"✗ Setup error: {e}\n{tb}")

    threading.Thread(target=_background, daemon=True).start()

    return _ok(
        {"status": "processing", "pdf": pdf_filename, "excel": student_formula.filename},
        "Setup started. Poll GET /setup/status for progress.",
    )


# ══════════════════════════════════════════════════════════════════════════════
#  GET /setup/status
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/setup/status", summary="Return current setup processing state")
def setup_status():
    """
    Returns:
      status  — "idle" | "processing" | "ready" | "error"
      message — Human-readable description
      error   — Error detail (only when status == "error")
      students_loaded — Number of Excel sheets (students) available
    """
    with _state_lock:
        payload = dict(_state)
    payload["students_loaded"] = len(_sheets)
    return _ok(payload, "Status fetched.")


# ══════════════════════════════════════════════════════════════════════════════
#  POST /student/recommend
# ══════════════════════════════════════════════════════════════════════════════

class RecommendRequest(BaseModel):
    student_code: str
    term: str = "Next Term"


@app.post("/student/recommend", summary="Get recommended courses for a student by their code")
def recommend(req: RecommendRequest):
    """
    Accepts:
      student_code — The student ID / code (same value stored in User.code on Laravel).
      term         — Target academic term (e.g. "Fall 2025"). Defaults to "Next Term".

    Returns:
      A list of recommended course objects:
        course_code, course_title, course_title_in_arabic,
        credits, justification, catalog_availability_proof
    """
    # ── Guard: setup must be ready ────────────────────────────────────────────
    if _state["status"] != "ready":
        _err(
            f"Advisor is not ready yet (status: {_state['status']}). "
            "Run POST /setup first and wait for status=ready.",
            503,
        )

    if not _condensed_text:
        _err("Catalog summary is missing. Re-run POST /setup.", 503)

    if not _sheets:
        _err("Student Excel not loaded. Re-run POST /setup.", 503)

    # ── Search for the student ────────────────────────────────────────────────
    query = req.student_code.strip()
    if not query:
        _err("student_code must not be empty.")

    matches = core.find_students(_sheets, query)

    if not matches:
        _err(f"No student found matching code '{query}'.", 404)

    # Use the best match (highest score — first in the sorted list)
    sheet_name, df = matches[0]
    student_label  = core._student_label(df, sheet_name)

    # ── Run the 3-step Gemini recommendation ─────────────────────────────────
    try:
        courses = core.recommend(
            catalog_uri=None,          # we use condensed text, no URI needed
            catalog_mime=None,
            student_md=df.to_markdown(index=False),
            term=req.term,
            log=print,
            student_label=student_label,
            condensed_catalog_text=_condensed_text,
            catalog_path=None,
            validate_course_codes=False,
            df=df,
        )
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail={
                "success": False,
                "message": f"Recommendation engine failed: {e}",
                "student_code": query,
            },
        )

    return _ok(
        {
            "student_code":        query,
            "student_label":       student_label,
            "sheet":               sheet_name,
            "term":                req.term,
            "recommended_courses": courses,
        },
        f"Recommendations generated for {student_label}.",
    )


# ══════════════════════════════════════════════════════════════════════════════
#  GET /health
# ══════════════════════════════════════════════════════════════════════════════

@app.get("/health", summary="Health check")
def health():
    return {"status": "ok", "advisor_status": _state["status"]}
