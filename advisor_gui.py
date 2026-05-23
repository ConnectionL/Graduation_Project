"""
advisor_gui.py — Tkinter GUI for Academic Advisor AI.
All business logic lives in advisor_core.py.

New in this version:
  - API keys auto‑saved to .env on Load
  - Auto‑detect catalog/Excel in working directory on startup
  - Catalog Manager removed (compact mode replaces upload need)
  - Richer inline feedback in all log widgets
  - Universal English prompts
  - Arabic search normalisation (NFC) with fallback
  - Step‑0 catalog compression (automatic on first load)
  - Clean search results (SheetName | ValueFromMatch)
  - Only key count shown, not names
  - Recommendation logs and JSON use "Sheetname | identifier"
  - Excel export for recommendations
  - Output files automatically numbered in 'output/' folder
  - Sound notification on completion
  - Summary JSON now contains sheet_name, identifier_used, and course titles only
"""

import os, json, re, threading, unicodedata, platform
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext, simpledialog
import pandas as pd

import advisor_core as core

# ── Palette & fonts ───────────────────────────────────────────────────────────
C = {
    "bg":      "#0d1117", "surface":  "#161b22", "surface2": "#21262d",
    "border":  "#30363d", "accent":   "#2f81f7", "accent2":  "#388bfd",
    "success": "#3fb950", "warning":  "#d29922", "error":    "#f85149",
    "text":    "#e6edf3", "muted":    "#8b949e", "subtle":   "#484f58",
    "red_btn": "#8b1a1a",
}
FT  = ("Segoe UI", 22, "bold")
FSB = ("Segoe UI", 11)
FL  = ("Segoe UI", 10, "bold")
FB  = ("Segoe UI", 10)
FM  = ("Consolas",  9)

# ── Sound helper ──────────────────────────────────────────────────────────────
def play_beep():
    """Cross‑platform notification beep."""
    try:
        if platform.system() == "Windows":
            import winsound
            winsound.Beep(1000, 200)
        else:
            print('\a', end='', flush=True)
    except Exception:
        pass

# ── Widget helpers ────────────────────────────────────────────────────────────

def _card(parent):
    return tk.Frame(parent, bg=C["surface"],
                    highlightbackground=C["border"], highlightthickness=1)

def _label(parent, text, color=None, font=None, bg=None, **kw):
    return tk.Label(parent, text=text,
                    bg=bg or C["surface"], fg=color or C["text"],
                    font=font or FB, **kw)

def _entry(parent, var, width=40):
    return tk.Entry(parent, textvariable=var, width=width,
                    bg=C["surface2"], fg=C["text"], insertbackground=C["text"],
                    relief="flat", font=FB, bd=0,
                    highlightthickness=1,
                    highlightbackground=C["border"],
                    highlightcolor=C["accent"])

def _btn(parent, text, cmd, accent=True, color=None, **kw):
    bg = color or (C["accent"] if accent else C["surface2"])
    return tk.Button(parent, text=text, command=cmd,
                     bg=bg, fg=C["text"],
                     activebackground=C["accent2"], activeforeground=C["text"],
                     relief="flat", font=FL if accent else FB,
                     cursor="hand2", bd=0, padx=12, pady=6, **kw)

def _log_widget(parent, height=10):
    return scrolledtext.ScrolledText(
        parent, height=height, bg=C["surface2"], fg=C["text"],
        font=FM, relief="flat", bd=0, state="disabled",
        insertbackground=C["text"])

def _append(widget, msg, color=None):
    widget.configure(state="normal")
    if color:
        tag = f"col_{color.replace('#','')}"
        widget.tag_configure(tag, foreground=color)
        widget.insert("end", msg + "\n", tag)
    else:
        widget.insert("end", msg + "\n")
    widget.see("end")
    widget.configure(state="disabled")

def _set_text(widget, text):
    widget.configure(state="normal")
    widget.delete("1.0", "end")
    widget.insert("1.0", text)
    widget.configure(state="disabled")

def _smart_append(widget, msg):
    if msg.startswith("  ✓") or msg.startswith("✓") or "ACTIVE" in msg or "succeeded" in msg.lower():
        _append(widget, msg, C["success"])
    elif msg.startswith("  ✗") or msg.startswith("✗") or "FAIL" in msg or "ERROR" in msg.upper():
        _append(widget, msg, C["error"])
    elif msg.startswith("  ⚠") or msg.startswith("⚠") or "warning" in msg.lower():
        _append(widget, msg, C["warning"])
    elif "═" in msg or "─" in msg or msg.strip().startswith("BATCH") or msg.strip().startswith("Start"):
        _append(widget, msg, C["accent"])
    else:
        _append(widget, msg)

