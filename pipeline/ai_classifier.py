import asyncio
import json
import logging
from collections import Counter
from pathlib import Path
from typing import Optional

import anthropic

from pipeline.config import ANTHROPIC_API_KEY
from pipeline.models import RawJob
from pydantic import BaseModel

logger = logging.getLogger(__name__)

MODEL = "claude-haiku-4-5"
MAX_TOKENS = 300
MAX_CONCURRENT = 1
REQUEST_INTERVAL = 3.0      # seconds to hold the semaphore after each response, throttling to ~20 RPM
RATE_LIMIT_RETRY_WAIT = 60  # seconds to wait after a 429 before retrying
CACHE_PATH = Path(__file__).parent.parent / "data" / "classification_cache.json"

VALID_CATEGORIES = {
    "Asset Manager Jobs",
    "Assistant Property Manager Jobs",
    "Commercial Property Manager Jobs",
    "Community Manager Jobs",
    "Groundskeeper & Porter Jobs",
    "HOA & Association Manager Jobs",
    "Leasing Consultant Jobs",
    "Maintenance Supervisor Jobs",
    "Maintenance Technician Jobs",
    "Property Manager Jobs",
    "Real Estate Admin & Coordinator Jobs",
    "Regional Property Manager Jobs",
    "REJECT",
}

SYSTEM_PROMPT = """\
You are a strict classifier for a property management job board. You evaluate whether a job belongs on the board, and if so, which of 12 categories it fits.

Output only valid JSON matching this schema:
{
  "is_pm_job": boolean,
  "category": "Asset Manager Jobs" | "Assistant Property Manager Jobs" | "Commercial Property Manager Jobs" | "Community Manager Jobs" | "Groundskeeper & Porter Jobs" | "HOA & Association Manager Jobs" | "Leasing Consultant Jobs" | "Maintenance Supervisor Jobs" | "Maintenance Technician Jobs" | "Property Manager Jobs" | "Real Estate Admin & Coordinator Jobs" | "Regional Property Manager Jobs" | "REJECT",
  "confidence": integer 0-100,
  "reject_reason": string or null
}

DEFINITION OF PROPERTY MANAGEMENT:
The day-to-day operation, leasing, maintenance, or oversight of residential, commercial, multifamily, HOA, condo, or association real estate on behalf of owners or investors.

INCLUDE these:
- On-site managers, regional managers, asset managers (real estate)
- Leasing consultants and leasing agents working at apartment communities
- Resident services and customer service roles at multifamily rental communities (including centralized call centers), categorize as Leasing Consultant Jobs
- Property maintenance roles (techs, supervisors, groundskeepers, porters)
- HOA and community association managers
- Commercial property managers (office, retail, industrial)
- Real estate administrators and coordinators supporting property operations
- Compliance roles specific to housing (LIHTC, HUD, Section 8 specialists)

REJECT these:
- Real estate sales agents, brokers, transaction coordinators
- Mortgage, lending, title, escrow, real estate finance
- Construction, development, project management for new builds
- Hotel, hospitality, short-term rental management (unless explicitly STR property mgmt company)
- Corporate facilities management not tied to a specific property
- Software engineers or product roles at proptech companies
- Marketing, sales (B2B), or executive roles not directly operating properties
- Real estate analysts at investment banks or REITs (asset management at a REIT operating properties is borderline OK)

CATEGORY ASSIGNMENT GUIDE (be precise):
- Property Manager Jobs: general PM, multifamily PM, on-site manager
- Assistant Property Manager Jobs: APM, assistant manager, second-in-command at a property
- Regional Property Manager Jobs: regional, area, district manager overseeing multiple properties
- Community Manager Jobs: explicitly titled community manager at apartment communities
- Commercial Property Manager Jobs: commercial, retail, office, industrial property management
- HOA & Association Manager Jobs: HOA, condo association, community association managers
- Asset Manager Jobs: real estate asset managers (portfolio level, not single property)
- Leasing Consultant Jobs: leasing agent, leasing consultant, leasing specialist
- Maintenance Technician Jobs: maintenance tech, make-ready tech, turn tech, service tech
- Maintenance Supervisor Jobs: lead maintenance, maintenance manager, service manager
- Groundskeeper & Porter Jobs: groundskeeper, porter, custodian, housekeeper, day porter
- Real Estate Admin & Coordinator Jobs: admin, coordinator, leasing admin, property admin

DECISION RULES:
- If job is clearly NOT property management, set is_pm_job=false, category="REJECT", explain in reject_reason
- If job IS PM but does not cleanly fit a category, set is_pm_job=true and pick the closest category, but lower confidence
- If multiple categories could fit, pick the most specific one (e.g., "Community Manager Jobs" beats "Property Manager Jobs" if title says Community Manager)
- Confidence reflects category certainty, not PM certainty. Only lower it for genuinely ambiguous cases.
- If confidence < 60 on category assignment, that is fine; downstream code may flag for review.\
"""


class ClassifiedJob(RawJob):
    is_pm_job: bool
    category: str
    confidence: int
    reject_reason: Optional[str] = None


def _load_cache() -> dict[str, dict]:
    if CACHE_PATH.exists():
        try:
            with open(CACHE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Could not load classification cache: %s. Starting fresh.", exc)
    return {}


def _save_cache(cache: dict[str, dict]) -> None:
    CACHE_PATH.parent.mkdir(exist_ok=True)
    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2)


def _safe_classified(raw_job: RawJob, reason: str) -> ClassifiedJob:
    return ClassifiedJob(
        **raw_job.model_dump(),
        is_pm_job=False,
        category="REJECT",
        confidence=0,
        reject_reason=reason,
    )


