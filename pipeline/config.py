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
MAX_JOBS_PER_RUN: int = int(os.getenv("MAX_JOBS_PER_RUN", "11"))


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

# LinkedIn posts (pipeline/linkedin, .github/workflows/linkedin-post.yml). Off by
# default: while false each run still picks a job, draws the image and writes the
# caption to the linkedin-posts branch for review, but sends nothing to Buffer.
# Setting it "true" in the workflow is the single switch that starts real posting;
# back to "false" stops it. See CLAUDE.md, "LinkedIn posts".
LINKEDIN_POSTING_ENABLED: bool = _env_flag("LINKEDIN_POSTING_ENABLED", False)
BUFFER_API_KEY: str = os.getenv("BUFFER_API_KEY", "")
# Only needed if more than one LinkedIn channel is connected in Buffer.
BUFFER_CHANNEL_ID: str = os.getenv("BUFFER_CHANNEL_ID", "")
# Local time the post goes live. The workflow runs earlier and schedules it in
# Buffer, so GitHub's cron delays don't move the post.
LINKEDIN_POST_TIME: str = os.getenv("LINKEDIN_POST_TIME", "10:00")
LINKEDIN_POST_TIMEZONE: str = os.getenv("LINKEDIN_POST_TIMEZONE", "America/New_York")
# A company is not posted again within this many days.
LINKEDIN_COMPANY_COOLDOWN_DAYS: int = int(os.getenv("LINKEDIN_COMPANY_COOLDOWN_DAYS", "14"))
# Only jobs added to the feed within this many days count as a "new job".
LINKEDIN_MAX_JOB_AGE_DAYS: int = int(os.getenv("LINKEDIN_MAX_JOB_AGE_DAYS", "7"))
# Low-paying jobs aren't highlighted on LinkedIn: a job is skipped when the bottom
# of its pay range is at or below these (Grayson's floor, 2026-09-28).
LINKEDIN_MIN_HOURLY_PAY_FLOOR: float = float(os.getenv("LINKEDIN_MIN_HOURLY_PAY_FLOOR", "21"))
LINKEDIN_MIN_YEARLY_PAY_FLOOR: float = float(os.getenv("LINKEDIN_MIN_YEARLY_PAY_FLOOR", "45000"))

_sources_path = Path(__file__).parent.parent / "sources.yaml"

with open(_sources_path, "r") as _f:
    SOURCES: dict[str, Any] = yaml.safe_load(_f)
