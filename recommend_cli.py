#!/usr/bin/env python3
"""
recommend_cli.py – Command‑line wrapper for advisor_core.

Single student mode:
    python recommend_cli.py --catalog <pdf> --excel <xlsx> --id <query> [--term <term>] [--key <api_key>]

Batch mode (all students):
    python recommend_cli.py --catalog <pdf> --excel <xlsx> --batch [--term <term>] [--out-dir <dir>] [--key <api_key>]

All modes:
    - --catalog and --excel are required.
    - --key is optional: if provided, adds it to the API key pool.
    - Output is printed to stdout (single student) or saved as JSON files (batch).
"""

import argparse, json, os, sys, re
import advisor_core as core


def find_matching_identifier(df, query):
    """Find the first cell value containing the query (same logic as GUI)."""
    lower_q = query.lower()
    is_arb = bool(re.search(r'[\u0621-\u064A]', lower_q))
    if is_arb:
        norm_q = core._normalize_arabic(lower_q)
        for col in df.columns:
            values = df[col].astype(str).apply(core._normalize_arabic)
            if values.str.contains(norm_q, na=False, regex=False).any():
                mask = df[col].astype(str).apply(core._normalize_arabic).str.contains(norm_q, na=False, regex=False)
                return str(df[col][mask].iloc[0]).strip()
        for col in df.columns:
            values = df[col].astype(str).str.lower()
            if values.str.contains(lower_q, na=False, regex=False).any():
                mask = values.str.contains(lower_q, na=False, regex=False)
                return str(df[col][mask].iloc[0]).strip()
    else:
        for col in df.columns:
            values = df[col].astype(str).str.lower()
            if values.str.contains(lower_q, na=False, regex=False).any():
                mask = values.str.contains(lower_q, na=False, regex=False)
                return str(df[col][mask].iloc[0]).strip()
    return "Unknown"


def main():
    parser = argparse.ArgumentParser(description="Academic Advisor AI – CLI")
    parser.add_argument("--catalog", required=True, help="Path to the university catalog PDF")
    parser.add_argument("--excel", required=True, help="Path to the student Excel file (.xls/.xlsx)")
    parser.add_argument("--id", help="Student ID, English name, or Arabic name (for single student mode)")
    parser.add_argument("--batch", action="store_true", help="Run batch for all students (ignores --id)")
    parser.add_argument("--term", default="Student's upcoming term", help="Target term (e.g. 'Fall 2025')")
    parser.add_argument("--key", help="Gemini API key (optional, added to pool)")
    parser.add_argument("--out-dir", default="output", help="Output directory for batch mode JSON files")
    args = parser.parse_args()

    # --batch and --id are mutually exclusive? We'll just check.
    if not args.batch and not args.id:
        parser.error("Either --id (single student) or --batch must be specified.")

    if args.batch and args.id:
        print("⚠  Ignoring --id because --batch is set.", file=sys.stderr)

    # 1. API key handling
    if args.key:
        # Add provided key to pool (append to any existing keys)
        keys = list(core.pool.keys)
        if args.key not in keys:
            keys.append(args.key)
        core.pool.set_keys(keys, persist=False)
        print(f"✅ Added API key (total keys: {core.pool.key_count})", file=sys.stderr)
    elif core.pool.key_count == 0:
        print("ERROR: No API keys found. Set GEMINI_KEY_1 in .env, env var, or use --key.", file=sys.stderr)
        sys.exit(1)

    # 2. Load Excel
    print(f"Loading Excel: {args.excel} ...", file=sys.stderr)
    sheets = core.read_all_sheets(args.excel, log=lambda m: print(m, file=sys.stderr))

    # 3. Handle compact catalog
    pdf_dir = os.path.dirname(os.path.abspath(args.catalog))
    summary_path = os.path.join(pdf_dir, "catalog_summary.txt")
    condensed_text = None

    if os.path.exists(summary_path):
        print(f"Using existing compact catalog: {summary_path}", file=sys.stderr)
        with open(summary_path, encoding="utf-8") as f:
            condensed_text = f.read()
    else:
        print("Compact catalog not found. Uploading PDF and generating summary...", file=sys.stderr)
        try:
            uri, mime = core.get_or_upload_catalog(args.catalog, log=lambda m: print(m, file=sys.stderr))
            condensed_text = core.compress_catalog(
                uri, mime,
                log=lambda m: print(m, file=sys.stderr),
                output_path=summary_path)
            core.delete_cached_catalog(args.catalog, log=lambda m: print(m, file=sys.stderr))
        except Exception as e:
            print(f"FATAL: Could not prepare catalog: {e}", file=sys.stderr)
            sys.exit(1)

    # 4. Run single student or batch
    if args.batch:
        print(f"\nStarting batch for {len(sheets)} students...", file=sys.stderr)
        out_dir = args.out_dir
        s_path = os.path.join(out_dir, "batch_summary.json")
        d_path = os.path.join(out_dir, "batch_detailed.json")
        summary, detailed = core.batch_all(
            None, None,            # no uploaded catalog needed
            sheets,
            args.term,
            log=lambda m: print(m, file=sys.stderr),
            summary_path=s_path,
            detailed_path=d_path,
            condensed_catalog_text=condensed_text,
            catalog_path=args.catalog,
            validate_course_codes=False)
        print(f"\nBatch complete. Summary → {s_path}, Detailed → {d_path}", file=sys.stderr)
    else:
        # Single student
        matches = core.find_students(sheets, args.id)
        if not matches:
            print(f"ERROR: No student found for query '{args.id}'", file=sys.stderr)
            sys.exit(1)

        sheet_name, df = matches[0]
        label = find_matching_identifier(df, args.id)
        print(f"Matched sheet '{sheet_name}' | '{label}'", file=sys.stderr)

        results = core.recommend_students(
            None, None,
            [(sheet_name, df)],
            args.term,
            log=lambda m: print(m, file=sys.stderr),
            condensed_catalog_text=condensed_text,
            catalog_path=args.catalog,
            labels=[label],
            validate_course_codes=False)

        if "error" in results[0]:
            print(f"ERROR: {results[0]['error']}", file=sys.stderr)
            sys.exit(1)

        # Output JSON to stdout
        print(json.dumps(results[0], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()