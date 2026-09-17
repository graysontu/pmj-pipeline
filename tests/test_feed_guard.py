"""The guard exists so a failed or incomplete run cannot publish a gutted feed.
Every refusal must leave the previous feed byte-for-byte intact."""

from datetime import datetime, timedelta, timezone

import pytest
from lxml import etree

from pipeline.output_xml import FeedGuardTripped, count_feed_jobs, generate_feed_xml
from pipeline.state import State

NOW = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)


def _job(n: int) -> dict:
    return {
        "source_id": f"greenhouse_{n}",
        "title": f"Property Manager {n}",
        "company": "Acme Living",
        "location": "Dallas, TX",
        "category": "Property Manager Jobs",
        "apply_url": f"https://boards.greenhouse.io/acme/jobs/{n}?gh_src=keepme",
        "rewritten_description": "<p>Role description.</p>",
        "published_at": (NOW - timedelta(days=5)).isoformat(),
        "date_posted": (NOW - timedelta(days=5)).isoformat(),
        "job_type": "fulltime",
        "remote_type": "onsite",
        "company_url": "https://boards.greenhouse.io/acme",
    }


def _jobs(count: int) -> list[dict]:
    return [_job(n) for n in range(count)]


def test_writes_feed_and_counts_jobs(tmp_path):
    path = tmp_path / "feed.xml"

    generate_feed_xml(_jobs(10), path=path)

    assert count_feed_jobs(path) == 10
    assert etree.parse(str(path)).getroot().tag == "source"


def test_no_temp_file_is_left_behind(tmp_path):
    path = tmp_path / "feed.xml"

    generate_feed_xml(_jobs(3), path=path)

    assert list(tmp_path.iterdir()) == [path]


def test_first_run_with_no_previous_feed_is_allowed(tmp_path):
    path = tmp_path / "feed.xml"

    generate_feed_xml(_jobs(5), path=path, max_removal_fraction=0.15)

    assert count_feed_jobs(path) == 5


def test_empty_feed_is_refused_when_jobs_are_live(tmp_path):
    path = tmp_path / "feed.xml"
    generate_feed_xml(_jobs(10), path=path)
    before = path.read_bytes()

    with pytest.raises(FeedGuardTripped, match="empty feed"):
        generate_feed_xml([], path=path, max_removal_fraction=0.15)

    assert path.read_bytes() == before


def test_large_removal_is_refused(tmp_path):
    path = tmp_path / "feed.xml"
    generate_feed_xml(_jobs(100), path=path)
    before = path.read_bytes()

    with pytest.raises(FeedGuardTripped, match="refusing to remove 50 of 100"):
        generate_feed_xml(_jobs(50), path=path, max_removal_fraction=0.15)

    assert path.read_bytes() == before
    assert count_feed_jobs(path) == 100


def test_removal_inside_the_limit_is_allowed(tmp_path):
    path = tmp_path / "feed.xml"
    generate_feed_xml(_jobs(100), path=path)

    generate_feed_xml(_jobs(90), path=path, max_removal_fraction=0.15)

    assert count_feed_jobs(path) == 90


def test_override_permits_the_one_time_cleanup(tmp_path):
    path = tmp_path / "feed.xml"
    generate_feed_xml(_jobs(613), path=path)

    generate_feed_xml(_jobs(254), path=path, max_removal_fraction=0.15, allow_mass_removal=True)

    assert count_feed_jobs(path) == 254


def test_override_does_not_permit_an_empty_feed(tmp_path):
    """An empty feed is a failed run under any override."""
    path = tmp_path / "feed.xml"
    generate_feed_xml(_jobs(10), path=path)

    with pytest.raises(FeedGuardTripped, match="empty feed"):
        generate_feed_xml([], path=path, max_removal_fraction=0.15, allow_mass_removal=True)


def test_growing_the_feed_is_never_blocked(tmp_path):
    path = tmp_path / "feed.xml"
    generate_feed_xml(_jobs(10), path=path)

    generate_feed_xml(_jobs(400), path=path, max_removal_fraction=0.15)

    assert count_feed_jobs(path) == 400


def test_guard_is_skipped_when_not_requested(tmp_path):
    """scripts/make_test_feed.py writes a 3-job feed on purpose."""
    path = tmp_path / "feed.xml"
    generate_feed_xml(_jobs(100), path=path)

    generate_feed_xml(_jobs(3), path=path)

    assert count_feed_jobs(path) == 3


def test_corrupt_previous_feed_does_not_block_publishing(tmp_path):
    path = tmp_path / "feed.xml"
    path.write_text("<source><job>truncated", encoding="utf-8")

    generate_feed_xml(_jobs(10), path=path, max_removal_fraction=0.15)

    assert count_feed_jobs(path) == 10


def test_requisition_query_parameters_survive_into_the_feed(tmp_path):
    path = tmp_path / "feed.xml"

    generate_feed_xml(_jobs(1), path=path)

    urls = [el.text for el in etree.parse(str(path)).getroot().iter("url")]
    assert urls == ["https://boards.greenhouse.io/acme/jobs/0?gh_src=keepme"]


# --- state retention -------------------------------------------------------


def test_closed_jobs_leave_the_feed_but_stay_in_state(tmp_path, monkeypatch):
    """Retention rule: a job stays published until confirmed closed or aged out.
    Being absent from a day's discovery batch is irrelevant here."""
    import pipeline.state as state_module

    state_path = tmp_path / "state.json"
    monkeypatch.setattr(state_module, "STATE_PATH", state_path)

    state = state_module.State()
    state._data = {
        "greenhouse_1": _job(1),
        "greenhouse_2": {**_job(2), "closed_at": NOW.isoformat()},
        "greenhouse_3": {
            **_job(3),
            "published_at": (NOW - timedelta(days=90)).isoformat(),
        },
    }

    active = {j["source_id"] for j in state.get_active_jobs()}
    published = {j["source_id"] for j in state.get_published_jobs()}

    assert active == {"greenhouse_1"}
    # The closed job is still tracked, so a reposted requisition can reopen it;
    # the 90-day-old one is outside the age window entirely.
    assert published == {"greenhouse_1", "greenhouse_2"}
    assert state.closed_count() == 1
