"""
MT5 HTML report parser (Detailed Account History "ReportHistory" and
account statements).

MT5 writes one large <table>: header info rows (Name/Account/Company/Date),
then sections introduced by a single wide <th> ("Positions", "Orders",
"Deals", "Open Positions", "Results"...). Each section has a header row
followed by data rows. Spacer cells carry class="hidden" and are dropped.

Only the standard library is used so parsing is deterministic and offline.
"""
from __future__ import annotations

import re
from html.parser import HTMLParser

import pandas as pd

SECTION_NAMES = {
    "positions": "positions",
    "orders": "orders",
    "deals": "deals",
    "open positions": "open_positions",
    "working orders": "working_orders",
    "results": "results",
    "summary": "results",
    "history": "positions",
}


class _TableParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows: list[list[dict]] = []
        self._row: list[dict] | None = None
        self._cell: dict | None = None
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = {"text": "", "tag": tag, "hidden": "hidden" in (a.get("class") or ""),
                          "colspan": int(a.get("colspan", "1") or 1)
                          if str(a.get("colspan", "1")).isdigit() else 1}
        elif tag == "br" and self._cell is not None:
            self._cell["text"] += " "
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._cell["text"] = re.sub(r"\s+", " ", self._cell["text"]).strip()
            self._row.append(self._cell)
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._cell is not None:
                self._row.append(self._cell)
                self._cell = None
            self.rows.append(self._row)
            self._row = None
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._cell is not None:
            self._cell["text"] += data
        if self._in_title:
            self.title += data


def _section_of(row: list[dict]) -> str | None:
    visible = [c for c in row if not c["hidden"] and c["text"]]
    if len(visible) == 1 and (visible[0]["tag"] == "th" or visible[0]["colspan"] >= 4):
        key = visible[0]["text"].strip().lower()
        return SECTION_NAMES.get(key)
    return None


def _dedupe_headers(headers: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    out = []
    for h in headers:
        base = h.strip() or "col"
        if base in seen:
            seen[base] += 1
            out.append(f"{base}{seen[base]}")  # Time -> Time1 (close time)
        else:
            seen[base] = 0
            out.append(base)
    return out


def is_mt5_html(text: str) -> bool:
    t = text[:20000].lower()
    return "<table" in t and any(k in t for k in ("trade history report", "deals", "positions",
                                                  "account:", "statement"))


def parse_mt5_html(text: str) -> dict:
    """Parse an MT5 HTML report.

    Returns a dict with keys ``account`` (header info), ``tables``
    (section -> DataFrame of raw strings), ``results`` (key/value summary as
    stated by MT5), ``title`` and ``issues``.
    """
    p = _TableParser()
    p.feed(text)
    issues: list[str] = []
    account: dict = {}
    results: dict = {}
    tables: dict[str, pd.DataFrame] = {}

    section = None
    headers: list[str] | None = None
    buffer: list[list[str]] = []

    def flush():
        if section and section not in ("results",) and headers and buffer:
            width = len(headers)
            rows = []
            for r in buffer:
                if len(r) < width:
                    r = r + [""] * (width - len(r))
                rows.append(r[:width])
            df = pd.DataFrame(rows, columns=headers)
            tables[section] = pd.concat([tables[section], df], ignore_index=True) \
                if section in tables else df

    for row in p.rows:
        sec = _section_of(row)
        if sec:
            flush()
            section, headers, buffer = sec, None, []
            continue
        cells = [c["text"] for c in row if not c["hidden"]]
        if not any(cells):
            continue
        if section is None:
            # Header info: "Name:", "Account:", "Company:", "Date:"
            for i, c in enumerate(cells[:-1]):
                if c.endswith(":"):
                    account[c[:-1].strip().lower()] = cells[i + 1]
            continue
        if section == "results":
            for i, c in enumerate(cells[:-1]):
                if c.endswith(":") and cells[i + 1] and not cells[i + 1].endswith(":"):
                    results[c[:-1].strip()] = cells[i + 1]
            continue
        if headers is None:
            headers = _dedupe_headers(cells)
            continue
        buffer.append(cells)
    flush()

    acct_id, currency, server, mode = None, None, None, None
    raw_acct = account.get("account", "")
    m = re.match(r"\s*([A-Za-z0-9_\-]+)\s*(?:\((.*)\))?", raw_acct)
    if m:
        acct_id = m.group(1)
        if m.group(2):
            parts = [x.strip() for x in m.group(2).split(",")]
            currency = parts[0] if parts else None
            server = parts[1] if len(parts) > 1 else None
            mode = ", ".join(parts[2:]) if len(parts) > 2 else None
    if not acct_id:
        issues.append("Account number not found in report header.")
    if "deals" not in tables and "positions" not in tables:
        issues.append("No Deals or Positions section found in HTML report.")

    return {
        "title": p.title.strip(),
        "account": {"account_id": acct_id, "name": account.get("name"),
                    "company": account.get("company"), "currency": currency,
                    "server": server, "mode": mode, "report_date": account.get("date"),
                    "raw": account},
        "tables": tables,
        "results": results,
        "issues": issues,
    }
