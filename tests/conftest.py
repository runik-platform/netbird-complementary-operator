import os
import sys
from pathlib import Path

os.environ.setdefault("NB_MGMT_URL", "https://nb.test")
os.environ.setdefault("NB_API_KEY", "test-pat")
os.environ.setdefault("NB_RATE_LIMIT_BASE_DELAY", "0")
os.environ.setdefault("NB_RATE_LIMIT_MAX_DELAY", "0")
os.environ.setdefault("NB_RATE_LIMIT_MAX_RETRIES", "2")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