# ── Main Application ──────────────────────────────────────────────────────────

class AdvisorApp(tk.Tk):

    def __init__(self):
        super().__init__()
        self.title("Academic Advisor AI")
        self.geometry("1150x800")
        self.minsize(960, 640)
        self.configure(bg=C["bg"])

        # State variables
        self._catalog_path = tk.StringVar()
        self._student_file = tk.StringVar()
        self._term         = tk.StringVar(value="Student's upcoming term")
        self._output_dir   = tk.StringVar(value=os.getcwd())
        self._api_keys = []
        self._api_key_names_var = tk.StringVar()
        self._search_query = tk.StringVar()

        self._catalog_uri  = None
        self._catalog_mime = None
        self._sheets       = {}
        self._search_full_results = []
        self._batch_summary  = []
        self._batch_detailed = []
        self._current_matches = []

        # Optional catalog summary (compact text)
        self._condensed_text = None

        # Load keys from .env / env vars
        self._api_keys = [k for k in core.pool.keys if k != "YOUR_API_KEY_HERE"]
        self._update_api_key_names()

        self._build_styles()
        self._build_ui()
        self._auto_detect_files()
        self._register_dnd()

    # ── Styles ────────────────────────────────────────────────────────────────
    def _build_styles(self):
        s = ttk.Style(self)
        s.theme_use("clam")
        s.configure("TNotebook", background=C["bg"], tabmargins=0)
        s.configure("TNotebook.Tab", background=C["surface2"],
                    foreground=C["muted"], font=FB, padding=(16, 8))
        s.map("TNotebook.Tab",
              background=[("selected", C["surface"])],
              foreground=[("selected", C["text"])])
        s.configure("Horizontal.TProgressbar",
                    troughcolor=C["surface2"], background=C["accent"],
                    thickness=6, relief="flat")

    # ── UI ────────────────────────────────────────────────────────────────────
    def _build_ui(self):
        hdr = tk.Frame(self, bg=C["bg"])
        hdr.pack(fill="x", padx=28, pady=(18, 6))
        tk.Label(hdr, text="Academic Advisor AI", bg=C["bg"],
                 fg=C["text"], font=FT).pack(side="left")
        tk.Label(hdr, text="  ·  Gemini‑powered course recommendation",
                 bg=C["bg"], fg=C["muted"], font=FSB).pack(side="left", pady=5)

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=18, pady=(0, 8))

        tab_names = ["⚙  Setup", "🔍  Student Search", "📋  Batch All"]
        self._tabs = []
        for name in tab_names:
            f = tk.Frame(nb, bg=C["bg"])
            nb.add(f, text=f"  {name}  ")
            self._tabs.append(f)

        self._build_setup(self._tabs[0])
        self._build_search(self._tabs[1])
        self._build_batch(self._tabs[2])

        # Status bar
        self._status = tk.StringVar(value="Ready.")
        sb = tk.Frame(self, bg=C["surface"], height=26)
        sb.pack(fill="x", side="bottom")
        tk.Label(sb, textvariable=self._status, bg=C["surface"],
                 fg=C["muted"], font=("Segoe UI", 9), anchor="w", padx=12).pack(fill="x")

    # ── Setup Tab ─────────────────────────────────────────────────────────────
    def _build_setup(self, p):

        def file_row(card, label_text, var, browse_fn, row_idx):
            _label(card, label_text, color=C["muted"], font=FL, anchor="w").grid(
                row=row_idx*2, column=0, columnspan=3, sticky="w", padx=14, pady=(10, 2))
            f = tk.Frame(card, bg=C["surface"])
            f.grid(row=row_idx*2+1, column=0, columnspan=3, sticky="ew", padx=14, pady=(0, 4))
            f.columnconfigure(0, weight=1)
            _entry(f, var).grid(row=0, column=0, sticky="ew")
            _btn(f, "Browse", browse_fn, accent=False).grid(row=0, column=1, padx=(6, 0))

        # ── API Keys ──
        c1 = _card(p)
        c1.pack(fill="x", padx=18, pady=(18, 8))
        c1.columnconfigure(0, weight=1)
        _label(c1, "Gemini API Keys  (auto‑rotates on quota errors)",
               color=C["muted"], font=FL, anchor="w").pack(fill="x", padx=14, pady=(10, 2))
        key_row = tk.Frame(c1, bg=C["surface"])
        key_row.pack(fill="x", padx=14, pady=(0, 4))
        key_row.columnconfigure(0, weight=1)
        key_row.columnconfigure(1, weight=0)
        key_row.columnconfigure(2, weight=0)
        tk.Label(key_row, textvariable=self._api_key_names_var,
                 bg=C["surface"], fg=C["text"], font=FL,
                 anchor="w", padx=6, pady=6).grid(row=0, column=0,
                 sticky="ew")
        _btn(key_row, "➕ Add Key", self._add_api_key,
             accent=False).grid(row=0, column=1, padx=(6, 0))
        _btn(key_row, "💾 Save to .env", self._save_keys_to_env,
             accent=False).grid(row=0, column=2, padx=(6, 0))
        _label(c1, "Actual key values are never displayed. Add new keys through the GUI; they are stored in .env.",
               color=C["subtle"], font=("Segoe UI", 8), anchor="w").pack(
            fill="x", padx=14, pady=(0, 10))

        # ── Files ──
        c2 = _card(p)
        c2.pack(fill="x", padx=18, pady=8)
        c2.columnconfigure(0, weight=1)
        _label(c2, "Drag & drop files onto the fields, or use Browse.  "
               "If left blank, the app auto‑detects files in the current folder.",
               color=C["subtle"], font=("Segoe UI", 8), anchor="w").grid(
            row=0, column=0, columnspan=3, sticky="w", padx=14, pady=(8, 0))
        file_row(c2, "Catalog PDF",
                 self._catalog_path,
                 lambda: self._browse_file(self._catalog_path, [("PDF", "*.pdf")]), 1)
        file_row(c2, "Student Excel  (.xls / .xlsx)",
                 self._student_file,
                 lambda: self._browse_file(self._student_file,
                                           [("Excel", "*.xls *.xlsx")]), 2)

        # ── Term + Output dir ──
        c3 = _card(p)
        c3.pack(fill="x", padx=18, pady=8)
        c3.columnconfigure(1, weight=1)
        c3.columnconfigure(3, weight=1)
        _label(c3, "Target Term", color=C["muted"], font=FL, anchor="w").grid(
            row=0, column=0, padx=(14, 6), pady=(10, 10))
        _entry(c3, self._term, width=22).grid(row=0, column=1, sticky="ew", pady=(10, 10))
        _label(c3, "  Output Directory", color=C["muted"], font=FL, anchor="w").grid(
            row=0, column=2, padx=(18, 6), pady=(10, 10))
        od = tk.Frame(c3, bg=C["surface"])
        od.grid(row=0, column=3, sticky="ew", padx=(0, 14), pady=(10, 10))
        od.columnconfigure(0, weight=1)
        _entry(od, self._output_dir).grid(row=0, column=0, sticky="ew")
        _btn(od, "Choose", lambda: self._browse_dir(self._output_dir),
             accent=False).grid(row=0, column=1, padx=(6, 0))

        # ── Load button ──
        br = tk.Frame(p, bg=C["bg"])
        br.pack(fill="x", padx=18, pady=6)
        _btn(br, "▶  Load Files & Prepare Catalog", self._load_files).pack(side="left")
        self._catalog_badge = tk.Label(br, text="", bg=C["bg"], fg=C["muted"], font=FB)
        self._catalog_badge.pack(side="left", padx=10)

        # ── Setup log (larger height) ──
        lc = _card(p)
        lc.pack(fill="both", expand=True, padx=18, pady=(4, 16))
        lh = tk.Frame(lc, bg=C["surface"])
        lh.pack(fill="x", padx=14, pady=(10, 4))
        _label(lh, "Setup Log", color=C["muted"], font=FL, anchor="w").pack(side="left")
        _btn(lh, "Clear", lambda: _set_text(self._setup_log, ""),
             accent=False).pack(side="right")
        self._setup_log = _log_widget(lc, height=18)
        self._setup_log.pack(fill="both", expand=True, padx=14, pady=(0, 12))

    # ── Search Tab ────────────────────────────────────────────────────────────
    def _build_search(self, p):
        sc = _card(p)
        sc.pack(fill="x", padx=18, pady=(18, 8))
        sc.columnconfigure(0, weight=1)

        _label(sc, "Search by student ID, English name, or Arabic name  "
               "(comma‑separate multiple queries)",
               color=C["muted"], font=FL, anchor="w").grid(
            row=0, column=0, columnspan=4, sticky="w", padx=14, pady=(10, 4))
        self._search_entry = _entry(sc, self._search_query, width=55)
        self._search_entry.grid(row=1, column=0, sticky="ew", padx=14, pady=(0, 4))
        self._search_entry.bind("<Return>", self._on_search_enter)
        _btn(sc, "🔍 Find", self._do_search, accent=False).grid(
            row=1, column=1, padx=(0, 6), pady=(0, 4))
        _btn(sc, "⚡ Recommend Selected", self._do_recommend_selected).grid(
            row=1, column=2, padx=(0, 6), pady=(0, 4))
        _btn(sc, "⚡ Recommend All Matches", self._do_recommend_all).grid(
            row=1, column=3, padx=(0, 14), pady=(0, 4))
        _label(sc, 'Example: "20210001, Ahmed, فاطمة" — partial matches work.',
               color=C["subtle"], font=("Segoe UI", 8), anchor="w").grid(
            row=2, column=0, columnspan=4, sticky="w", padx=14, pady=(0, 8))

        rc = _card(p)
        rc.pack(fill="x", padx=18, pady=4)
        _label(rc, "Matches  (click to select; Ctrl+click for multiple)",
               color=C["muted"], font=FL, anchor="w").pack(fill="x", padx=14, pady=(10, 4))
        lf = tk.Frame(rc, bg=C["surface"])
        lf.pack(fill="x", padx=14, pady=(0, 10))
        vsb = tk.Scrollbar(lf, orient="vertical")
        self._res_list = tk.Listbox(
            lf, bg=C["surface2"], fg=C["text"], font=FB,
            selectbackground=C["accent"], relief="flat", bd=0,
            height=5, activestyle="none", selectmode="extended",
            yscrollcommand=vsb.set)
        vsb.config(command=self._res_list.yview)
        self._res_list.pack(side="left", fill="x", expand=True)
        self._res_list.bind("<Return>", self._on_search_enter)
        vsb.pack(side="right", fill="y")

        oc = _card(p)
        oc.pack(fill="both", expand=True, padx=18, pady=(4, 16))
        oh = tk.Frame(oc, bg=C["surface"])
        oh.pack(fill="x", padx=14, pady=(10, 4))
        _label(oh, "Recommendations", color=C["muted"], font=FL, anchor="w").pack(side="left")
        _btn(oh, "💾 Save Detailed JSON", self._save_search_detailed, accent=False).pack(side="right")
        _btn(oh, "💾 Save Summary JSON", self._save_search_summary, accent=False).pack(side="right", padx=(0, 6))
        _btn(oh, "📊 Save as Excel", self._save_search_excel, accent=False).pack(side="right", padx=(0, 6))
        self._search_out = _log_widget(oc, height=16)
        self._search_out.pack(fill="both", expand=True, padx=14, pady=(0, 12))

    # ── Batch Tab ─────────────────────────────────────────────────────────────
    def _build_batch(self, p):
        ic = _card(p)
        ic.pack(fill="x", padx=18, pady=(18, 8))
        _label(ic,
               "Process every student (sheet) in the Excel file independently.\n"
               "A 1.5 s gap is kept between requests to avoid rate‑limit errors.",
               color=C["text"], font=FB, justify="left").pack(anchor="w", padx=14, pady=10)

        ctrl = tk.Frame(ic, bg=C["surface"])
        ctrl.pack(fill="x", padx=14, pady=(0, 12))
        _btn(ctrl, "▶  Run Batch for All Students", self._do_batch).pack(side="left")
        self._batch_badge = tk.Label(ctrl, text="", bg=C["surface"],
                                     fg=C["muted"], font=FB)
        self._batch_badge.pack(side="left", padx=12)

        pc = _card(p)
        pc.pack(fill="x", padx=18, pady=4)
        pi = tk.Frame(pc, bg=C["surface"])
        pi.pack(fill="x", padx=14, pady=10)
        self._prog_var = tk.DoubleVar()
        ttk.Progressbar(pi, variable=self._prog_var, maximum=100,
                        style="Horizontal.TProgressbar").pack(fill="x")
        self._prog_label = tk.Label(pi, text="", bg=C["surface"],
                                    fg=C["muted"], font=("Segoe UI", 9))
        self._prog_label.pack(anchor="w", pady=(4, 0))

        lc = _card(p)
        lc.pack(fill="both", expand=True, padx=18, pady=(4, 8))
        lh = tk.Frame(lc, bg=C["surface"])
        lh.pack(fill="x", padx=14, pady=(10, 4))
        _label(lh, "Batch Log", color=C["muted"], font=FL, anchor="w").pack(side="left")
        _btn(lh, "Clear", lambda: _set_text(self._batch_log, ""), accent=False).pack(side="right")
        self._batch_log = _log_widget(lc, height=16)
        self._batch_log.pack(fill="both", expand=True, padx=14, pady=(0, 12))

        sr = tk.Frame(p, bg=C["bg"])
        sr.pack(fill="x", padx=18, pady=(0, 16))
        _btn(sr, "💾 Save Summary JSON",
             lambda: self._save_batch(detailed=False), accent=False).pack(side="left", padx=(0, 8))
        _btn(sr, "📄 Save Detailed JSON",
             lambda: self._save_batch(detailed=True), accent=False).pack(side="left", padx=(0, 8))
        _btn(sr, "📊 Save Detailed Excel", self._save_batch_excel, accent=False).pack(side="left")

    # ── Drag & Drop ───────────────────────────────────────────────────────────
    def _register_dnd(self):
        try:
            from tkinterdnd2 import DND_FILES  # noqa
            self._dnd_ok = True
        except ImportError:
            self._dnd_ok = False

        msg = ("ℹ  Drag‑and‑drop enabled (tkinterdnd2)."
               if self._dnd_ok else
               "ℹ  Drag‑and‑drop unavailable — install tkinterdnd2 for it: pip install tkinterdnd2")
        _smart_append(self._setup_log, msg)

        if self._dnd_ok:
            for var in (self._catalog_path, self._student_file):
                self._bind_dnd(var)

    def _bind_dnd(self, var):
        try:
            from tkinterdnd2 import DND_FILES
            def _recurse(w):
                try:
                    if isinstance(w, tk.Entry) and str(w.cget("textvariable")) == str(var):
                        w.drop_target_register(DND_FILES)
                        w.dnd_bind("<<Drop>>", lambda e: var.set(e.data.strip("{}")))
                except Exception:
                    pass
                for child in w.winfo_children():
                    _recurse(child)
            _recurse(self)
        except Exception:
            pass

    # ── Auto-detect ───────────────────────────────────────────────────────────
    def _auto_detect_files(self):
        cwd = os.getcwd()
        if not self._catalog_path.get():
            found = core.auto_detect_catalog(cwd)
            if found:
                self._catalog_path.set(found)
                _smart_append(self._setup_log,
                              f"ℹ  Auto‑detected catalog: {os.path.basename(found)}")
        if not self._student_file.get():
            found = core.auto_detect_excel(cwd)
            if found:
                self._student_file.set(found)
                _smart_append(self._setup_log,
                              f"ℹ  Auto‑detected Excel:   {os.path.basename(found)}")

    # ── Helpers ───────────────────────────────────────────────────────────────
    def _browse_file(self, var, types_):
        p = filedialog.askopenfilename(filetypes=types_)
        if p:
            var.set(p)

    def _browse_dir(self, var):
        p = filedialog.askdirectory()
        if p:
            var.set(p)

    def _set_status(self, msg):
        self._status.set(msg)
        self.update_idletasks()

    def _update_api_key_names(self):
        count = len(self._api_keys)
        if count == 0:
            self._api_key_names_var.set("No API keys detected")
        else:
            self._api_key_names_var.set(f"{count} API key(s) loaded")

    def _add_api_key(self):
        new_key = simpledialog.askstring("Add Gemini API Key",
                                         "Paste a Gemini API key below:",
                                         show="*", parent=self)
        if not new_key:
            return
        new_key = new_key.strip()
        if not new_key:
            return
        if new_key in self._api_keys:
            messagebox.showinfo("Duplicate key", "This API key is already added.")
            return
        self._api_keys.append(new_key)
        self._update_api_key_names()
        core.pool.set_keys(self._api_keys, persist=False)
        _smart_append(self._setup_log, "  ✓ Added new API key (hidden from UI)")

    def _apply_api_keys(self, persist: bool = False):
        raw = [k.strip() for k in self._api_keys if k.strip()]
        if raw:
            core.pool.set_keys(raw, persist=persist)

    def _save_keys_to_env(self):
        self._apply_api_keys(persist=True)
        _smart_append(self._setup_log, f"  ✓ API keys saved to {core.ENV_FILE}")
        self._set_status(f"API keys saved to {core.ENV_FILE}")

    def _log_setup(self, msg):
        self.after(0, lambda: _smart_append(self._setup_log, msg))

    def _log_batch(self, msg):
        self.after(0, lambda: _smart_append(self._batch_log, msg))

    def _log_search(self, msg):
        self.after(0, lambda: _smart_append(self._search_out, msg))

    # ── Next available numbered filename helper ───────────────────────────────
    def _next_numbered_path(self, base_dir, base_name, ext):
        """Return a path like base_dir/base_name (N).ext with the smallest N not already used."""
        os.makedirs(base_dir, exist_ok=True)
        existing = os.listdir(base_dir)
        pattern = re.compile(r'^\s*' + re.escape(base_name) + r'\s*(?:\((\d+)\))?\s*\.' + re.escape(ext) + r'$', re.IGNORECASE)
        max_num = 0
        for fname in existing:
            m = pattern.match(fname)
            if m:
                num_str = m.group(1)
                if num_str:
                    num = int(num_str)
                else:
                    num = 1
                if num > max_num:
                    max_num = num
        next_num = max_num + 1
        if next_num == 1 and not os.path.exists(os.path.join(base_dir, f"{base_name}.{ext}")):
            return os.path.join(base_dir, f"{base_name}.{ext}")
        return os.path.join(base_dir, f"{base_name} ({next_num}).{ext}")

    # ── Load Files ────────────────────────────────────────────────────────────
    def _load_files(self):
        self._apply_api_keys()
        catalog = self._catalog_path.get().strip()
        student = self._student_file.get().strip()

        if not catalog:
            catalog = core.auto_detect_catalog()
            if catalog:
                self._catalog_path.set(catalog)
                _smart_append(self._setup_log, f"ℹ  Auto‑detected catalog: {os.path.basename(catalog)}")
        if not student:
            student = core.auto_detect_excel()
            if student:
                self._student_file.set(student)
                _smart_append(self._setup_log, f"ℹ  Auto‑detected Excel: {os.path.basename(student)}")

        if not catalog or not student:
            messagebox.showerror("Missing Files",
                "Could not find catalog PDF or student Excel file.\n"
                "Place them in the same folder as the app, or use Browse to locate them.")
            return

        pdf_dir = os.path.dirname(os.path.abspath(catalog))
        summary_path = os.path.join(pdf_dir, "catalog_summary.txt")

        def load_or_build_summary():
            if os.path.exists(summary_path):
                _smart_append(self._setup_log, f"📄 Loaded cached compact catalog from {summary_path}")
                with open(summary_path, encoding="utf-8") as f:
                    self._condensed_text = f.read()
                self._catalog_uri = None
                self._catalog_mime = None
                self.after(0, lambda: self._catalog_badge.config(
                    text="✓ Compact catalog ready", fg=C["success"]))
                return

            _smart_append(self._setup_log, "📤 Compact catalog not found. Uploading PDF to generate it…")
            try:
                uri, mime = core.get_or_upload_catalog(
                    catalog, log=lambda m: _smart_append(self._setup_log, m))
                self._catalog_uri = uri
                self._catalog_mime = mime

                summary = core.compress_catalog(
                    uri, mime,
                    log=lambda m: _smart_append(self._setup_log, m),
                    output_path=summary_path)
                self._condensed_text = summary
                _smart_append(self._setup_log, "✓ Compact catalog generated and saved.")

                core.delete_cached_catalog(catalog, log=lambda m: _smart_append(self._setup_log, m))
                self._catalog_uri = None
                self._catalog_mime = None
                self.after(0, lambda: self._catalog_badge.config(
                    text="✓ Compact catalog ready", fg=C["success"]))
            except Exception as e:
                _smart_append(self._setup_log, f"  ✗ Compact catalog generation failed: {e}")
                self.after(0, lambda: self._catalog_badge.config(
                    text="✗ Catalog prep failed", fg=C["error"]))
                raise

        def task():
            self._set_status("Loading…")
            _smart_append(self._setup_log, "\n── Loading Files ──────────────────────")
            try:
                self._sheets = core.read_all_sheets(
                    student, log=lambda m: _smart_append(self._setup_log, m))
                self._set_status(f"Excel loaded — {len(self._sheets)} student(s).")
            except Exception as e:
                _smart_append(self._setup_log, f"  ✗ Excel failed: {e}")
                self._set_status("Excel load failed.")
                return

            try:
                load_or_build_summary()
            except Exception:
                pass

            if self._condensed_text:
                self._set_status(f"Ready — {len(self._sheets)} students loaded. Using compact catalog.")
            else:
                self._set_status("Excel loaded, but catalog preparation failed — see log.")

        threading.Thread(target=task, daemon=True).start()

    # ── Search Actions ────────────────────────────────────────────────────────
    def _find_matching_identifier(self, df, queries):
        """Return the first matching cell value (normalised or raw) from the sheet."""
        for q in queries:
            lower_q = q.lower()
            is_arb = bool(re.search(r'[\u0621-\u064A]', lower_q))
            if is_arb:
                # Try normalised first
                norm_q = core._normalize_arabic(lower_q)
                for col in df.columns:
                    values = df[col].astype(str).apply(core._normalize_arabic)
                    if values.str.contains(norm_q, na=False, regex=False).any():
                        mask = df[col].astype(str).apply(core._normalize_arabic).str.contains(norm_q, na=False, regex=False)
                        return str(df[col][mask].iloc[0]).strip()
                # Fallback: raw match
                for col in df.columns:
                    values = df[col].astype(str).str.lower()
                    if values.str.contains(lower_q, na=False, regex=False).any():
                        mask = values.str.contains(lower_q, na=False, regex=False)
                        return str(df[col][mask].iloc[0]).strip()
            else:
                # English / ID
                for col in df.columns:
                    values = df[col].astype(str).str.lower()
                    if values.str.contains(lower_q, na=False, regex=False).any():
                        mask = values.str.contains(lower_q, na=False, regex=False)
                        return str(df[col][mask].iloc[0]).strip()
        return core._first_nonempty_value(df, df.columns[0]) or "?"

    def _do_search(self):
        queries = [q.strip() for q in self._search_query.get().split(",") if q.strip()]
        if not queries:
            return
        if not self._sheets:
            messagebox.showwarning("Not loaded", "Load files first (Setup tab).")
            return
        matches = []
        for q in queries:
            matches.extend(core.find_students(self._sheets, q))
        seen = set()
        unique = []
        for sheet, df in matches:
            if sheet not in seen:
                seen.add(sheet)
                unique.append((sheet, df))
        self._current_matches = unique
        self._res_list.delete(0, "end")
        if not unique:
            self._res_list.insert("end", "  No students found.")
        else:
            for sheet, df in unique:
                identifier = self._find_matching_identifier(df, queries)
                self._res_list.insert("end", f"  {sheet}  |  {identifier}")
        self._set_status(f"Found {len(unique)} match(es) for: {', '.join(queries)}")

    def _on_search_enter(self, event):
        query = self._search_query.get().strip()
        selected = self._res_list.curselection()
        if event.widget is self._search_entry:
            if query:
                self._do_search()
            elif selected:
                self._do_recommend_selected()
            else:
                self._do_recommend_all()
            return "break"
        if event.widget is self._res_list:
            if selected:
                self._do_recommend_selected()
            elif not query:
                self._do_recommend_all()
            return "break"

    def _do_recommend_selected(self):
        if not self._current_matches:
            messagebox.showwarning("No matches", "Run a search first.")
            return
        sel = self._res_list.curselection()
        if not sel:
            messagebox.showinfo("Nothing selected",
                                "Click a row in the matches list (Ctrl+click for multiple).")
            return
        self._run_recommend([self._current_matches[i] for i in sel])

    def _do_recommend_all(self):
        if not self._current_matches:
            messagebox.showwarning("No matches", "Run a search first.")
            return
        self._run_recommend(self._current_matches)

    def _run_recommend(self, targets):
        if not self._catalog_uri and not self._condensed_text:
            messagebox.showwarning("Catalog not prepared",
                                   "Load files first (Setup tab) to generate the catalog summary.")
            return
        term = self._term.get().strip() or "Next Term"
        queries = [q.strip() for q in self._search_query.get().split(",") if q.strip()]
        labels = []
        for sheet, df in targets:
            ident = self._find_matching_identifier(df, queries)
            labels.append(ident)

        def task():
            self._set_status(f"Generating recommendations for {len(targets)} student(s)…")
            _set_text(self._search_out, "")
            try:
                results = core.recommend_students(
                    self._catalog_uri, self._catalog_mime,
                    [(sheet, df) for sheet, df in targets],
                    term,
                    log=self._log_search,
                    condensed_catalog_text=self._condensed_text,
                    catalog_path=self._catalog_path.get().strip(),
                    labels=labels,
                    validate_course_codes=False)
                self._search_full_results = results
                _smart_append(self._search_out, "\n── Result JSON ────────────────────────")
                _smart_append(self._search_out, json.dumps(results, ensure_ascii=False, indent=2))
                self._set_status(f"✓ Done — {len(results)} student(s) processed.")
                play_beep()
            except Exception as e:
                _smart_append(self._search_out, f"  ✗ Fatal error: {e}")
                self._set_status(f"Error: {e}")
                play_beep()

        threading.Thread(target=task, daemon=True).start()

    def _save_search_summary(self):
        """Save search results as summary JSON: sheet_name, identifier_used, course titles."""
        if not self._search_full_results:
            messagebox.showinfo("No data", "Nothing to save.")
            return
        summary_data = []
        for res in self._search_full_results:
            if "error" in res:
                summary_data.append(res)
            else:
                summary_data.append({
                    "sheet_name": res["sheet"],
                    "identifier_used": res["identifier"],
                    "recommended_courses": [c["course_title"] for c in res.get("recommended_courses", [])]
                })
        out_dir = os.path.join(os.getcwd(), "output")
        path = self._next_numbered_path(out_dir, "search_summary", "json")
        core.save_results(summary_data, path)
        self._set_status(f"Summary saved to {path}")

    def _save_search_detailed(self):
        """Save full search results as detailed JSON."""
        if not self._search_full_results:
            messagebox.showinfo("No data", "Nothing to save.")
            return
        out_dir = os.path.join(os.getcwd(), "output")
        path = self._next_numbered_path(out_dir, "search_detailed", "json")
        core.save_results(self._search_full_results, path)
        self._set_status(f"Detailed saved to {path}")

    def _save_search_excel(self):
        if not self._search_full_results:
            messagebox.showinfo("No data", "Nothing to export.")
            return
        out_dir = os.path.join(os.getcwd(), "output")
        path = self._next_numbered_path(out_dir, "recommendations", "xlsx")
        try:
            with pd.ExcelWriter(path, engine='openpyxl') as writer:
                for res in self._search_full_results:
                    identifier = res.get("identifier", res.get("sheet", "unknown"))
                    sheet_name = identifier[:31]
                    courses = res.get("recommended_courses", [])
                    if courses:
                        df = pd.DataFrame(courses)
                        df.to_excel(writer, sheet_name=sheet_name, index=False)
                    else:
                        pd.DataFrame({"Info": ["No courses recommended"]}).to_excel(
                            writer, sheet_name=sheet_name, index=False)
            self._set_status(f"Excel saved to {path}")
        except Exception as e:
            messagebox.showerror("Export failed", str(e))

    # ── Batch Actions ─────────────────────────────────────────────────────────
    def _do_batch(self):
        if not self._catalog_uri and not self._condensed_text:
            messagebox.showwarning("Catalog not prepared",
                                   "Load files first (Setup tab) to generate the catalog summary.")
            return
        if not self._sheets:
            messagebox.showwarning("Not loaded", "Load the student file first.")
            return

        term     = self._term.get().strip() or "Next Term"
        out_dir  = self._output_dir.get().strip() or os.getcwd()
        s_path   = self._next_numbered_path(os.path.join(out_dir, "output"), "batch_summary", "json")
        d_path   = self._next_numbered_path(os.path.join(out_dir, "output"), "batch_detailed", "json")

        def progress(i, total, sheet):
            self.after(0, lambda: (
                self._prog_var.set(i / total * 100),
                self._prog_label.config(text=f"{i}/{total}: {sheet}"),
                self._set_status(f"Batch: processing student {i}/{total} — {sheet}"),
            ))

        def task():
            self.after(0, lambda: self._batch_badge.config(text="Running…", fg=C["warning"]))
            try:
                s, d = core.batch_all(
                    self._catalog_uri, self._catalog_mime, self._sheets, term,
                    log=self._log_batch, progress=progress,
                    summary_path=s_path, detailed_path=d_path,
                    condensed_catalog_text=self._condensed_text,
                    catalog_path=self._catalog_path.get().strip(),
                    validate_course_codes=False)
                self._batch_summary  = s
                self._batch_detailed = d
                ok = sum(1 for r in s if "error" not in r)
                self.after(0, lambda: (
                    self._batch_badge.config(
                        text=f"✓ {ok}/{len(s)} succeeded", fg=C["success"]),
                    self._prog_var.set(100),
                    self._prog_label.config(text="Complete!"),
                ))
                self._set_status(f"Batch done — {ok}/{len(s)} succeeded → {out_dir}")
                _smart_append(self._batch_log, f"JSON saved to {out_dir}")
                play_beep()
            except Exception as e:
                self.after(0, lambda: self._batch_badge.config(text=f"✗ {e}", fg=C["error"]))
                self._set_status(f"Batch error: {e}")
                play_beep()

        threading.Thread(target=task, daemon=True).start()

    def _save_batch(self, detailed=False):
        data = self._batch_detailed if detailed else self._batch_summary
        default_name = "batch_detailed" if detailed else "batch_summary"
        out_dir = os.path.join(os.getcwd(), "output")
        path = self._next_numbered_path(out_dir, default_name, "json")
        if data:
            core.save_results(data, path)
            self._set_status(f"Saved to {path}")
        else:
            messagebox.showinfo("No data", "Nothing to save yet.")

    def _save_batch_excel(self):
        if not self._batch_detailed:
            messagebox.showinfo("No data", "Run the batch first.")
            return
        out_dir = os.path.join(os.getcwd(), "output")
        path = self._next_numbered_path(out_dir, "batch_detailed", "xlsx")
        try:
            with pd.ExcelWriter(path, engine='openpyxl') as writer:
                for res in self._batch_detailed:
                    if "error" in res:
                        continue
                    sheet_name = res.get("sheet", "unknown")[:31]
                    courses = res.get("recommended_courses", [])
                    if courses:
                        df = pd.DataFrame(courses)
                        df.to_excel(writer, sheet_name=sheet_name, index=False)
                    else:
                        pd.DataFrame({"Info": ["No courses recommended"]}).to_excel(
                            writer, sheet_name=sheet_name, index=False)
            self._set_status(f"Excel saved to {path}")
        except Exception as e:
            messagebox.showerror("Export failed", str(e))

if __name__ == "__main__":
    app = AdvisorApp()
    app.mainloop()