import os
from pathlib import Path


def positive_int(name: str, default: int) -> int:
    value = int(os.getenv(name, str(default)))
    if value < 1:
        raise ValueError(f"{name} must be positive")
    return value


ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("DATA_DIR", str(ROOT / "data"))).resolve()
MAX_DURATION = positive_int("MAX_DURATION_SECONDS", 1200)
MAX_FILE_MB = positive_int("MAX_FILE_MB", 80)
MAX_CACHE_MB = positive_int("MAX_CACHE_MB", 512)
MAX_JOBS = positive_int("MAX_JOBS", 12)
DOWNLOAD_WORKERS = positive_int("DOWNLOAD_WORKERS", 1)
SEARCH_TIMEOUT = positive_int("SEARCH_TIMEOUT_SECONDS", 60)
JOB_TIMEOUT = positive_int("JOB_TIMEOUT_SECONDS", 300)
FILE_TTL = positive_int("FILE_TTL_SECONDS", 3600)
APP_KEY = os.getenv("APP_KEY", "")
FFMPEG_LOCATION = os.getenv("FFMPEG_LOCATION", "")
