import logging
from pathlib import Path

from tenacity import retry, stop_after_attempt, wait_exponential

from pipeline.config import GOOGLE_INDEXING_CREDENTIALS_JSON

logger = logging.getLogger(__name__)

# NOTE: This module exists for future use. Google notification is currently handled
# by JobBoardly directly, so notify_google() is not called in normal pipeline runs.
# Enable it by removing --skip-indexing from the workflow and setting the credentials env var.


def _get_service():
    if not GOOGLE_INDEXING_CREDENTIALS_JSON:
        logger.warning("GOOGLE_INDEXING_CREDENTIALS_JSON not set. Skipping Google Indexing API.")
        return None

    creds_path = Path(GOOGLE_INDEXING_CREDENTIALS_JSON)
    if not creds_path.exists():
        logger.warning("Credentials file not found: %s. Skipping.", creds_path)
        return None

    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build

        scopes = ["https://www.googleapis.com/auth/indexing"]
        credentials = service_account.Credentials.from_service_account_file(
            str(creds_path), scopes=scopes
        )
        return build("indexing", "v3", credentials=credentials)
    except Exception as exc:
        logger.warning("Failed to initialize Google Indexing API: %s. Skipping.", exc)
        return None


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
def _publish(service, url: str, action: str) -> None:
    service.urlNotifications().publish(body={"url": url, "type": action}).execute()


def notify_google(url: str, action: str = "URL_UPDATED") -> None:
    """Notify the Google Indexing API of a URL update. Silently skips if credentials are missing."""
    service = _get_service()
    if service is None:
        return
    try:
        _publish(service, url, action)
        logger.info("Notified Google: %s %s", action, url)
    except Exception as exc:
        logger.warning("Google Indexing API notification failed for %s: %s", url, exc)
