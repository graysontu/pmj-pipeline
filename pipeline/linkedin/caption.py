"""The text of each LinkedIn post: a short opening line, the facts, the link to
the job on the site, and a few hashtags.

Only the opening line is written by Claude. Every fact (location, pay, company,
link) comes straight from the job record, so the model cannot misstate them.
If the model call fails or returns something unusable, a plain template line is
used instead - a post never fails because of its caption.
"""

import logging
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import anthropic
from bs4 import BeautifulSoup

from pipeline.config import ANTHROPIC_API_KEY
from pipeline.linkedin.picker import Candidate

logger = logging.getLogger(__name__)

HOOK_MODEL = "claude-opus-5"
HOOK_MAX_CHARS = 220

# Tags every link so the site's analytics can count visitors LinkedIn sends.
UTM_PARAMS = {"utm_source": "linkedin", "utm_medium": "social", "utm_campaign": "job_post"}

ROLE_HASHTAGS = {
    "Property Manager Jobs": "PropertyManager",
    "Assistant Property Manager Jobs": "AssistantPropertyManager",
    "Community Manager Jobs": "CommunityManager",
    "Regional Property Manager Jobs": "RegionalManager",
    "Leasing Consultant Jobs": "LeasingJobs",
    "HOA & Association Manager Jobs": "HOAManagement",
    "Commercial Property Manager Jobs": "CommercialRealEstate",
    "Asset Manager Jobs": "AssetManagement",
    "Real Estate Admin & Coordinator Jobs": "RealEstateJobs",
}

SYSTEM_PROMPT = """\
You write the opening line of a LinkedIn post for PropertyManagementJobs.us, a job \
board for property management careers. Each post announces one job opening. Under \
your line the post already lists the job's location, pay and company, then a link \
to the full listing.

Write one or two sentences, at most 200 characters, that would make someone who \
works in property management want to open the listing. Draw on what is distinctive \
about this particular role in its description: the kind of property, the scope, \
the team, the growth path. Use only facts the description states. Leave out pay, \
location and benefits, since the post lists those below your line.

Reply with the line only: no hashtags, emojis, links or quotation marks."""


def tracked_url(url: str) -> str:
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query))
    query.update(UTM_PARAMS)
    return urlunsplit(parts._replace(query=urlencode(query)))


def _plain_text(html: str) -> str:
    return BeautifulSoup(html or "", "html.parser").get_text(" ", strip=True)


def _clean_hook(text: str) -> str | None:
    hook = re.sub(r"\s+", " ", text or "").strip().strip("\"'“”")
    if not 20 <= len(hook) <= HOOK_MAX_CHARS:
        return None
    if re.search(r"https?://|www\.|#|\$", hook):
        return None
    return hook


def write_hook(candidate: Candidate, client: anthropic.Anthropic | None = None) -> str | None:
    """Claude's opening line, or None if it couldn't produce a usable one."""
    if client is None:
        if not ANTHROPIC_API_KEY:
            logger.info("No ANTHROPIC_API_KEY; using the template opening line.")
            return None
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY, timeout=120.0, max_retries=2)

    prompt = (
        f"Job title: {candidate.title}\n"
        f"Company: {candidate.company}\n"
        f"Category: {candidate.category}\n\n"
        f"Job description:\n{_plain_text(candidate.description_html)}"
    )
    try:
        response = client.beta.messages.create(
            model=HOOK_MODEL,
            max_tokens=4000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": "low"},
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as exc:  # noqa: BLE001 - any failure, API or SDK, falls back to the template
        logger.warning("Opening line not generated (%s: %s); using the template.", type(exc).__name__, exc)
        return None

    if response.stop_reason != "end_turn":
        logger.warning("Opening line stopped with %s; using the template.", response.stop_reason)
        return None
    text = "".join(block.text for block in response.content if block.type == "text")
    hook = _clean_hook(text)
    if hook is None:
        logger.warning("Opening line unusable (%r); using the template.", text[:300])
    return hook


def template_hook(candidate: Candidate) -> str:
    city = candidate.location.rsplit(", ", 1)[0]
    return f"New opening: {candidate.title} at {candidate.company} in {city}."


def _city_hashtag(location: str) -> str:
    city = location.rsplit(", ", 1)[0]
    return "".join(part.capitalize() if part.islower() else part
                   for part in re.findall(r"[A-Za-z]+", city)) + "Jobs"


def build_post_text(candidate: Candidate, hook: str) -> str:
    hashtags = ["#PropertyManagement"]
    if candidate.category in ROLE_HASHTAGS:
        hashtags.append("#" + ROLE_HASHTAGS[candidate.category])
    hashtags += ["#NowHiring", "#" + _city_hashtag(candidate.location)]
    return (
        f"{hook}\n\n"
        f"\U0001F4CD {candidate.location}\n"
        f"\U0001F4B0 {candidate.pay}\n"
        f"\U0001F3E2 {candidate.company}\n\n"
        f"See the full job and apply \U0001F449 {tracked_url(candidate.site_url)}\n\n"
        + " ".join(hashtags)
    )
