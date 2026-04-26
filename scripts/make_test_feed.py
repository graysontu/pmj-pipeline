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

state = State()
all_jobs = state.get_active_jobs()

jobs = []
for target_cat in CATEGORIES:
    for job in all_jobs:
        if job.get("category") == target_cat:
            jobs.append(job)
            break

generate_feed_xml(jobs, path=TEST_FEED_PATH)
