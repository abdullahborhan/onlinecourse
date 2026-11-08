"""
Import orchestrator: detect -> parse -> normalise -> validate -> persist.

Supported inputs
----------------
* MT5 Detailed Account History / statement HTML  (``mt5_html``)
* MT5 CSV deal history                           (``csv_deals``)
* CSV closed-position list                       (``csv_positions``)
* Equity / balance history CSV                   (``csv_equity``)
* EA journal / Experts / transaction logs        (``ea_log``)
* EA .set configuration                          (``ea_set``)

Every file is SHA-256 de-duplicated, archived byte-for-byte, and produces an
import validation report. A file is REJECTED (nothing stored) when required
information is missing; it is never padded with zeros.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

import config
from modules import data_validator as dv
from modules.audit_logger import audit, get_logger
from modules.csv_parser import read_csv_text
from modules.database import Database, archive_original
from modules.ea_log_parser import parse_ea_log
from modules.html_parser import is_mt5_html, parse_mt5_html
from modules.normalizer import (normalize_deals, normalize_equity, normalize_orders,
                                normalize_positions)
from modules.parsing_utils import read_text, sha256_bytes
from modules.set_parser import compare_to_baseline, map_set_params, parse_set_file

log = get_logger("importer")

SYNTHETIC_MARKER = "SVPI_SYNTHETIC_TEST_DATA"

IMPORTED = "IMPORTED"
IMPORTED_WITH_WARNINGS = "IMPORTED_WITH_WARNINGS"
DUPLICATE_FILE = "DUPLICATE_FILE"
REJECTED = "REJECTED"


@dataclass
class ImportResult:
    filename: str
    sha256: str = ""
    kind: str = "unknown"
    status: str = REJECTED
    account_id: str | None = None
    source_id: int | None = None
    records: dict = field(default_factory=dict)
    validation: dict = field(default_factory=dict)
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    period_start: str | None = None
    period_end: str | None = None

    @property
    def ok(self) -> bool:
        return self.status in (IMPORTED, IMPORTED_WITH_WARNINGS)

    def to_dict(self) -> dict:
        return json.loads(json.dumps(self.__dict__, default=str))


def _split_issues(res, issues):
    """Issues prefixed 'ERROR:' reject the file; the rest are warnings."""
    for i in issues:
        (res.errors if str(i).startswith("ERROR:") else res.warnings).append(i)


def detect_kind(filename: str, text: str) -> str:
    ext = Path(filename).suffix.lower()
    head = text.lstrip()[:3000].lower()
    if ext == ".set":
        return "ea_set"
    if ext in (".htm", ".html") or head.startswith("<!doctype html") or head.startswith("<html"):
        return "mt5_html" if is_mt5_html(text) else "unknown_html"
    if ext in (".csv", ".txt", ".log", ".tsv"):
        first = head.split("\n")[0]
        delim_like = sum(first.count(d) for d in (",", ";", "\t")) >= 2
        if delim_like:
            try:
                _, info = read_csv_text(text[:20000])
                if info["kind"] in ("deals", "positions", "equity"):
                    return f"csv_{info['kind']}"
            except Exception:  # noqa: BLE001
                pass
            if any(k in first for k in ("strategy", "event", "message")):
                return "ea_log"
        if ext in (".log", ".txt"):
            return "ea_log"
        return "unknown_csv"
    return "unknown"


class MT5Importer:
    def __init__(self, db: Database, cfg: config.SVPIConfig | None = None):
        self.db = db
        self.cfg = cfg or config.load_config()

    # ------------------------------------------------------------------ API
    def import_path(self, path: Path | str, account_id: str | None = None) -> ImportResult:
        path = Path(path)
        return self.import_bytes(path.read_bytes(), path.name, account_id=account_id,
                                 original_path=path)

    def import_bytes(self, data: bytes, filename: str, account_id: str | None = None,
                     original_path: Path | None = None) -> ImportResult:
        res = ImportResult(filename=filename)
        if not data:
            res.errors.append("Empty file.")
            return self._finish(res)
        res.sha256 = sha256_bytes(data)
        existing = self.db.find_source_by_sha(res.sha256)
        if existing:
            res.status = DUPLICATE_FILE
            res.source_id = existing["id"]
            res.account_id = existing["account_id"]
            res.warnings.append(f"Identical file already imported on {existing['imported_at']} "
                                f"as '{existing['filename']}'. Nothing re-imported.")
            return self._finish(res)

        try:
            text, encoding = read_text(data)
        except Exception as exc:  # noqa: BLE001
            res.errors.append(f"Cannot decode file: {exc}")
            return self._finish(res)

        is_synth = SYNTHETIC_MARKER in text or "SYNTHETIC" in filename.upper()
        if is_synth and not config.IS_SYNTHETIC_ENV:
            res.errors.append("File is marked as SYNTHETIC test data and cannot be imported "
                              "into the production database.")
            return self._finish(res)

        res.kind = detect_kind(filename, text)
        res.validation["encoding"] = encoding
        handler = {
            "mt5_html": self._import_html,
            "csv_deals": self._import_csv_deals,
            "csv_positions": self._import_csv_positions,
            "csv_equity": self._import_csv_equity,
            "ea_log": self._import_ea_log,
            "ea_set": self._import_set,
        }.get(res.kind)
        if handler is None:
            res.errors.append(
                f"Unrecognised file format ({res.kind}). Supported: MT5 HTML report, CSV deals/"
                "positions/equity history, EA .log/.txt journal, EA .set file.")
            return self._finish(res)

        # Archive original bytes before anything else.
        archive_path = None
        try:
            if original_path is not None:
                archive_path = str(archive_original(original_path, res.sha256))
            else:
                tmp = config.ARCHIVE_DIR / f"{res.sha256[:16]}_{Path(filename).name}"
                config.ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
                if not tmp.exists():
                    tmp.write_bytes(data)
                archive_path = str(tmp)
        except OSError as exc:
            res.warnings.append(f"Could not archive original file: {exc}")

        try:
            handler(res, text, filename, account_id, archive_path, is_synth, len(data))
        except Exception as exc:  # noqa: BLE001
            log.exception("Import failed for %s", filename)
            res.errors.append(f"Import failed: {exc}")
            res.status = REJECTED
        return self._finish(res)

    # ------------------------------------------------------------ helpers
    def _finish(self, res: ImportResult) -> ImportResult:
        if res.errors and res.status not in (DUPLICATE_FILE,):
            res.status = REJECTED
        audit("FILE_IMPORT", {"file": res.filename, "kind": res.kind, "status": res.status,
                              "records": res.records, "errors": res.errors[:5],
                              "account": res.account_id}, db=self.db)
        return res

    def _resolve_account(self, account_id: str | None, res: ImportResult) -> str:
        if account_id:
            return str(account_id)
        accts = self.db.accounts()
        if len(accts) == 1:
            res.warnings.append(f"No account number in file; assigned to the only known "
                                f"account {accts[0]}.")
            return accts[0]
        res.warnings.append("No account number in file or selection; stored under "
                            "'UNSPECIFIED'. Select the account when uploading to avoid this.")
        return "UNSPECIFIED"

    def _register(self, res, filename, kind, account, archive_path, synth, size, validation,
                  status, ps=None, pe=None) -> int:
        return self.db.register_source_file(
            filename=filename, sha256=res.sha256, file_kind=kind, size_bytes=size,
            account_id=account, status=status, validation=validation, archive_path=archive_path,
            period_start=ps, period_end=pe, is_synthetic=synth)

    def _store_deals(self, res, deals, val, account, src):
        new, dup = self.db.insert_deals(deals)
        res.records["deals"] = {"new": new, "duplicate": dup}
        if dup:
            res.warnings.append(f"{dup} deal(s) were already in the database (overlapping "
                                "period) and were not duplicated.")
        return new, dup

    # ------------------------------------------------------------- handlers
    def _import_html(self, res, text, filename, account_id, archive_path, synth, size):
        parsed = parse_mt5_html(text)
        acct = parsed["account"]
        account = acct.get("account_id") or self._resolve_account(account_id, res)
        if account_id and acct.get("account_id") and str(account_id) != acct["account_id"]:
            res.warnings.append(f"Selected account {account_id} differs from report account "
                                f"{acct['account_id']}; the report's account was used.")
        res.account_id = account
        res.warnings.extend(parsed["issues"])
        tables = parsed["tables"]
        validation = {"sections": {k: len(v) for k, v in tables.items()},
                      "report_title": parsed["title"], "account_header": acct,
                      "stated_results": parsed["results"]}

        deals = positions = orders = pd.DataFrame()
        if "deals" in tables:
            deals, issues, meta = normalize_deals(tables["deals"], account, None)
            _split_issues(res, issues)
            validation["deals_mapping"] = meta
        if "positions" in tables:
            positions, issues, meta = normalize_positions(tables["positions"], account, None)
            res.warnings.extend(issues)
            validation["positions_mapping"] = meta
        if "orders" in tables:
            orders, issues, _ = normalize_orders(tables["orders"], account, None)
            res.warnings.extend(issues)
        if "open_positions" in tables:
            validation["open_positions_rows"] = len(tables["open_positions"])
            res.warnings.append(f"{len(tables['open_positions'])} open position row(s) present: "
                                "floating P/L is not included in closed-trade metrics.")

        if deals.empty and positions.empty:
            res.errors.append("HTML report contains no usable Deals or Positions rows.")
            return
        if not deals.empty:
            validation["deals"] = dv.validate_deals(deals)
            validation["results_cross_check"] = dv.cross_check_results(deals, parsed["results"])
            cc = validation["results_cross_check"]
            if cc.get("checked") and not cc["match"]:
                res.warnings.append(
                    f"Computed net profit {cc['computed_total_net_profit']} differs from MT5 "
                    f"stated {cc['stated_total_net_profit']} - verify the report is complete.")
        if not positions.empty:
            validation["positions"] = dv.validate_positions(positions)

        errs = validation.get("deals", {}).get("errors", []) + \
            validation.get("positions", {}).get("errors", [])
        res.errors.extend(errs)
        if res.errors:
            return
        for k in ("deals", "positions"):
            res.warnings.extend(validation.get(k, {}).get("warnings", []))

        ps = deals["time"].min() if not deals.empty else positions["open_time"].min()
        pe = deals["time"].max() if not deals.empty else positions["close_time"].max()
        status = IMPORTED_WITH_WARNINGS if res.warnings else IMPORTED
        src = self._register(res, filename, "mt5_html", account, archive_path, synth, size,
                             validation, status, ps, pe)
        res.source_id = src
        self.db.upsert_account(account, {k: v for k, v in acct.items() if k != "raw"})
        for df in (deals, positions, orders):
            if not df.empty:
                df["source_file_id"] = src
        if not deals.empty:
            self._store_deals(res, deals, validation, account, src)
        if not positions.empty:
            new, dup = self.db.insert_report_positions(positions)
            res.records["positions"] = {"new": new, "duplicate": dup}
        if not orders.empty:
            new, dup = self.db.insert_orders(orders)
            res.records["orders"] = {"new": new, "duplicate": dup}
        res.status = status
        res.period_start, res.period_end = str(ps), str(pe)
        res.validation = validation
        self.db.update_source_counts(src, sum(v["new"] for v in res.records.values()),
                                     sum(v["duplicate"] for v in res.records.values()))

    def _import_csv_deals(self, res, text, filename, account_id, archive_path, synth, size):
        raw, info = read_csv_text(text)
        account = self._resolve_account(account_id, res)
        res.account_id = account
        deals, issues, meta = normalize_deals(raw, account, None, info["decimal_comma"])
        _split_issues(res, issues)
        if deals.empty:
            res.errors.extend(issues or ["No valid deal rows."])
            return
        validation = {"csv": info, "mapping": meta, "deals": dv.validate_deals(deals)}
        res.errors.extend(validation["deals"]["errors"])
        if res.errors:
            return
        res.warnings.extend(validation["deals"]["warnings"])
        status = IMPORTED_WITH_WARNINGS if res.warnings else IMPORTED
        ps, pe = deals["time"].min(), deals["time"].max()
        src = self._register(res, filename, "csv_deals", account, archive_path, synth, size,
                             validation, status, ps, pe)
        deals["source_file_id"] = src
        res.source_id = src
        new, dup = self._store_deals(res, deals, validation, account, src)
        self.db.update_source_counts(src, new, dup)
        res.status, res.validation = status, validation
        res.period_start, res.period_end = str(ps), str(pe)

    def _import_csv_positions(self, res, text, filename, account_id, archive_path, synth, size):
        raw, info = read_csv_text(text)
        account = self._resolve_account(account_id, res)
        res.account_id = account
        pos, issues, meta = normalize_positions(raw, account, None, info["decimal_comma"])
        res.warnings.extend(issues)
        if pos.empty:
            res.errors.extend(issues or ["No valid position rows."])
            return
        validation = {"csv": info, "mapping": meta, "positions": dv.validate_positions(pos)}
        res.errors.extend(validation["positions"]["errors"])
        if res.errors:
            return
        res.warnings.extend(validation["positions"]["warnings"])
        status = IMPORTED_WITH_WARNINGS if res.warnings else IMPORTED
        ps, pe = pos["open_time"].min(), pos["close_time"].max()
        src = self._register(res, filename, "csv_positions", account, archive_path, synth, size,
                             validation, status, ps, pe)
        pos["source_file_id"] = src
        res.source_id = src
        new, dup = self.db.insert_report_positions(pos)
        res.records["positions"] = {"new": new, "duplicate": dup}
        self.db.update_source_counts(src, new, dup)
        res.status, res.validation = status, validation
        res.period_start, res.period_end = str(ps), str(pe)

    def _import_csv_equity(self, res, text, filename, account_id, archive_path, synth, size):
        raw, info = read_csv_text(text)
        account = self._resolve_account(account_id, res)
        res.account_id = account
        eq, issues, meta = normalize_equity(raw, account, None, info["decimal_comma"])
        res.warnings.extend(issues)
        if eq.empty:
            res.errors.extend(issues or ["No valid equity rows."])
            return
        validation = {"csv": info, "mapping": meta, "equity": dv.validate_equity(eq)}
        res.warnings.extend(validation["equity"]["warnings"])
        status = IMPORTED_WITH_WARNINGS if res.warnings else IMPORTED
        ps, pe = eq["time"].min(), eq["time"].max()
        src = self._register(res, filename, "csv_equity", account, archive_path, synth, size,
                             validation, status, ps, pe)
        eq["source_file_id"] = src
        res.source_id = src
        new, dup = self.db.insert_equity(eq)
        res.records["equity_points"] = {"new": new, "duplicate": dup}
        self.db.update_source_counts(src, new, dup)
        res.status, res.validation = status, validation
        res.period_start, res.period_end = str(ps), str(pe)

    def _import_ea_log(self, res, text, filename, account_id, archive_path, synth, size):
        df, info = parse_ea_log(text, filename, self.cfg.analysis.strategy_comment_patterns)
        account = self._resolve_account(account_id, res)
        res.account_id = account
        res.warnings.extend(info["issues"])
        if df.empty:
            res.errors.append("No EA log lines parsed.")
            return
        status = IMPORTED_WITH_WARNINGS if res.warnings else IMPORTED
        ps, pe = df["time"].min(), df["time"].max()
        src = self._register(res, filename, "ea_log", account, archive_path, synth, size,
                             {"ea_log": info}, status, ps, pe)
        df["account_id"] = account
        df["source_file_id"] = src
        res.source_id = src
        new, dup = self.db.insert_ea_events(df)
        res.records["ea_events"] = {"new": new, "duplicate": dup}
        self.db.update_source_counts(src, new, dup)
        res.status, res.validation = status, {"ea_log": info}
        res.period_start, res.period_end = str(ps), str(pe)

    def _import_set(self, res, text, filename, account_id, archive_path, synth, size):
        raw, info = parse_set_file(text)
        if not raw:
            res.errors.extend(info["issues"])
            return
        mapped = map_set_params(raw)
        drift = compare_to_baseline(mapped, self.cfg.risk)
        validation = {"set_file": info, "mapped": mapped, "drift_vs_approved": drift}
        unmapped = [d["parameter"] for d in drift if d["verdict"] == "NOT_FOUND_IN_SET_FILE"]
        if unmapped:
            res.warnings.append("Parameters not identified in .set (confirm manually on the Data "
                                f"Management page): {', '.join(unmapped)}")
        looser = [d["parameter"] for d in drift if d["verdict"] == "LOOSER_THAN_APPROVED"]
        if looser:
            res.warnings.append(f"EA settings LOOSER than approved baseline: {', '.join(looser)}")
        status = IMPORTED_WITH_WARNINGS if res.warnings else IMPORTED
        account = account_id or None
        res.account_id = account
        src = self._register(res, filename, "ea_set", account, archive_path, synth, size,
                             validation, status)
        res.source_id = src
        self.db.save_ea_config(raw, mapped, origin="SET_FILE_UNCONFIRMED", source_file_id=src)
        res.records["ea_config"] = {"new": 1, "duplicate": 0}
        res.status, res.validation = status, validation


def import_incoming_directory(db: Database, cfg=None, move_processed: bool = True,
                              account_id: str | None = None) -> list[ImportResult]:
    """Import every file in data/incoming (used by the weekly workflow).

    ``account_id`` is applied to files that do not state an account (CSV, logs)."""
    importer = MT5Importer(db, cfg)
    results = []
    config.INCOMING_DIR.mkdir(parents=True, exist_ok=True)
    for path in sorted(config.INCOMING_DIR.iterdir()):
        if path.name.startswith(".") or not path.is_file():
            continue
        res = importer.import_path(path, account_id=account_id)
        results.append(res)
        if move_processed and res.status != REJECTED:
            dest = config.PROCESSED_DIR / path.name
            if dest.exists():
                dest = config.PROCESSED_DIR / f"{res.sha256[:8]}_{path.name}"
            config.PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
            path.replace(dest)
    return results
