from datetime import datetime, timedelta, timezone

from pipeline.census import BoardCensus
from pipeline.closure import (
    CLOSED,
    OPEN,
    UNKNOWN,
    apply_verdicts,
    build_source_index,
    reconcile,
)

NOW = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)

SOURCES = {
    "greenhouse": [{"slug": "acme", "company_name": "Acme Living"}],
    "workable": [{"slug": "beta-co", "company_name": "Beta Co"}],
}
INDEX = build_source_index(SOURCES)


def _job(source_id="greenhouse_111", company="Acme Living", days_old=10, **extra):
    return {
        "source_id": source_id,
        "company": company,
        "title": "Property Manager",
        "apply_url": "https://boards.greenhouse.io/acme/jobs/111?gh_src=abc123",
        "published_at": (NOW - timedelta(days=days_old)).isoformat(),
        "category": "Property Manager Jobs",
        "rewritten_description": "<p>x</p>",
        "location": "Dallas, TX",
        "date_posted": (NOW - timedelta(days=days_old)).isoformat(),
        **extra,
    }


def _census(open_ids=("111",), ok=True, error=None, source_type="greenhouse", slug="acme"):
    return BoardCensus(
        source_type=source_type, slug=slug, company_name="Acme Living",
        ok=ok, open_ids=frozenset(open_ids), error=error,
    )


class FakeState:
    """Minimal stand-in exercising the same closure API as pipeline.state.State."""

    def __init__(self, jobs):
        self._data = {j["source_id"]: dict(j) for j in jobs}

    def record_absent(self, source_id, strikes, reason, now=None):
        entry = self._data[source_id]
        entry["closure_misses"] = strikes
        entry["closure_reason"] = reason

    def mark_closed(self, source_id, reason, now=None):
        self._data[source_id].setdefault("closed_at", (now or NOW).isoformat())

    def clear_closure(self, source_id, now=None):
        entry = self._data[source_id]
        was_closed = bool(entry.get("closed_at"))
        entry.pop("closed_at", None)
        entry["closure_misses"] = 0
        return was_closed


# --- verdicts --------------------------------------------------------------


def test_listed_requisition_is_open():
    report = reconcile([_job()], {("greenhouse", "acme"): _census(["111"])}, INDEX, 2, NOW)

    assert [v.verdict for v in report.verdicts] == [OPEN]
    assert report.removals == []


def test_absent_requisition_is_closed_but_not_removed_on_first_miss():
    report = reconcile([_job()], {("greenhouse", "acme"): _census(["999"])}, INDEX, 2, NOW)

    verdict = report.verdicts[0]
    assert verdict.verdict == CLOSED
    assert verdict.strikes == 1
    assert verdict.will_remove is False
    assert report.removals == []


def test_second_consecutive_miss_removes():
    job = _job(closure_misses=1)
    report = reconcile([job], {("greenhouse", "acme"): _census(["999"])}, INDEX, 2, NOW)

    verdict = report.verdicts[0]
    assert verdict.strikes == 2
    assert verdict.will_remove is True
    assert len(report.removals) == 1


def test_strikes_one_removes_immediately():
    """The setting the one-time approved backlog cleanup uses."""
    report = reconcile([_job()], {("greenhouse", "acme"): _census(["999"])}, INDEX, 1, NOW)

    assert report.verdicts[0].will_remove is True


def test_failed_board_is_unknown_not_closed():
    censuses = {("greenhouse", "acme"): _census(ok=False, error="HTTP 503", open_ids=())}
    report = reconcile([_job(closure_misses=1)], censuses, INDEX, 2, NOW)

    verdict = report.verdicts[0]
    assert verdict.verdict == UNKNOWN
    assert verdict.will_remove is False
    # The prior strike is carried, not advanced: nothing was proven this run.
    assert verdict.strikes == 1
    assert "503" in verdict.reason


def test_company_dropped_from_sources_is_unknown():
    report = reconcile(
        [_job(company="Gone Co")], {("greenhouse", "acme"): _census()}, INDEX, 2, NOW
    )

    assert report.verdicts[0].verdict == UNKNOWN
    assert "no longer in sources.yaml" in report.verdicts[0].reason


def test_company_that_changed_ats_is_unknown():
    """Beta Co is configured as workable now; its old greenhouse jobs cannot be
    checked against a workable board, so they must not be closed."""
    job = _job(source_id="greenhouse_555", company="Beta Co")
    censuses = {("workable", "beta-co"): _census(source_type="workable", slug="beta-co")}
    report = reconcile([job], censuses, INDEX, 2, NOW)

    assert report.verdicts[0].verdict == UNKNOWN
    assert "no longer polled" in report.verdicts[0].reason


def test_missing_census_is_unknown():
    report = reconcile([_job()], {}, INDEX, 2, NOW)

    assert report.verdicts[0].verdict == UNKNOWN
    assert "no census was attempted" in report.verdicts[0].reason


def test_unparseable_source_id_is_unknown():
    report = reconcile(
        [_job(source_id="legacyid")], {("greenhouse", "acme"): _census()}, INDEX, 2, NOW
    )

    assert report.verdicts[0].verdict == UNKNOWN
    assert "no requisition part" in report.verdicts[0].reason


