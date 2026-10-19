"""Central configuration. Everything tunable lives here (thresholds are
management parameters, not universal standards - see proposal section 4.2)."""
import os
from pathlib import Path
from typing import TypedDict


class DatabaseConfig(TypedDict):
    host: str
    port: int
    user: str
    dbname: str
    password: str | None

ROOT = Path(__file__).resolve().parents[1]
DATA_XLSX = ROOT / "data" / "national_service_posting_dummy_data_50000.xlsx"
OUT = ROOT / "outputs"
SQL = ROOT / "sql"

DB: DatabaseConfig = {
    "host": "127.0.0.1",
    "port": 5432,
    "user": "postgres",
    "dbname": os.environ.get("NSME_DB_NAME", "ns_me"),
    "password": os.environ.get("NSME_DB_PASSWORD"),
}
SCHEMA = "me"

SEED = 42
CYCLE = "2026/27"
CYCLE_START = "2026-11-01"      # 12 monthly reporting periods
N_PERIODS = 12

# Indicator catalogue: id, name, level, unit, baseline, target, source, method
INDICATORS = [
    ("IND01", "Deployment (reporting-for-duty) rate", "OUTPUT", "%", 70, 98,
     "Posting + monthly reports", "Posted personnel who have reported for duty / posted personnel x 100"),
    ("IND02", "Monitoring visits per 100 active personnel", "ACTIVITY", "visits/100", 15, 30,
     "Monitoring visit records", "Total visits in month / active personnel x 100"),
    ("IND03", "Supervisor evaluation submission rate", "OUTPUT", "%", 60, 90,
     "Supervisor evaluation records", "Evaluations submitted / active personnel x 100"),
    ("IND04", "Average attendance rate", "OUTPUT", "%", 80, 90,
     "Monthly attendance returns", "Mean % of expected days present among active personnel"),
    ("IND05", "Mean supervisor rating (1-5)", "OUTCOME", "score", 3.0, 4.0,
     "Supervisor evaluation records", "Mean rating over submitted evaluations"),
    ("IND06", "Service retention / completion rate", "OUTCOME", "%", 85, 95,
     "Service completion records", "Personnel still in service (or completed) / posted x 100"),
    ("IND07", "Attendance-return submission compliance", "OUTPUT", "%", 50, 95,
     "Attendance returns (agency submissions)", "Returns submitted / personnel expected x 100"),
]
# Per-indicator (green, yellow) thresholds on achievement rate; default proposal rule 95 / 80
THRESHOLDS = {i[0]: (95.0, 80.0) for i in INDICATORS}

# Early-warning model
RISK_HORIZON = 2                 # months ahead the label looks
RISK_COMPOSITE_THRESHOLD = 90.0  # future mean composite achievement below this => at risk
TRAIN_LAST_PERIOD = 5            # purged temporal split: label of period 5 uses months 6-7
TEST_FIRST_PERIOD = 8
