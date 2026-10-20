"""Configuration for Options Whale Radar."""
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
LOGS_DIR = BASE_DIR / "logs"
REPORTS_DIR = BASE_DIR / "reports"
UNIVERSE_FILE = BASE_DIR / "universe.txt"
DB_PATH = DATA_DIR / "whale.db"

TIMEZONE = "America/Winnipeg"

# Premium tiers (USD) used for unusual-activity thresholds.
PREMIUM_TIERS = [250_000, 500_000, 1_000_000, 2_500_000, 5_000_000]

# A contract is flagged as unusual if ANY of these hold.
MIN_PREMIUM_FLAG = 250_000      # estimated premium in USD
MIN_VOL_OI_FLAG = 3.0           # volume / open interest
MIN_VOLUME_FLAG = 1000          # contracts traded

REQUESTS_PER_SEC = 1.0
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# Classification confidence can NEVER exceed this: our free delayed chain
# snapshots carry no time & sales, so execution direction (bid vs ask) is
# always unknown.
MAX_CLASSIFIER_CONFIDENCE = 65

for _d in (DATA_DIR, LOGS_DIR, REPORTS_DIR):
    _d.mkdir(parents=True, exist_ok=True)
