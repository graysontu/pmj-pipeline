import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

STATE_PATH = Path(__file__).parent.parent / "data" / "state.json"
ACTIVE_DAYS = 60


def _load() -> dict:
    if STATE_PATH.exists():
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Could not load state: %s. Starting fresh.", exc)
    return {}


def _save(data: dict) -> None:
    STATE_PATH.parent.mkdir(exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


class State:
    def __init__(self) -> None:
        self._data = _load()

    def is_processed(self, source_id: str) -> bool:
        return source_id in self._data

    def mark_processed(self, source_id: str, published_at: datetime, job_data: dict) -> None:
        self._data[source_id] = {
            "published_at": published_at.isoformat(),
            **job_data,
        }

    def get_active_jobs(self) -> list[dict]:
        cutoff = datetime.now(tz=timezone.utc) - timedelta(days=ACTIVE_DAYS)
        return [
            v for v in self._data.values()
            if datetime.fromisoformat(v["published_at"]).astimezone(timezone.utc) >= cutoff
        ]

    def save(self) -> None:
        _save(self._data)
