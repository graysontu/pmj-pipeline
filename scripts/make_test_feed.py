"""Generate output/feed_test.xml with the first 3 active jobs from state."""
from pathlib import Path

from pipeline.output_xml import generate_feed_xml
from pipeline.state import State

TEST_FEED_PATH = Path(__file__).parent.parent / "output" / "feed_test.xml"

state = State()
all_jobs = state.get_active_jobs()

# Pick one job from each of the first 3 distinct categories for better category mapping coverage
seen_categories: set[str] = set()
jobs = []
for job in all_jobs:
    cat = job.get("category", "")
    if cat not in seen_categories:
        seen_categories.add(cat)
        jobs.append(job)
    if len(jobs) == 3:
        break

generate_feed_xml(jobs, path=TEST_FEED_PATH)
