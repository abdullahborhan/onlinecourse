"""
SHARK V80 PERFORMANCE INTELLIGENCE (SVPI) - central configuration.

Every threshold that influences a verdict lives here so that it is explicit,
reviewable and configurable. Nothing in this file is "proof" of how the EA is
actually configured: the risk baseline is the APPROVED REFERENCE profile and is
compared against observed trading behaviour.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

APP_NAME = "SHARK V80 PERFORMANCE INTELLIGENCE"
APP_SHORT = "SVPI"
APP_VERSION = "1.0.0"

# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent

# SVPI_ENV=test|demo isolates synthetic data from the production database.
ENVIRONMENT = os.environ.get("SVPI_ENV", "production").lower()
IS_SYNTHETIC_ENV = ENVIRONMENT in {"test", "demo"}

DATA_DIR = BASE_DIR / "data"
INCOMING_DIR = DATA_DIR / "incoming"
PROCESSED_DIR = DATA_DIR / "processed"
ARCHIVE_DIR = DATA_DIR / "archive"
REPORTS_DIR = BASE_DIR / "reports"
WEEKLY_REPORTS_DIR = REPORTS_DIR / "weekly"
MONTHLY_REPORTS_DIR = REPORTS_DIR / "monthly"
DATABASE_DIR = BASE_DIR / "database"
LOG_DIR = BASE_DIR / "logs"
BACKUP_DIR = DATABASE_DIR / "backups"
USER_CONFIG_PATH = BASE_DIR / "svpi_settings.json"

if ENVIRONMENT == "production":
    DATABASE_PATH = DATABASE_DIR / "performance.db"
else:
    DATABASE_PATH = DATABASE_DIR / f"{ENVIRONMENT}_SYNTHETIC.db"

DATABASE_PATH = Path(os.environ.get("SVPI_DB_PATH", str(DATABASE_PATH)))

# --------------------------------------------------------------------------
# Time handling
# --------------------------------------------------------------------------
# Timestamps inside MT5 reports are broker SERVER time. The broker's server
# timezone is not stated inside MT5 reports, so it is a declared assumption
# that every report repeats. Change it if your broker server is different.
SERVER_TIMEZONE = "UTC"
REPORT_TIMEZONE = "Asia/Kuala_Lumpur"
# Weekly schedule (local Malaysia time).
WEEKLY_SCHEDULE_DAY = "SUNDAY"
WEEKLY_SCHEDULE_TIME = "20:00"

# --------------------------------------------------------------------------
# Approved baseline risk configuration (REFERENCE, not proof)
# --------------------------------------------------------------------------


@dataclass
class RiskConfig:
    risk_profile: str = "MODERATE"
    max_layers: int = 3
    layer_risk_pct: tuple = (0.50, 0.25, 0.25)
    max_basket_risk_pct: float = 1.00
    daily_loss_lock_pct: float = 2.00
    daily_hard_stop_pct: float = 3.00
    weekly_loss_lock_pct: float = 5.00
    max_equity_drawdown_pct: float = 10.00
    daily_profit_target_pct: float = 1.50
    # If True, the profit target is treated as a lock (no new entries after it is hit).
    profit_target_is_lock: bool = True
    martingale_enabled: bool = False
    # Relative tolerance before an individual layer's SL-implied risk is flagged
    # (lot-step rounding makes exact percentages impossible). 0.10 = +10%.
    risk_tolerance_rel: float = 0.10
    # Equity observations must be at least this frequent (median seconds) for
    # daily locks to be marked COMPLIANT rather than UNVERIFIABLE.
    max_equity_sampling_s_for_lock_verification: int = 900
    source: str = "APPROVED_BASELINE"  # or "SET_FILE" / "USER_PROVIDED"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["layer_risk_pct"] = list(self.layer_risk_pct)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "RiskConfig":
        d = dict(d)
        if "layer_risk_pct" in d:
            d["layer_risk_pct"] = tuple(float(x) for x in d["layer_risk_pct"])
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**known)


# --------------------------------------------------------------------------
# Analysis parameters
# --------------------------------------------------------------------------


@dataclass
class AnalysisConfig:
    # A closed trade whose NET result (profit + commission + swap + fee) has an
    # absolute value <= this amount (account currency) is classified BREAKEVEN.
    # Breakeven trades are counted in the total-trades denominator of win rate
    # and loss rate, and excluded from gross profit / gross loss.
    breakeven_epsilon: float = 0.0
    # Drawdown episodes deeper than this (% of peak) count for drawdown frequency.
    drawdown_episode_min_pct: float = 1.0
    # Minimum closed trades for a period to be considered statistically usable.
    min_trades_for_verdict: int = 20
    # Martingale / lot escalation: volume multiplier after a loss that is flagged.
    lot_escalation_factor: float = 1.5
    # Strategy classification patterns (regex, case-insensitive) on the EA comment.
    strategy_comment_patterns: dict = field(default_factory=lambda: {
        "BREAKOUT": r"(breakout|\bbrk\b|\bbo\b|\bbrk[_\-])",
        "MOMENTUM": r"(momentum|scalp|\bmom\b|\bms\b|\bmom[_\-])",
        "PULLBACK": r"(pullback|pull[ _-]?back|\bpb\b|\bpb[_\-])",
    })
    # Optional explicit magic-number -> strategy map supplied by the user.
    # Empty by default: SVPI never guesses which magic belongs to which strategy.
    magic_strategy_map: dict = field(default_factory=dict)
    # Basket / layer identification in comments (regex with one capture group).
    basket_id_pattern: str = r"(?:basket|bsk|grp|group)[ _#:=-]*([A-Za-z0-9]+)"
    layer_pattern: str = r"(?:layer|lyr|\bl)[ _#:=-]*([1-9])\b"
    # Anomaly detection
    anomaly_z_threshold: float = 3.0
    anomaly_min_history_trades: int = 30
    # Significance level for the win-rate deterioration test.
    win_rate_alpha: float = 0.05
    # Transaction cost materiality: costs as % of gross profit.
    cost_materiality_pct: float = 10.0


# --------------------------------------------------------------------------
# Report status criteria (explicit and configurable)
# --------------------------------------------------------------------------


@dataclass
class StatusCriteria:
    # Any VERIFIED breach of these checks -> TEMPORARILY STOP LIVE TRADING.
    critical_checks: tuple = (
        "max_equity_drawdown",
        "daily_hard_stop",
        "max_layers",
        "martingale",
        "weekly_loss_lock",
    )
    # Any VERIFIED breach of these -> NEEDS ATTENTION.
    warning_checks: tuple = (
        "daily_loss_lock",
        "daily_profit_lock",
        "max_basket_risk",
        "stop_loss_presence",
        "lot_escalation",
    )
    # Closed-trade drawdown (% of peak balance) treated as severe.
    severe_drawdown_pct: float = 10.0
    # Profit-factor decline vs previous comparable period that triggers attention (%).
    pf_decline_attention_pct: float = 30.0
    # Anomaly severities that trigger attention.
    attention_anomaly_severities: tuple = ("HIGH", "CRITICAL")
    # Unexplained anomalies with this severity -> STOP.
    stop_anomaly_severities: tuple = ("CRITICAL",)


# --------------------------------------------------------------------------
# AI analyst / privacy
# --------------------------------------------------------------------------


@dataclass
class AIConfig:
    # External AI is OFF by default. Even when enabled, only an aggregated fact
    # sheet (never raw statements) is sent, and only after explicit consent.
    external_ai_enabled: bool = False
    provider: str = "anthropic"
    model: str = "claude-opus-5-5"
    max_tokens: int = 2000


@dataclass
class NotificationConfig:
    enabled: bool = False
    method: str = "log"  # "log" | "email"
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    email_to: str = ""
    # Password is read from the SVPI_SMTP_PASSWORD environment variable only.


@dataclass
class SVPIConfig:
    risk: RiskConfig = field(default_factory=RiskConfig)
    analysis: AnalysisConfig = field(default_factory=AnalysisConfig)
    status: StatusCriteria = field(default_factory=StatusCriteria)
    ai: AIConfig = field(default_factory=AIConfig)
    notify: NotificationConfig = field(default_factory=NotificationConfig)
    server_timezone: str = SERVER_TIMEZONE
    report_timezone: str = REPORT_TIMEZONE

    def to_dict(self) -> dict:
        return {
            "risk": self.risk.to_dict(),
            "analysis": asdict(self.analysis),
            "status": {k: list(v) if isinstance(v, tuple) else v
                       for k, v in asdict(self.status).items()},
            "ai": asdict(self.ai),
            "notify": asdict(self.notify),
            "server_timezone": self.server_timezone,
            "report_timezone": self.report_timezone,
        }


def _merge(dc_cls, values: dict):
    inst = dc_cls()
    for k, v in (values or {}).items():
        if k in dc_cls.__dataclass_fields__:
            cur = getattr(inst, k)
            if isinstance(cur, tuple) and isinstance(v, list):
                v = tuple(v)
            setattr(inst, k, v)
    return inst


def load_config(path: Path | None = None) -> SVPIConfig:
    """Load config, overlaying user overrides from svpi_settings.json if present."""
    path = Path(path) if path else USER_CONFIG_PATH
    cfg = SVPIConfig()
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
        if "risk" in data:
            cfg.risk = RiskConfig.from_dict(data["risk"])
        if "analysis" in data:
            cfg.analysis = _merge(AnalysisConfig, data["analysis"])
        if "status" in data:
            cfg.status = _merge(StatusCriteria, data["status"])
        if "ai" in data:
            cfg.ai = _merge(AIConfig, data["ai"])
        if "notify" in data:
            cfg.notify = _merge(NotificationConfig, data["notify"])
        cfg.server_timezone = data.get("server_timezone", cfg.server_timezone)
        cfg.report_timezone = data.get("report_timezone", cfg.report_timezone)
    return cfg


def save_config(cfg: SVPIConfig, path: Path | None = None) -> Path:
    path = Path(path) if path else USER_CONFIG_PATH
    path.write_text(json.dumps(cfg.to_dict(), indent=2), encoding="utf-8")
    return path


def ensure_directories() -> None:
    for d in (INCOMING_DIR, PROCESSED_DIR, ARCHIVE_DIR, WEEKLY_REPORTS_DIR,
              MONTHLY_REPORTS_DIR, DATABASE_DIR, LOG_DIR, BACKUP_DIR):
        d.mkdir(parents=True, exist_ok=True)
