"""
Generate a SYNTHETIC demonstration dataset into the DEMO database only.

    $env:SVPI_ENV="demo"; python scripts/generate_demo_data.py

The data is random (seeded), carries the SVPI_SYNTHETIC_TEST_DATA marker and
can never be imported into the production database. Use it only to explore the
dashboard; it says nothing about SHARK V80's real performance.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

if os.environ.get("SVPI_ENV", "production").lower() != "demo":
    sys.exit("Refusing to run: set SVPI_ENV=demo so synthetic data goes to the demo database.")

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

import config  # noqa: E402
from modules.database import Database  # noqa: E402
from modules.mt5_importer import MT5Importer  # noqa: E402
from modules.report_generator import generate_report  # noqa: E402
from modules.analysis_service import week_bounds  # noqa: E402
from tests import synthetic_data as syn  # noqa: E402

ACCOUNT = "DEMO80"
TAGS = {"BREAKOUT": "BRK", "MOMENTUM": "MOM", "PULLBACK": "PB"}


def make_positions(weeks: int = 6, seed: int = 80) -> list[dict]:
    rng = np.random.default_rng(seed)
    start = pd.Timestamp("2026-09-07")
    out, pid, bsk, price = [], 300000, 0, 5000.0
    t = start + pd.Timedelta(hours=1)
    end = start + pd.Timedelta(days=7 * weeks)
    while t < end:
        strat = rng.choice(list(TAGS), p=[0.4, 0.35, 0.25])
        direction = rng.choice(["buy", "sell"])
        sign = 1 if direction == "buy" else -1
        layers = rng.choice([1, 2, 3], p=[0.55, 0.3, 0.15])
        bsk += 1
        win = rng.random() < {"BREAKOUT": 0.55, "MOMENTUM": 0.6, "PULLBACK": 0.45}[strat]
        move = rng.uniform(15, 40) * (1 if win else -1)
        close_t = t + pd.Timedelta(minutes=int(rng.integers(10, 120)))
        sl_dist = 30.0
        for layer in range(1, layers + 1):
            pid += 1
            vol = {1: 0.5, 2: 0.25, 3: 0.25}[layer]
            o_t = t + pd.Timedelta(minutes=5 * (layer - 1))
            o_p = round(price + sign * (layer - 1) * rng.uniform(-6, 8), 2)
            c_p = round(price + sign * move, 2)
            sl = round(o_p - sign * sl_dist, 2)
            out.append(syn.pos(str(pid), direction, vol, o_t.strftime("%Y-%m-%d %H:%M:%S"),
                               o_p, sl, close_t.strftime("%Y-%m-%d %H:%M:%S"), c_p,
                               comment=f"SV80 {TAGS[strat]} L{layer} bsk={bsk}",
                               tp=round(o_p + sign * 45, 2), comm=-0.05 * vol * 10))
        price = max(1000.0, price + rng.normal(0, 25))
        t = close_t + pd.Timedelta(minutes=int(rng.integers(40, 300)))
    return out


def main():
    config.ensure_directories()
    db = Database()
    positions = make_positions()
    deposits = [("2026-09-07 00:00:00", 10_000.0, "Initial deposit")]
    deals = syn.build_deals(positions, deposits)
    imp = MT5Importer(db)
    r = imp.import_bytes(deals.to_csv(index=False).encode(), "demo_deals_SYNTHETIC.csv",
                         account_id=ACCOUNT)
    print("deals:", r.status, r.records)
    eq = syn.dense_equity_csv(deals, "2026-09-07", "2026-10-19", freq="15min")
    r = imp.import_bytes(eq.encode(), "demo_equity_SYNTHETIC.csv", account_id=ACCOUNT)
    print("equity:", r.status, r.records)
    r = imp.import_bytes(syn.set_file().encode(), "demo_SYNTHETIC.set", account_id=ACCOUNT)
    print(".set:", r.status)
    s, _ = week_bounds("2026-09-07")
    for i in range(6):
        ws = s + pd.Timedelta(days=7 * i)
        rep = generate_report(db, ACCOUNT, ws, ws + pd.Timedelta(days=7))
        print(f"week {ws.date()}: {rep['status']}")
    print(f"Demo database: {db.path}")


if __name__ == "__main__":
    main()
