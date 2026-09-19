"""Board census: the complete set of requisition IDs each ATS currently lists.

This is deliberately separate from the discovery fetchers in pipeline/sources/.
A discovery fetch is *filtered* - by the JOB_MAX_AGE_DAYS window, by the
1-per-company cap, and (for Workable and SmartRecruiters) by only detail-fetching
jobs inside the age window. None of those lists is a complete board inventory, so
absence from a discovery batch is NOT evidence that a job closed. Only absence
from a complete, successful census is.

The contract every census function keeps: raise CensusError on anything
ambiguous - a 404, a 5xx, a timeout, a bot block, non-JSON, or a paginated walk
that ended up with fewer IDs than the board said it had. A partial ID set is
never returned as a success, because treating one as authoritative would delete
live jobs.
"""

import logging
import time
from dataclasses import dataclass

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

logger = logging.getLogger(__name__)

TIMEOUT = 30.0
USER_AGENT = "pmj-pipeline-census/1.0 (+https://propertymanagementjobs.us)"

# Census pagination limits sit well above observed board sizes. The largest board
# seen is ~170 postings; Workable pages at 10 per request, so a 101-job board
# needs 11 pages. The discovery fetchers cap at MAX_PAGES=10 and silently
# truncate, which is exactly why the census does its own paging instead of
# reusing them.
MAX_PAGES = 200
SR_PAGE_LIMIT = 100
WORKABLE_PAGE_DELAY = 0.3


class CensusError(Exception):
    """Raised when a board's open-requisition set could not be established with
    confidence. Callers must treat this as 'unknown', never as 'all closed'."""


@dataclass(frozen=True)
class BoardCensus:
    source_type: str
    slug: str
    company_name: str
    ok: bool
    open_ids: frozenset[str]
    declared_total: int | None = None
    error: str | None = None

    @property
    def key(self) -> tuple[str, str]:
        return (self.source_type, self.slug)


def _check_complete(found: int, declared: int | None, slug: str) -> None:
    """A paginated walk that collected fewer IDs than the board declared is
    incomplete, and an incomplete set would look like mass closure."""
    if declared is not None and found < declared:
        raise CensusError(
            f"incomplete listing for '{slug}': collected {found} of {declared} declared postings"
        )


# Statuses worth retrying. 429 is the one that matters: Workable rate-limits the
# census when its ~77 requests arrive in a burst, and on 2026-09-18 that put all 7
# Workable accounts out of reach for a whole run - 29 feed jobs (10.5%) went
# unchecked. 404 is deliberately absent; a dead slug should fail fast.
RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TransportError):
        return True
    return (
        isinstance(exc, httpx.HTTPStatusError)
        and exc.response.status_code in RETRYABLE_STATUSES
    )


@retry(
    retry=retry_if_exception(_is_retryable),
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=2, min=2, max=30),
    reraise=True,
)
def _request(client: httpx.Client, method: str, url: str, **kwargs) -> dict | list:
    response = client.request(method, url, **kwargs)
    response.raise_for_status()
    try:
        return response.json()
    except ValueError as exc:
        # A 200 carrying HTML is what a bot block or an interstitial looks like.
        raise CensusError(f"non-JSON response from {url}: {exc}") from exc


