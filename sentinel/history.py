"""A small on-disk log of SSD health counters, so Sentinel can tell old errors from new ones.

The Device Health Report asks for the Samsung's integrity-error counter to be logged weekly: a stable count
means an old event, a rising count means the drive is deteriorating. Sentinel records one entry per drive per day.
"""

import datetime
import json
import os
from pathlib import Path

PATH = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "sentinel" / "history.json"
MAX_ENTRIES = 400

# Optional baselines that predate Sentinel's first run, keyed by drive serial number, e.g. from an earlier
# health report: {"<serial>": [{"date": "2026-09-26", "media_errors": 152, "percent_used": 12}]}.
# Without one, a drive's first reading becomes its baseline.
SEED = {}


def _load():
    try:
        with open(PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return json.loads(json.dumps(SEED))


def _save(data):
    PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = PATH.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, PATH)


def record(serial, media_errors, percent_used):
    """Log today's counters for a drive and return its first (baseline) entry."""
    data = _load()
    entries = data.setdefault(serial, [])
    today = datetime.date.today().isoformat()
    entry = {"date": today, "media_errors": media_errors, "percent_used": percent_used}
    if entries and entries[-1]["date"] == today:
        entries[-1] = entry
    else:
        entries.append(entry)
    data[serial] = entries[:1] + entries[1:][-(MAX_ENTRIES - 1):]
    try:
        _save(data)
    except OSError:
        pass
    return entries[0]
