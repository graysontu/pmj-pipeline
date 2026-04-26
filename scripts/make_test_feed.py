"""Generate output/feed_test.xml with the first 3 active jobs from state."""
from pathlib import Path

from pipeline.output_xml import generate_feed_xml
from pipeline.state import State

TEST_FEED_PATH = Path(__file__).parent.parent / "output" / "feed_test.xml"

state = State()
jobs = state.get_active_jobs()[:3]
generate_feed_xml(jobs, path=TEST_FEED_PATH)
