"""Retry rules shared by the census and the discovery fetchers.

Workable rate-limits by IP, through Cloudflare, and a burst can lock an IP out
for hours: on 2026-10-04 research probing from one IP earned a 429 carrying
Retry-After: 57415 (16 hours). Retrying that inside a run cannot succeed and only
adds requests, so a 429 that names a long wait is not retried, and the caller
stops sending that ATS requests for the rest of the run. A short Retry-After is
honoured exactly; without one, the exponential backoff applies as before.
"""

import httpx
from tenacity import wait_exponential

# 429 is the one that matters. 404 is deliberately absent; a dead slug should fail fast.
RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})

# Longest Retry-After worth waiting for inside a run.
MAX_RETRY_AFTER = 60.0

_backoff = wait_exponential(multiplier=2, min=2, max=30)


def retry_after_seconds(exc: BaseException) -> float | None:
    """The Retry-After a response asked for, in seconds, if it gave a number."""
    if not isinstance(exc, httpx.HTTPStatusError):
        return None
    value = exc.response.headers.get("retry-after") if exc.response is not None else None
    if not isinstance(value, str):
        return None
    try:
        return max(0.0, float(value.strip()))
    except ValueError:
        return None


def is_rate_limited(exc: BaseException) -> bool:
    return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429


def is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TransportError):
        return True
    if not (isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in RETRYABLE_STATUSES):
        return False
    wait = retry_after_seconds(exc)
    return wait is None or wait <= MAX_RETRY_AFTER


def wait_for_retry(retry_state) -> float:
    """Tenacity wait: the server's Retry-After when it gave one, else backoff."""
    exc = retry_state.outcome.exception() if retry_state.outcome else None
    wait = retry_after_seconds(exc) if exc else None
    return wait if wait is not None else _backoff(retry_state)