async def _call_api(
    client: anthropic.AsyncAnthropic,
    raw_job: RawJob,
    semaphore: asyncio.Semaphore,
) -> dict:
    user_prompt = (
        f"TITLE: {raw_job.title}\n"
        f"COMPANY: {raw_job.company}\n"
        f"LOCATION: {raw_job.location}\n"
        f"DESCRIPTION (first 2000 chars):\n{raw_job.description_text[:2000]}"
    )

    async with semaphore:
        response = await client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )
        await asyncio.sleep(REQUEST_INTERVAL)

    raw_text = response.content[0].text.strip()

    # Strip markdown code fences if the model wraps its JSON output
    if raw_text.startswith("```"):
        raw_text = raw_text.split("```")[1]
        if raw_text.startswith("json"):
            raw_text = raw_text[4:]
        raw_text = raw_text.strip()

    return json.loads(raw_text)


async def _classify_one(
    client: anthropic.AsyncAnthropic,
    raw_job: RawJob,
    cache: dict[str, dict],
    semaphore: asyncio.Semaphore,
    counter: list[int],
    total: int,
) -> ClassifiedJob:
    if raw_job.source_id in cache:
        result_fields = cache[raw_job.source_id]
    else:
        write_to_cache = True
        try:
            parsed = await _call_api(client, raw_job, semaphore)
            category = parsed.get("category", "REJECT")
            if category not in VALID_CATEGORIES:
                logger.warning(
                    "Invalid category '%s' for %s. Falling back to REJECT.", category, raw_job.source_id
                )
                category = "REJECT"
            result_fields = {
                "is_pm_job": bool(parsed.get("is_pm_job", False)),
                "category": category,
                "confidence": max(0, min(100, int(parsed.get("confidence", 0)))),
                "reject_reason": parsed.get("reject_reason") or None,
            }
        except (json.JSONDecodeError, KeyError, ValueError, IndexError) as exc:
            logger.warning("Malformed API response for %s: %s", raw_job.source_id, exc)
            result_fields = {
                "is_pm_job": False,
                "category": "REJECT",
                "confidence": 0,
                "reject_reason": "API parse error",
            }
        except anthropic.RateLimitError:
            logger.warning(
                "Rate limit hit for %s. Waiting %ds then retrying.", raw_job.source_id, RATE_LIMIT_RETRY_WAIT
            )
            await asyncio.sleep(RATE_LIMIT_RETRY_WAIT)
            try:
                parsed = await _call_api(client, raw_job, semaphore)
                category = parsed.get("category", "REJECT")
                if category not in VALID_CATEGORIES:
                    category = "REJECT"
                result_fields = {
                    "is_pm_job": bool(parsed.get("is_pm_job", False)),
                    "category": category,
                    "confidence": max(0, min(100, int(parsed.get("confidence", 0)))),
                    "reject_reason": parsed.get("reject_reason") or None,
                }
            except Exception as retry_exc:
                logger.error("Retry failed for %s: %s. Skipping (not cached).", raw_job.source_id, retry_exc)
                result_fields = {
                    "is_pm_job": False,
                    "category": "REJECT",
                    "confidence": 0,
                    "reject_reason": "API error",
                }
                write_to_cache = False
        except anthropic.APIError as exc:
            logger.error("Anthropic API error for %s: %s. Skipping (not cached).", raw_job.source_id, exc)
            result_fields = {
                "is_pm_job": False,
                "category": "REJECT",
                "confidence": 0,
                "reject_reason": "API error",
            }
            write_to_cache = False

        if write_to_cache:
            cache[raw_job.source_id] = result_fields

    counter[0] += 1
    if counter[0] % 25 == 0 or counter[0] == total:
        logger.info("Classifying %d/%d...", counter[0], total)

    return ClassifiedJob(**raw_job.model_dump(), **result_fields)


async def _batch_classify_async(jobs: list[RawJob]) -> list[ClassifiedJob]:
    if not ANTHROPIC_API_KEY:
        raise ValueError("ANTHROPIC_API_KEY is not set. Check your .env file.")

    cache = _load_cache()
    cached_count = sum(1 for j in jobs if j.source_id in cache)
    logger.info(
        "Classifying %d jobs. %d already cached, %d need API calls.",
        len(jobs),
        cached_count,
        len(jobs) - cached_count,
    )

    semaphore = asyncio.Semaphore(MAX_CONCURRENT)
    client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
    counter: list[int] = [0]
    total = len(jobs)

    tasks = [
        _classify_one(client, job, cache, semaphore, counter, total)
        for job in jobs
    ]
    results = await asyncio.gather(*tasks)

    _save_cache(cache)
    return list(results)


def classify_job(raw_job: RawJob) -> ClassifiedJob:
    """Classify a single job synchronously. Useful for one-off calls and testing."""
    return asyncio.run(_batch_classify_async([raw_job]))[0]


def batch_classify(jobs: list[RawJob]) -> list[ClassifiedJob]:
    """Classify a list of jobs in parallel with caching. Returns results in input order."""
    if not jobs:
        return []
    return asyncio.run(_batch_classify_async(jobs))


def summarize(classified: list[ClassifiedJob]) -> None:
    kept = [j for j in classified if j.is_pm_job]
    rejected = [j for j in classified if not j.is_pm_job]

    print(f"\nClassified {len(classified)} jobs. {len(kept)} kept, {len(rejected)} rejected.")
    if kept:
        print("By category:")
        for category, count in sorted(Counter(j.category for j in kept).items(), key=lambda x: -x[1]):
            print(f"  {category}: {count}")
