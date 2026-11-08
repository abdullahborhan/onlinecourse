"""
SYNTHETIC TEST DATA - NOT REAL TRADING RESULTS.

Builds MT5-style files (CSV deals, HTML report, equity CSV, EA log, .set)
from a small hand-specified set of positions whose expected metrics are
calculated by hand in ``EXPECTED``. Every artefact carries the marker
``SVPI_SYNTHETIC_TEST_DATA`` so it can never be imported into production.
"""
from __future__ import annotations

import pandas as pd

MARKER = "SVPI_SYNTHETIC_TEST_DATA"
ACCOUNT = "SYN001"
SYMBOL = "Volatility 80 Index"
MAGIC = "80080"
START_BALANCE = 10_000.0

# Each position: id, dir, open time, open price, sl, tp, comment,
# exits: list of (time, volume, price, commission), entry volume
POSITIONS = [
    # ---------------- Week 1: 2026-01-05 .. 2026-01-11
    dict(pid="1001", dir="buy", vol=0.5, t_open="2026-01-05 10:00:00", p_open=1000.0, sl=980.0,
         tp=1040.0, comment="SV80 BRK L1 bsk=A1",
         exits=[("2026-01-05 11:00:00", 0.5, 1040.0, -0.5)]),
    dict(pid="1002", dir="buy", vol=0.25, t_open="2026-01-05 10:20:00", p_open=1010.0, sl=990.0,
         tp=1040.0, comment="SV80 BRK L2 bsk=A1",
         exits=[("2026-01-05 11:00:00", 0.25, 1040.0, -0.25)]),
    dict(pid="1003", dir="sell", vol=0.5, t_open="2026-01-06 09:00:00", p_open=1100.0, sl=1120.0,
         tp=1060.0, comment="SV80 PB L1 bsk=A2",
         exits=[("2026-01-06 10:00:00", 0.5, 1120.0, -0.5)]),
    dict(pid="1004", dir="buy", vol=0.5, t_open="2026-01-07 12:00:00", p_open=1200.0, sl=1190.0,
         tp=1220.0, comment="SV80 MOM L1 bsk=A3",
         exits=[("2026-01-07 12:05:00", 0.5, 1200.0, 0.0)]),
    dict(pid="1005", dir="sell", vol=1.0, t_open="2026-01-08 14:00:00", p_open=1300.0, sl=1310.0,
         tp=1280.0, comment="SV80 MOM L1 bsk=A4",
         exits=[("2026-01-08 14:30:00", 0.5, 1290.0, -0.5),
                ("2026-01-08 15:00:00", 0.5, 1280.0, -0.5)]),
    dict(pid="1006", dir="buy", vol=0.5, t_open="2026-01-09 09:00:00", p_open=1400.0, sl=0.0,
         tp=0.0, comment="",
         exits=[("2026-01-09 10:00:00", 0.5, 1390.0, -0.5)]),
    # ---------------- Week 2: 2026-01-12 .. 2026-01-18
    dict(pid="2001", dir="buy", vol=0.5, t_open="2026-01-12 10:00:00", p_open=1000.0, sl=990.0,
         tp=1020.0, comment="SV80 BRK L1 bsk=B1",
         exits=[("2026-01-12 10:30:00", 0.5, 990.0, -0.5)]),
    dict(pid="2002", dir="buy", vol=0.25, t_open="2026-01-12 10:10:00", p_open=995.0, sl=985.0,
         tp=1020.0, comment="SV80 BRK L2 bsk=B1",
         exits=[("2026-01-12 10:30:00", 0.25, 985.0, -0.25)]),
    dict(pid="2003", dir="sell", vol=0.5, t_open="2026-01-13 09:00:00", p_open=1100.0, sl=1110.0,
         tp=1080.0, comment="SV80 PB L1 bsk=B2",
         exits=[("2026-01-13 09:40:00", 0.5, 1080.0, -0.5)]),
]
DEPOSITS = [("2026-01-05 00:00:00", START_BALANCE, "Initial deposit"),
            ("2026-01-12 00:00:00", 1_000.0, "Deposit")]

# Hand-calculated expected results (net = profit + commission + swap + fee)
EXPECTED = {
    "week1": {
        "nets": [19.5, 7.25, -10.5, 0.0, 14.0, -5.5],
        "net_profit": 24.75, "gross_profit": 40.75, "gross_loss": -16.0,
        "profit_factor": 40.75 / 16.0, "trades": 6, "wins": 3, "losses": 2, "breakeven": 1,
        "win_rate": 50.0, "largest_win": 19.5, "largest_loss": -10.5,
        "commission": -2.75, "partial_close_positions": 1,
        "start_balance": 10_000.0, "total_return_pct": 0.2475,
    },
    "week2": {"nets": [-5.5, -2.75, 9.5], "net_profit": 1.25, "trades": 3, "wins": 1,
              "losses": 2},
    "all": {"net_profit": 26.0, "trades": 9},
}