def census_greenhouse(client: httpx.Client, slug: str) -> tuple[frozenset[str], int | None]:
    # content=true is omitted on purpose: the census only needs IDs, and the light
    # response is roughly a tenth the size of the discovery payload.
    data = _request(client, "GET", f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
    if not isinstance(data, dict):
        raise CensusError(f"unexpected Greenhouse payload for '{slug}': {type(data).__name__}")
    jobs = data.get("jobs")
    if jobs is None:
        raise CensusError(f"Greenhouse payload for '{slug}' has no 'jobs' key")
    declared = (data.get("meta") or {}).get("total")
    ids = frozenset(str(j["id"]) for j in jobs if j.get("id") is not None)
    _check_complete(len(ids), declared, slug)
    return ids, declared


def census_lever(client: httpx.Client, slug: str) -> tuple[frozenset[str], int | None]:
    data = _request(client, "GET", f"https://api.lever.co/v0/postings/{slug}?mode=json")
    if not isinstance(data, list):
        raise CensusError(f"unexpected Lever payload for '{slug}': {type(data).__name__}")
    # Lever returns every posting in one unpaginated array and declares no total,
    # so a parsed list is complete by construction.
    return frozenset(str(j["id"]) for j in data if j.get("id") is not None), None


def census_ashby(client: httpx.Client, slug: str) -> tuple[frozenset[str], int | None]:
    data = _request(client, "GET", f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
    if not isinstance(data, dict):
        raise CensusError(f"unexpected Ashby payload for '{slug}': {type(data).__name__}")
    jobs = data.get("jobs")
    if jobs is None:
        raise CensusError(f"Ashby payload for '{slug}' has no 'jobs' key")
    return frozenset(str(j["id"]) for j in jobs if j.get("id") is not None), None


def census_recruitee(client: httpx.Client, slug: str) -> tuple[frozenset[str], int | None]:
    data = _request(client, "GET", f"https://{slug}.recruitee.com/api/offers/")
    if not isinstance(data, dict):
        raise CensusError(f"unexpected Recruitee payload for '{slug}': {type(data).__name__}")
    offers = data.get("offers")
    if offers is None:
        raise CensusError(f"Recruitee payload for '{slug}' has no 'offers' key")
    return frozenset(str(j["id"]) for j in offers if j.get("id") is not None), None


def census_workable(client: httpx.Client, slug: str) -> tuple[frozenset[str], int | None]:
    url = f"https://apply.workable.com/api/v3/accounts/{slug}/jobs"
    ids: set[str] = set()
    declared: int | None = None
    token: str | None = None

    for page_index in range(MAX_PAGES):
        body: dict = {"query": "", "location": [], "department": [], "worktype": [], "remote": []}
        if token:
            body["token"] = token
        # Workable pages 10 jobs at a time, so a full census is ~77 rapid requests
        # across 7 accounts. A short pause between pages keeps the burst under the
        # rate limit; retries above handle it when that is not enough.
        if page_index:
            time.sleep(WORKABLE_PAGE_DELAY)
        page = _request(client, "POST", url, json=body)
        if not isinstance(page, dict):
            raise CensusError(f"unexpected Workable payload for '{slug}': {type(page).__name__}")
        if declared is None:
            declared = page.get("total")
        for job in page.get("results", []):
            if job.get("id") is None:
                continue
            # The v3 list carries a state field; anything not published is not a
            # live requisition. A missing field means published.
            if job.get("state") not in (None, "published"):
                continue
            ids.add(str(job["id"]))
        token = page.get("nextPage")
        if not token:
            break
    else:
        raise CensusError(
            f"Workable listing for '{slug}' did not terminate within {MAX_PAGES} pages"
        )

    _check_complete(len(ids), declared, slug)
    return frozenset(ids), declared


def census_smartrecruiters(client: httpx.Client, slug: str) -> tuple[frozenset[str], int | None]:
    url = f"https://api.smartrecruiters.com/v1/companies/{slug}/postings"
    ids: set[str] = set()
    declared: int | None = None
    offset = 0

    for _ in range(MAX_PAGES):
        page = _request(client, "GET", url, params={"limit": SR_PAGE_LIMIT, "offset": offset})
        if not isinstance(page, dict):
            raise CensusError(
                f"unexpected SmartRecruiters payload for '{slug}': {type(page).__name__}"
            )
        if declared is None:
            declared = page.get("totalFound")
        content = page.get("content", [])
        ids.update(str(j["id"]) for j in content if j.get("id") is not None)
        offset += len(content)
        if not content or offset >= (declared or 0):
            break
    else:
        raise CensusError(
            f"SmartRecruiters listing for '{slug}' did not terminate within {MAX_PAGES} pages"
        )

    # A nonexistent SmartRecruiters company answers 200 with zero postings, which
    # is indistinguishable from a real board that emptied out. Refuse to treat an
    # empty result as authoritative rather than closing every job for that company.
    if not ids:
        raise CensusError(
            f"SmartRecruiters returned zero postings for '{slug}'; a bad slug looks "
            "identical to an empty board, so this is not evidence of closure"
        )

    _check_complete(len(ids), declared, slug)
    return frozenset(ids), declared


CENSUS_FNS = {
    "greenhouse": census_greenhouse,
    "lever": census_lever,
    "ashby": census_ashby,
    "workable": census_workable,
    "recruitee": census_recruitee,
    "smartrecruiters": census_smartrecruiters,
}


def run_census(sources: dict[str, list[dict]]) -> dict[tuple[str, str], BoardCensus]:
    """Census every board in sources.yaml. Never raises: a board that cannot be
    established is returned with ok=False so its jobs stay 'unknown'."""
    results: dict[tuple[str, str], BoardCensus] = {}

    with httpx.Client(timeout=TIMEOUT, headers={"User-Agent": USER_AGENT}) as client:
        for source_type, entries in sources.items():
            fn = CENSUS_FNS.get(source_type)
            for entry in entries or []:
                slug = entry["slug"]
                company_name = entry["company_name"]

                if fn is None:
                    results[(source_type, slug)] = BoardCensus(
                        source_type, slug, company_name, False, frozenset(),
                        error=f"no census function for source type '{source_type}'",
                    )
                    continue

                try:
                    ids, declared = fn(client, slug)
                except CensusError as exc:
                    error = str(exc)
                except httpx.HTTPStatusError as exc:
                    error = f"HTTP {exc.response.status_code}"
                except Exception as exc:
                    error = f"{type(exc).__name__}: {exc}"
                else:
                    results[(source_type, slug)] = BoardCensus(
                        source_type, slug, company_name, True, ids, declared_total=declared
                    )
                    logger.info("Census %s/%s: %d open requisitions", source_type, slug, len(ids))
                    continue

                results[(source_type, slug)] = BoardCensus(
                    source_type, slug, company_name, False, frozenset(), error=error
                )
                logger.warning("Census unavailable for %s/%s: %s", source_type, slug, error)

    ok = sum(1 for c in results.values() if c.ok)
    logger.info(
        "Census complete: %d of %d boards established, %d open requisitions total",
        ok, len(results), sum(len(c.open_ids) for c in results.values() if c.ok),
    )
    return results
