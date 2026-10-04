"""Discovery against Workable, which rate-limits by IP."""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import httpx
import pytest

from pipeline.sources import workable

NOW = datetime.now(tz=timezone.utc).isoformat()


@pytest.fixture(autouse=True)
def _fresh_run(monkeypatch):
    monkeypatch.setattr(workable, "_rate_limited", False)
    monkeypatch.setattr(workable.time, "sleep", lambda seconds: None)


def _status_error(code: int) -> httpx.HTTPStatusError:
    response = MagicMock()
    response.status_code = code
    response.headers = {}
    return httpx.HTTPStatusError("boom", request=MagicMock(), response=response)


def _listing(shortcode):
    return {"id": shortcode, "shortcode": shortcode, "title": "Leasing Agent", "published": NOW,
            "location": {"city": "Dallas", "region": "Texas", "countryCode": "US"}}


@patch("pipeline.sources.workable._get_detail")
@patch("pipeline.sources.workable._get_page")
def test_a_rate_limited_account_stops_the_remaining_accounts(mock_page, mock_detail):
    """The lockout covers the whole IP, so asking the next account would fail too
    and could extend it. Its jobs are picked up on the next run."""
    mock_page.side_effect = _status_error(429)

    assert workable.fetch_workable_jobs("first", "First") == []
    assert workable.fetch_workable_jobs("second", "Second") == []

    assert mock_page.call_count == 1


@patch("pipeline.sources.workable._get_detail")
@patch("pipeline.sources.workable._get_page")
def test_a_missing_account_does_not_stop_the_others(mock_page, mock_detail):
    mock_page.side_effect = [_status_error(404), {"results": [_listing("A1")]}]
    mock_detail.side_effect = lambda client, slug, shortcode: {**_listing(shortcode), "description": "<p>x</p>"}

    assert workable.fetch_workable_jobs("gone", "Gone") == []
    assert [j.source_id for j in workable.fetch_workable_jobs("live", "Live")] == ["workable_A1"]


@patch("pipeline.sources.workable._get_detail")
@patch("pipeline.sources.workable._get_page")
def test_a_rate_limit_during_detail_fetches_keeps_what_was_fetched(mock_page, mock_detail):
    mock_page.return_value = {"results": [_listing("A1"), _listing("A2"), _listing("A3")]}
    mock_detail.side_effect = [{**_listing("A1"), "description": "<p>x</p>"}, _status_error(429)]

    jobs = workable.fetch_workable_jobs("acct", "Acct")

    assert [j.source_id for j in jobs] == ["workable_A1"]
    assert mock_detail.call_count == 2
    assert workable._rate_limited is True


def test_list_and_detail_requests_retry_a_429():
    client = MagicMock(spec=httpx.Client)
    limited = MagicMock(status_code=429)
    limited.raise_for_status.side_effect = _status_error(429)
    ok = MagicMock(status_code=200)
    ok.json.return_value = {"results": []}
    client.post.side_effect = [limited, ok]

    page = workable._get_page.retry_with(wait=lambda *_a, **_k: 0)(client, "acct", None)

    assert page == {"results": []}
    assert client.post.call_count == 2
