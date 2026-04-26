"""Generate output/feed_test.xml with the first 3 active jobs from state."""
from pathlib import Path

from pipeline.output_xml import generate_feed_xml
from pipeline.state import State

TEST_FEED_PATH = Path(__file__).parent.parent / "output" / "feed_test.xml"

CATEGORIES = [
    "Property Manager Jobs",
    "Real Estate Admin & Coordinator Jobs",
    "Leasing Consultant Jobs",
    "Maintenance Technician Jobs",
]

# Additional specific jobs to include (e.g. to cover hourly pay)
EXTRA_IDS = [
    "greenhouse_5194285008",  # Assistant Maintenance Manager $28-$30/hr
    "greenhouse_5135865008",  # Maintenance Technician $20-$32/hr
]

state = State()
all_jobs = state.get_active_jobs()
all_jobs_by_id = {job["source_id"]: job for job in all_jobs}

jobs = []
for target_cat in CATEGORIES:
    for job in all_jobs:
        if job.get("category") == target_cat:
            jobs.append(job)
            break

for sid in EXTRA_IDS:
    if sid in all_jobs_by_id:
        jobs.append(all_jobs_by_id[sid])

generate_feed_xml(jobs, path=TEST_FEED_PATH)