def test_apply_url_query_parameters_are_preserved_in_the_verdict():
    report = reconcile([_job()], {("greenhouse", "acme"): _census(["999"])}, INDEX, 2, NOW)

    assert report.verdicts[0].apply_url.endswith("?gh_src=abc123")


def test_report_counts_and_summary():
    jobs = [
        _job(source_id="greenhouse_111"),
        _job(source_id="greenhouse_222"),
        _job(source_id="greenhouse_333", company="Gone Co"),
    ]
    censuses = {("greenhouse", "acme"): _census(["111"])}
    report = reconcile(jobs, censuses, INDEX, 1, NOW)

    assert report.checked == 3
    assert len(report.open_jobs) == 1
    assert len(report.closed_jobs) == 1
    assert len(report.unknown_jobs) == 1
    assert len(report.removals) == 1
    assert report.boards_ok == 1
    assert report.boards_failed == 0
    assert any("Removals: 1" in line for line in report.summary_lines())


# --- apply_verdicts --------------------------------------------------------


def test_apply_removes_only_at_the_strike_threshold():
    jobs = [_job(source_id="greenhouse_111"), _job(source_id="greenhouse_222", closure_misses=1)]
    state = FakeState(jobs)
    censuses = {("greenhouse", "acme"): _census(["999"])}
    report = reconcile(jobs, censuses, INDEX, 2, NOW)

    removed, reinstated = apply_verdicts(state, report, NOW)

    assert (removed, reinstated) == (1, 0)
    assert "closed_at" not in state._data["greenhouse_111"]
    assert state._data["greenhouse_111"]["closure_misses"] == 1
    assert state._data["greenhouse_222"]["closed_at"]


def test_apply_never_rewrites_publication_date_or_identity():
    job = _job(closure_misses=1)
    original_published_at = job["published_at"]
    original_url = job["apply_url"]
    state = FakeState([job])
    report = reconcile([job], {("greenhouse", "acme"): _census(["999"])}, INDEX, 2, NOW)

    apply_verdicts(state, report, NOW)

    entry = state._data["greenhouse_111"]
    assert entry["published_at"] == original_published_at
    assert entry["apply_url"] == original_url
    assert entry["source_id"] == "greenhouse_111"


def test_reopened_requisition_is_reinstated_with_its_original_date():
    job = _job(closed_at=NOW.isoformat(), closure_misses=2)
    original_published_at = job["published_at"]
    state = FakeState([job])
    report = reconcile([job], {("greenhouse", "acme"): _census(["111"])}, INDEX, 2, NOW)

    removed, reinstated = apply_verdicts(state, report, NOW)

    assert (removed, reinstated) == (0, 1)
    assert "closed_at" not in state._data["greenhouse_111"]
    assert state._data["greenhouse_111"]["published_at"] == original_published_at


def test_unknown_verdict_writes_nothing():
    job = _job(closure_misses=1)
    state = FakeState([job])
    censuses = {("greenhouse", "acme"): _census(ok=False, error="timeout", open_ids=())}
    report = reconcile([job], censuses, INDEX, 2, NOW)

    removed, reinstated = apply_verdicts(state, report, NOW)

    assert (removed, reinstated) == (0, 0)
    # The strike counter neither advances nor resets on an inconclusive check.
    assert state._data["greenhouse_111"]["closure_misses"] == 1
    assert "closed_at" not in state._data["greenhouse_111"]


def test_open_verdict_resets_the_strike_counter():
    """A job that went missing once and came back must start over, so intermittent
    board flakiness cannot accumulate into a removal."""
    job = _job(closure_misses=1)
    state = FakeState([job])
    report = reconcile([job], {("greenhouse", "acme"): _census(["111"])}, INDEX, 2, NOW)

    apply_verdicts(state, report, NOW)

    assert state._data["greenhouse_111"]["closure_misses"] == 0


# --- already-closed accounting ---------------------------------------------


def test_already_closed_job_is_not_re_proposed_for_removal():
    """Without this, every daily report would keep re-proposing the whole
    historical backlog and the summary would never settle."""
    job = _job(closed_at=NOW.isoformat(), closure_misses=2)
    report = reconcile([job], {("greenhouse", "acme"): _census(["999"])}, INDEX, 2, NOW)

    verdict = report.verdicts[0]
    assert verdict.verdict == CLOSED
    assert verdict.already_closed is True
    assert verdict.will_remove is False
    assert report.removals == []
    assert len(report.already_closed_jobs) == 1
    assert any("already out of the feed" in line for line in report.summary_lines())


def test_already_closed_job_is_not_rewritten_in_state():
    """Keeps state.json (and the daily commit) stable instead of churning."""
    job = _job(closed_at=NOW.isoformat(), closure_misses=2, closure_reason="original reason")
    state = FakeState([job])
    report = reconcile([job], {("greenhouse", "acme"): _census(["999"])}, INDEX, 2, NOW)

    removed, reinstated = apply_verdicts(state, report, NOW)

    assert (removed, reinstated) == (0, 0)
    entry = state._data["greenhouse_111"]
    assert entry["closure_misses"] == 2
    assert entry["closure_reason"] == "original reason"
