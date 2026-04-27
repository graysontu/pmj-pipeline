"""Generate output/feed_test.xml with the first 3 active jobs from state."""
from pathlib import Path

from pipeline.output_xml import generate_feed_xml
from pipeline.state import State

TEST_FEED_PATH = Path(__file__).parent.parent / "output" / "feed_test.xml"

# One job per company, chosen to test company logo URLs
TEST_IDS = [
    "greenhouse_5194176008",  # Bozzuto
    "greenhouse_4203729009",  # Cortland
    "lever_15cd2830-dfa4-40a2-a57f-df23b50d0180",  # Lessen
]

state = State()
all_jobs_by_id = {job["source_id"]: job for job in state.get_active_jobs()}

jobs = [all_jobs_by_id[sid] for sid in TEST_IDS if sid in all_jobs_by_id]

generate_feed_xml(jobs, path=TEST_FEED_PATH)
