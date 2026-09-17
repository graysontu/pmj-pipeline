"""The census is what decides a job is dead, so its failure modes matter more
than its happy path. Every test here that expects CensusError is guarding against
a class of false closure."""

from unittest.mock import MagicMock, patch

import httpx
import pytest

from pipeline.census import (
    CensusError,
    census_greenhouse,
    census_lever,
    census_smartrecruiters,
    census_workable,
    run_census,
)


def _client() -> MagicMock:
    return MagicMock(spec=httpx.Client)


def _json_response(payload, content_type="application/json"):
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json.return_value = payload
    response.headers = {"content-type": content_type}
    return response


def _html_response():
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json.side_effect = ValueError("Expecting value: line 1 column 1 (char 0)")
    return response


# --- Greenhouse ------------------------------------------------------------


def test_greenhouse_collects_ids():
    client = _client()
    client.request.return_value = _json_response(
        {"jobs": [{"id": 111}, {"id": 222}], "meta": {"total": 2}}
    )

    ids, declared = census_greenhouse(client, "acme")

    assert ids == {"111", "222"}
    assert declared == 2


def test_greenhouse_short_listing_is_an_error_not_mass_closure():
    """A board that declares 50 postings but returns 2 is a broken response. If
    this returned successfully, 48 live jobs would be marked closed."""
    client = _client()
    client.request.return_value = _json_response(
        {"jobs": [{"id": 111}, {"id": 222}], "meta": {"total": 50}}
    )

    with pytest.raises(CensusError, match="incomplete listing"):
        census_greenhouse(client, "acme")


def test_greenhouse_html_response_is_an_error():
    """A 200 carrying HTML is a bot block or interstitial, not an empty board."""
    client = _client()
    client.request.return_value = _html_response()

    with pytest.raises(CensusError, match="non-JSON"):
        census_greenhouse(client, "acme")


def test_greenhouse_missing_jobs_key_is_an_error():
    client = _client()
    client.request.return_value = _json_response({"meta": {"total": 0}})

    with pytest.raises(CensusError, match="no 'jobs' key"):
        census_greenhouse(client, "acme")


def test_greenhouse_genuinely_empty_board_is_allowed():
    """Greenhouse 404s on a bad slug, so a 200 with zero jobs really is an empty
    board and is safe to treat as authoritative."""
    client = _client()
    client.request.return_value = _json_response({"jobs": [], "meta": {"total": 0}})

    ids, _ = census_greenhouse(client, "acme")

    assert ids == frozenset()


# --- Lever -----------------------------------------------------------------


def test_lever_collects_ids():
    client = _client()
    client.request.return_value = _json_response([{"id": "abc-123"}, {"id": "def-456"}])

    ids, declared = census_lever(client, "acme")

    assert ids == {"abc-123", "def-456"}
    assert declared is None


def test_lever_dict_payload_is_an_error():
    client = _client()
    client.request.return_value = _json_response({"error": "nope"})

    with pytest.raises(CensusError, match="unexpected Lever payload"):
        census_lever(client, "acme")


# --- Workable --------------------------------------------------------------


def test_workable_pages_past_the_discovery_fetcher_limit():
    """The discovery fetcher stops at 10 pages and silently truncates. Workable
    pages 10 jobs at a time, so a 101-job board needs 11 - the census must not
    inherit that truncation."""
    client = _client()
    pages = []
    for page_index in range(11):
        results = [{"id": page_index * 10 + n, "state": "published"} for n in range(10)]
        if page_index == 10:
            results = [{"id": 100, "state": "published"}]
        pages.append(
            _json_response(
                {"total": 101, "results": results,
                 "nextPage": None if page_index == 10 else f"t{page_index + 1}"}
            )
        )
    client.request.side_effect = pages

    ids, declared = census_workable(client, "acme")

    assert len(ids) == 101
    assert declared == 101


def test_workable_truncated_walk_is_an_error():
    client = _client()
    client.request.return_value = _json_response(
        {"total": 40, "results": [{"id": 1}, {"id": 2}], "nextPage": None}
    )

    with pytest.raises(CensusError, match="incomplete listing"):
        census_workable(client, "acme")


def test_workable_ignores_unpublished_states():
    client = _client()
    client.request.return_value = _json_response(
        {
            "total": 2,
            "results": [
                {"id": 1, "state": "published"},
                {"id": 2, "state": "draft"},
                {"id": 3},
            ],
            "nextPage": None,
        }
    )

    # total=2 counts the published pair; the draft is excluded and the
    # state-less entry is treated as published.
    ids, _ = census_workable(client, "acme")

    assert ids == {"1", "3"}


# --- SmartRecruiters -------------------------------------------------------


def test_smartrecruiters_collects_ids_across_pages():
    client = _client()
    client.request.side_effect = [
        _json_response({"totalFound": 3, "content": [{"id": "a"}, {"id": "b"}]}),
        _json_response({"totalFound": 3, "content": [{"id": "c"}]}),
    ]

    ids, declared = census_smartrecruiters(client, "acme")

    assert ids == {"a", "b", "c"}
    assert declared == 3


def test_smartrecruiters_empty_result_is_an_error():
    """SmartRecruiters answers 200 with zero postings for a nonexistent slug, so
    an empty list proves nothing and must not close that company's jobs."""
    client = _client()
    client.request.return_value = _json_response({"totalFound": 0, "content": []})

    with pytest.raises(CensusError, match="not evidence of closure"):
        census_smartrecruiters(client, "acme")


# --- run_census ------------------------------------------------------------


SOURCES = {"greenhouse": [{"slug": "good", "company_name": "Good Co"},
                          {"slug": "bad", "company_name": "Bad Co"}]}


def test_run_census_marks_failures_without_raising():
    def fake(client, slug):
        if slug == "bad":
            raise httpx.ConnectTimeout("timed out")
        return frozenset({"1"}), 1

    with patch.dict("pipeline.census.CENSUS_FNS", {"greenhouse": fake}):
        results = run_census(SOURCES)

    assert results[("greenhouse", "good")].ok is True
    assert results[("greenhouse", "good")].open_ids == {"1"}

    bad = results[("greenhouse", "bad")]
    assert bad.ok is False
    assert bad.open_ids == frozenset()
    assert "ConnectTimeout" in bad.error


def test_run_census_reports_http_status_errors():
    def fake(client, slug):
        response = MagicMock()
        response.status_code = 404
        raise httpx.HTTPStatusError("nope", request=MagicMock(), response=response)

    with patch.dict("pipeline.census.CENSUS_FNS", {"greenhouse": fake}):
        results = run_census(SOURCES)

    assert all(not c.ok for c in results.values())
    assert results[("greenhouse", "good")].error == "HTTP 404"


def test_run_census_flags_unknown_source_type():
    with patch.dict("pipeline.census.CENSUS_FNS", {}, clear=True):
        results = run_census(SOURCES)

    assert not results[("greenhouse", "good")].ok
    assert "no census function" in results[("greenhouse", "good")].error
