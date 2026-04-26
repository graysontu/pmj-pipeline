"""Backfill rewritten_description in state from rewrite cache for any job
where state still has the original source description."""
import json
from pathlib import Path

STATE_PATH = Path(__file__).parent.parent / "data" / "state.json"
CACHE_PATH = Path(__file__).parent.parent / "data" / "rewrite_cache.json"

with open(STATE_PATH, encoding="utf-8") as f:
    state = json.load(f)

with open(CACHE_PATH, encoding="utf-8") as f:
    cache = json.load(f)

updated = 0
for sid, job in state.items():
    if sid in cache and cache[sid].get("html"):
        cached_html = cache[sid]["html"]
        if job.get("rewritten_description") != cached_html:
            job["rewritten_description"] = cached_html
            updated += 1

with open(STATE_PATH, "w", encoding="utf-8") as f:
    json.dump(state, f, indent=2)

print(f"Updated {updated} jobs in state from rewrite cache.")
