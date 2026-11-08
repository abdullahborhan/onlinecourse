"""
Command-line entry point for the weekly monitoring workflow.

    python run_weekly.py                 # normal scheduled run
    python run_weekly.py --account 12345 # one account
    python run_weekly.py --force         # regenerate even if inputs unchanged
    python run_weekly.py --week 2026-01-05 --no-import

Exit codes: 0 = reports generated, 2 = DATA MISSING for at least one account,
1 = unexpected error.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config  # noqa: E402
from modules.database import Database  # noqa: E402
from modules.report_generator import generate_report  # noqa: E402
from modules.analysis_service import week_bounds  # noqa: E402
from modules.weekly_workflow import run_weekly  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="SHARK V80 Performance Intelligence weekly run")
    ap.add_argument("--account")
    ap.add_argument("--now", help="Override 'now' (server time), e.g. 2026-01-11T12:00")
    ap.add_argument("--week", help="Generate the report for the week containing this date")
    ap.add_argument("--no-import", action="store_true")
    ap.add_argument("--no-pdf", action="store_true")
    ap.add_argument("--no-monthly", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--ai", action="store_true",
                    help="Append AI analyst appendix (offline unless external AI is enabled "
                         "and consented in settings)")
    a = ap.parse_args(argv)
    try:
        if a.week:
            if not a.account:
                ap.error("--week requires --account")
            s, e = week_bounds(a.week)
            r = generate_report(Database(), a.account, s, e, "WEEKLY", force=a.force,
                                pdf=not a.no_pdf, with_ai=a.ai)
            print(json.dumps({k: v for k, v in r.items() if k != "result"}, indent=2,
                             default=str))
            return 2 if r["status"] == "DATA MISSING" else 0
        summary = run_weekly(account=a.account, now=a.now, do_import=not a.no_import,
                             force=a.force, monthly=not a.no_monthly, with_ai=a.ai,
                             pdf=not a.no_pdf)
        print(json.dumps(summary, indent=2, default=str))
        if config.IS_SYNTHETIC_ENV:
            print("NOTE: running in a SYNTHETIC environment "
                  f"({config.ENVIRONMENT}); outputs are not real performance reports.")
        return 2 if summary["data_missing"] else 0
    except Exception as exc:  # noqa: BLE001
        print(f"SVPI weekly run failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