def build_deals() -> pd.DataFrame:
    """Deals in MT5 deal-history CSV layout (one IN deal + one or more OUT deals)."""
    rows = []
    for t, amount, cmt in DEPOSITS:
        rows.append(dict(Time=t, Type="balance", Entry="", Symbol="", Volume="", Price="",
                         Order="", Position="", Magic="", Commission=0.0, Swap=0.0, Fee=0.0,
                         Profit=amount, Comment=f"{cmt} {MARKER}", SL="", TP=""))
    for p in POSITIONS:
        rows.append(dict(Time=p["t_open"], Type=p["dir"], Entry="in", Symbol=SYMBOL,
                         Volume=p["vol"], Price=p["p_open"], Order=p["pid"], Position=p["pid"],
                         Magic=MAGIC, Commission=0.0, Swap=0.0, Fee=0.0, Profit=0.0,
                         Comment=p["comment"], SL=p["sl"], TP=p["tp"]))
        sign = 1 if p["dir"] == "buy" else -1
        for i, (t, v, px, comm) in enumerate(p["exits"]):
            rows.append(dict(Time=t, Type="sell" if p["dir"] == "buy" else "buy", Entry="out",
                             Symbol=SYMBOL, Volume=v, Price=px, Order=f"{p['pid']}{i + 1}",
                             Position=p["pid"], Magic=MAGIC, Commission=comm, Swap=0.0, Fee=0.0,
                             Profit=round((px - p["p_open"]) * sign * v, 2),
                             Comment=p["comment"], SL="", TP=""))
    df = pd.DataFrame(rows)
    df["_t"] = pd.to_datetime(df["Time"])
    df = df.sort_values("_t", kind="stable").drop(columns="_t").reset_index(drop=True)
    df.insert(1, "Deal", [str(50000 + i) for i in range(len(df))])
    bal, balances = 0.0, []
    for _, r in df.iterrows():
        bal += float(r["Profit"]) + float(r["Commission"]) + float(r["Swap"]) + float(r["Fee"])
        balances.append(round(bal, 2))
    df["Balance"] = balances
    df["Time"] = pd.to_datetime(df["Time"]).dt.strftime("%Y.%m.%d %H:%M:%S")
    return df


def deals_csv(**drop) -> str:
    df = build_deals()
    for col in drop.get("drop_columns", []):
        df = df.drop(columns=col)
    return df.to_csv(index=False)


def equity_csv() -> str:
    """Hourly equity for week 1 with an intratrade dip deeper than the closed-trade curve."""
    times = pd.date_range("2026-01-05 00:00", "2026-01-11 23:00", freq="h")
    deals = build_deals()
    deals["t"] = pd.to_datetime(deals["Time"], format="%Y.%m.%d %H:%M:%S")
    rows = []
    for t in times:
        past = deals[deals["t"] <= t]
        bal = float(past["Balance"].iloc[-1]) if not past.empty else 0.0
        eq = bal
        if t == pd.Timestamp("2026-01-06 09:00"):
            eq = bal - 60.0  # floating loss observed only in equity data
        rows.append({"Time": t.strftime("%Y.%m.%d %H:%M"), "Balance": bal, "Equity": eq})
    return pd.DataFrame(rows).to_csv(index=False) + f"# {MARKER}\n"


def _row(cells, bg="#FFFFFF"):
    tds = "".join(f'<td class="hidden" colspan="8"></td>' if c == "__HIDDEN__" else f"<td>{c}</td>"
                  for c in cells)
    return f'<tr bgcolor="{bg}" align="right">{tds}</tr>\n'


