"""The closure stage is wired into a pipeline that runs unattended, so the
important guarantees are about what it does NOT do: nothing when the flag is off,
and no removals when the census itself fails."""

from unittest.mock import patch

import pipeline.main as main
from pipeline.state import State


class FakeState(State):
    def __init__(self, data=None):
        self._data = data or {}


def test_stage_is_inert_when_the_flag_is_off():
    state = FakeState()

    with patch.object(main, "CLOSURE_CHECK_ENABLED", False), \
         patch.object(main, "run_census") as census:
        notices, removed, reinstated = main._closure_stage(state)

    census.assert_not_called()
    assert (notices, removed, reinstated) == ([], 0, 0)


def test_census_crash_removes_nothing_and_reports():
    state = FakeState()

    with patch.object(main, "CLOSURE_CHECK_ENABLED", True), \
         patch.object(main, "run_census", side_effect=RuntimeError("network down")):
        notices, removed, reinstated = main._closure_stage(state)

    assert removed == 0
    assert reinstated == 0
    assert len(notices) == 1
    assert "network down" in notices[0]


def test_mostly_failed_census_raises_a_notice():
    """Half the boards failing is systemic, not a couple of dead slugs."""
    from pipeline.census import BoardCensus

    censuses = {
        ("greenhouse", "a"): BoardCensus("greenhouse", "a", "A Co", True, frozenset({"1"})),
        ("greenhouse", "b"): BoardCensus("greenhouse", "b", "B Co", False, frozenset(), error="HTTP 503"),
        ("greenhouse", "c"): BoardCensus("greenhouse", "c", "C Co", False, frozenset(), error="timeout"),
    }

    with patch.object(main, "CLOSURE_CHECK_ENABLED", True), \
         patch.object(main, "run_census", return_value=censuses), \
         patch.object(main, "SOURCES", {"greenhouse": []}):
        notices, removed, _ = main._closure_stage(FakeState())

    assert removed == 0
    assert any("boards were unavailable" in n for n in notices)


def test_publish_feed_applies_the_guard(tmp_path):
    from pipeline.output_xml import FeedGuardTripped, generate_feed_xml

    path = tmp_path / "feed.xml"
    jobs = {
        f"greenhouse_{n}": {
            "source_id": f"greenhouse_{n}",
            "title": "Property Manager",
            "company": "Acme Living",
            "location": "Dallas, TX",
            "category": "Property Manager Jobs",
            "apply_url": "https://example.com/1",
            "rewritten_description": "<p>x</p>",
            "published_at": "2026-09-15T00:00:00+00:00",
            "date_posted": "2026-09-15T00:00:00+00:00",
        }
        for n in range(100)
    }
    generate_feed_xml(list(jobs.values()), path=path)

    # Simulate a run where almost everything vanished from state.
    state = FakeState({k: v for k, v in list(jobs.items())[:10]})

    with patch.object(main, "FEED_PATH", path, create=True), \
         patch("pipeline.main.generate_feed_xml") as gen:
        gen.side_effect = lambda active, **kw: generate_feed_xml(active, path=path, **kw)
        try:
            main._publish_feed(state, allow_mass_removal=False)
            tripped = False
        except FeedGuardTripped:
            tripped = True

    assert tripped is True
    from pipeline.output_xml import count_feed_jobs
    assert count_feed_jobs(path) == 100
