import html as html_module

from bs4 import BeautifulSoup


def html_to_text(raw_html: str) -> str:
    if not raw_html:
        return ""
    soup = BeautifulSoup(raw_html, "lxml")
    return soup.get_text(separator=" ", strip=True)


def unescape_html(raw: str) -> str:
    """Unescape HTML entities before parsing (needed for some ATS APIs like Greenhouse)."""
    return html_module.unescape(raw) if raw else ""


def infer_remote_type(title: str, location: str, extra: str = "") -> str:
    combined = f"{title} {location} {extra}".lower()
    if "hybrid" in combined:
        return "hybrid"
    if "remote" in combined:
        return "remote"
    return "onsite"


def normalize_location(city: str | None, region: str | None) -> str:
    parts = [p.strip() for p in [city, region] if p and p.strip()]
    return ", ".join(parts) if parts else "Unknown"


def normalize_job_type(raw: str) -> str:
    lower = raw.lower() if raw else ""
    if "part" in lower:
        return "parttime"
    if "contract" in lower or "temp" in lower or "intern" in lower:
        return "contract"
    return "fulltime"
