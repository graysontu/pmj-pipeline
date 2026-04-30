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

_sources_path = Path(__file__).parent.parent / "sources.yaml"

with open(_sources_path, "r") as _f:
    SOURCES: dict[str, Any] = yaml.safe_load(_f)