def mt5_html(encoding: str = "utf-16") -> bytes:
    """MT5 'Trade History Report'-style HTML (no position id in Deals, like real MT5)."""
    deals = build_deals()
    html = [f"<!DOCTYPE html><html><head><title>{ACCOUNT}: Trade History Report {MARKER}"
            "</title></head><body><table>\n"]
    html.append('<tr align="left"><th colspan="14"><div><b>Trade History Report</b></div></th>'
                "</tr>\n")
    html.append(f'<tr align="left"><th colspan="4">Name:</th><th colspan="10"><b>Synthetic '
                f"Tester</b></th></tr>\n")
    html.append(f'<tr align="left"><th colspan="4">Account:</th><th colspan="10"><b>{ACCOUNT} '
                f"(USD, Deriv-Demo, demo, Hedge)</b></th></tr>\n")
    html.append('<tr align="left"><th colspan="4">Company:</th><th colspan="10"><b>Synthetic '
                "Broker</b></th></tr>\n")
    html.append('<tr align="center"><th colspan="14"><div><b>Positions</b></div></th></tr>\n')
    html.append(_row(["Time", "Position", "Symbol", "Type", "__HIDDEN__", "Volume", "Price",
                      "S / L", "T / P", "Time", "Price", "Commission", "Swap", "Profit"],
                     "#E5F0FC"))
    for p in POSITIONS:
        sign = 1 if p["dir"] == "buy" else -1
        vwap = sum(v * px for _, v, px, _ in p["exits"]) / p["vol"]
        profit = sum(round((px - p["p_open"]) * sign * v, 2) for _, v, px, _ in p["exits"])
        comm = sum(c for *_, c in p["exits"])
        ft = lambda s: pd.Timestamp(s).strftime("%Y.%m.%d %H:%M:%S")  # noqa: E731
        html.append(_row([ft(p["t_open"]), p["pid"], SYMBOL, p["dir"], "__HIDDEN__", p["vol"],
                          p["p_open"], p["sl"] or "", p["tp"] or "", ft(p["exits"][-1][0]),
                          f"{vwap:.2f}", f"{comm:.2f}", "0.00", f"{profit:.2f}"]))
    html.append('<tr align="center"><th colspan="14"><div><b>Deals</b></div></th></tr>\n')
    html.append(_row(["Time", "Deal", "Symbol", "Type", "Direction", "Volume", "Price", "Order",
                      "Commission", "Fee", "Swap", "Profit", "Balance", "Comment"], "#E5F0FC"))
    for _, r in deals.iterrows():
        profit = f"{float(r['Profit']):,.2f}".replace(",", " ")
        bal = f"{float(r['Balance']):,.2f}".replace(",", " ")
        html.append(_row([r["Time"], r["Deal"], r["Symbol"], r["Type"], r["Entry"], r["Volume"],
                          r["Price"], r["Order"], f"{float(r['Commission']):.2f}", "0.00",
                          "0.00", profit, bal, r["Comment"]]))
    total = sum(float(x) for x in deals[deals["Type"].isin(["buy", "sell"])]["Profit"]) + \
        sum(float(x) for x in deals[deals["Type"].isin(["buy", "sell"])]["Commission"])
    html.append(_row(["", "", "", "", "", "", "", "", "-4.00", "0.00", "0.00", "30.00",
                      "11 026.00", ""]))
    html.append('<tr align="center"><th colspan="14"><div><b>Results</b></div></th></tr>\n')
    html.append(f"<tr><td>Total Net Profit:</td><td><b>{total:.2f}</b></td>"
                "<td>Gross Profit:</td><td>0</td></tr>\n")
    html.append("</table></body></html>")
    return "".join(html).encode(encoding)


def ea_log(filename_date: str = "20260105") -> str:
    lines = [
        f"CS\t0\t09:59:58.100\tSHARK_V80 (Volatility 80 Index,M1)\tBREAKOUT signal id=S1 "
        f"spread=12.0 {MARKER}",
        "CS\t0\t10:00:00.120\tSHARK_V80 (Volatility 80 Index,M1)\tBREAKOUT buy opened "
        "ticket=1001 layer=1 requested price=999.8",
        "CS\t0\t10:20:00.050\tSHARK_V80 (Volatility 80 Index,M1)\tBREAKOUT buy opened "
        "ticket=1002 layer=2",
        "CS\t0\t10:25:00.000\tSHARK_V80 (Volatility 80 Index,M1)\torder rejected: requote",
    ]
    return "\n".join(lines) + "\n"


def set_file(looser: bool = False) -> str:
    return "\n".join([
        f"; {MARKER}",
        "RiskProfile=MODERATE",
        "MaxLayers=3||1||1||5||N",
        "Layer1_RiskPercent=0.5",
        "Layer2_RiskPercent=0.25",
        "Layer3_RiskPercent=0.25",
        "MaxBasketRiskPercent=1.0",
        f"DailyLossLockPercent={'2.5' if looser else '2.0'}",
        "DailyHardStopPercent=3.0",
        "WeeklyLossLockPercent=5.0",
        "MaxEquityDrawdownPercent=10.0",
        "DailyProfitTargetPercent=1.5",
        "UseMartingale=false",
        "MagicNumber=80080",
    ]) + "\n"
