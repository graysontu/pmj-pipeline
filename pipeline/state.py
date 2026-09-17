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

    def get_published_jobs(self) -> list[dict]:
        """Every job inside the ACTIVE_DAYS window, including ones already
        confirmed closed. This is the set the closure check reconciles against -
        it has to see closed jobs so a reopened requisition can be reinstated."""
        cutoff = datetime.now(tz=timezone.utc) - timedelta(days=ACTIVE_DAYS)
        jobs = []
        for v in self._data.values():
            try:
                published = datetime.fromisoformat(v["published_at"]).astimezone(timezone.utc)
            except (ValueError, TypeError, KeyError):
                continue
            if published >= cutoff:
                jobs.append(v)
        return jobs

    def get_active_jobs(self) -> list[dict]:
        """The jobs that belong in the feed: inside the ACTIVE_DAYS window and not
        confirmed closed. Age expiry is unchanged; closure is the new exclusion."""
        return [j for j in self.get_published_jobs() if not j.get("closed_at")]

    # --- closure bookkeeping -------------------------------------------------
    # These only ever write the closure_* / closed_at / last_seen_open keys.
    # published_at and every content field are left untouched, so a job that
    # closes and reopens keeps its original publication date and identity.

    def record_absent(
        self, source_id: str, strikes: int, reason: str, now: datetime | None = None
    ) -> None:
        entry = self._data.get(source_id)
        if entry is None:
            return
        entry["closure_misses"] = strikes
        entry["closure_reason"] = reason
        entry["closure_checked_at"] = (now or datetime.now(tz=timezone.utc)).isoformat()

    def mark_closed(self, source_id: str, reason: str, now: datetime | None = None) -> None:
        entry = self._data.get(source_id)
        if entry is None:
            return
        if entry.get("closed_at"):
            return
        entry["closed_at"] = (now or datetime.now(tz=timezone.utc)).isoformat()
        entry["closure_reason"] = reason
        logger.info("Confirmed closed, removing from feed: %s (%s)", source_id, entry.get("title"))

    def clear_closure(self, source_id: str, now: datetime | None = None) -> bool:
        """Record a job as seen open. Returns True if this reinstated a job that
        had previously been confirmed closed (a reposted requisition keeping its
        original ID)."""
        entry = self._data.get(source_id)
        if entry is None:
            return False
        was_closed = bool(entry.get("closed_at"))
        entry.pop("closed_at", None)
        entry.pop("closure_reason", None)
        entry["closure_misses"] = 0
        entry["last_seen_open"] = (now or datetime.now(tz=timezone.utc)).isoformat()
        if was_closed:
            logger.info(
                "Reinstating previously closed job now listed again: %s (%s)",
                source_id, entry.get("title"),
            )
        return was_closed

    def closed_count(self) -> int:
        return sum(1 for v in self._data.values() if v.get("closed_at"))

    def save(self) -> None:
        _save(self._data)
