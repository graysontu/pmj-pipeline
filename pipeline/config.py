import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

load_dotenv()

ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
SITE_BASE_URL: str = os.getenv("SITE_BASE_URL", "https://propertymanagementjobs.us")
GOOGLE_INDEXING_CREDENTIALS_JSON: str = os.getenv("GOOGLE_INDEXING_CREDENTIALS_JSON", "")
JOB_MAX_AGE_DAYS: int = int(os.getenv("JOB_MAX_AGE_DAYS", "2"))
MAX_JOBS_PER_RUN: int = int(os.getenv("MAX_JOBS_PER_RUN", "9"))


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


# Daily closure check. Off by default: flipping this to True is the single switch
# that enables removing confirmed-closed jobs from the feed, and back to False is
# the rollback. See CLAUDE.md, "Closure Detection".
CLOSURE_CHECK_ENABLED: bool = _env_flag("CLOSURE_CHECK_ENABLED", False)

# Consecutive confirmed-absent observations before a job leaves the feed. 2 means
# one anomalous-but-complete-looking board listing cannot delist anything.
CLOSURE_STRIKES: int = int(os.getenv("CLOSURE_STRIKES", "2"))

# Share of the live feed a single run may remove before the guard refuses to
# publish and keeps the previous feed. Steady-state churn is a few jobs a day, so
# 15% is generous; the one-time backlog cleanup needs an explicit override.
MAX_FEED_REMOVAL_FRACTION: float = float(os.getenv("MAX_FEED_REMOVAL_FRACTION", "0.15"))

_sources_path = Path(__file__).parent.parent / "sources.yaml"

with open(_sources_path, "r") as _f:
    SOURCES: dict[str, Any] = yaml.safe_load(_f)
