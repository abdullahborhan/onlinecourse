"""
Pytest fixtures. ALL DATA PRODUCED HERE IS SYNTHETIC and only ever written to
temporary directories / test databases (SVPI_ENV=test).
"""
import os
import sys
from pathlib import Path

os.environ["SVPI_ENV"] = "test"
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

import config  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_dirs(tmp_path, monkeypatch):
    """Redirect every writable directory to a temp folder for each test."""
    for name in ("DATA_DIR", "INCOMING_DIR", "PROCESSED_DIR", "ARCHIVE_DIR", "REPORTS_DIR",
                 "WEEKLY_REPORTS_DIR", "MONTHLY_REPORTS_DIR", "DATABASE_DIR", "BACKUP_DIR"):
        p = tmp_path / name.lower()
        p.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(config, name, p)
    monkeypatch.setattr(config, "DATABASE_PATH", tmp_path / "test_SYNTHETIC.db")
    monkeypatch.setattr(config, "USER_CONFIG_PATH", tmp_path / "svpi_settings.json")
    yield


@pytest.fixture
def db(tmp_path):
    from modules.database import Database
    return Database(tmp_path / "test_SYNTHETIC.db")
