#!/usr/bin/env python3
"""
Per-day Amplitude sets for the Growth -> DRU funnel, so the dashboard can
recompute the funnel for any date range client-side (same idea as
fetch_dru.py embedding every notification).

For each IST calendar day it records, among Growth-plan mobile-app events:
  - app : distinct Amplitude ids with ANY mobile-app event      ("has / uses the app")
  - dru : distinct Amplitude ids firing DailyRoundupStoryView    ("opened the DRU screen")

Amplitude ids are integers; they are remapped to opaque indices at
build_dashboard.py time so no raw id ships in dashboard.html.

Incremental like fetch_dru.py — each run re-fetches a trailing window and
overwrites those days (a day's set only settles after late events arrive).

Run:
    set -a && source .env && set +a
    python3 scripts/fetch_amplitude_funnel.py [--days N]
"""

import argparse
import base64
import datetime
import gzip
import io
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
REGION = "https://amplitude.com"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data", "amplitude_funnel.json")

PLAN_PREFIX = "growth"
DRU_EVENT = "DailyRoundupStoryView"


def log(msg):
    print(f"[{datetime.datetime.now(IST):%H:%M:%S}] {msg}", flush=True)


def fetch_chunk(auth, start, end):
    url = f"{REGION}/api/2/export?" + urllib.parse.urlencode({"start": start, "end": end})
    delay = 5
    for _ in range(4):
        try:
            req = urllib.request.Request(url, headers={"Authorization": f"Basic {auth}"})
            with urllib.request.urlopen(req, timeout=300) as resp:
                data = resp.read()
            out = []
            zf = zipfile.ZipFile(io.BytesIO(data))
            for name in zf.namelist():
                with zf.open(name) as f, gzip.open(f, "rt", encoding="utf-8") as gz:
                    for line in gz:
                        line = line.strip()
                        if line:
                            try:
                                out.append(json.loads(line))
                            except json.JSONDecodeError:
                                pass
            return out
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return []
            log(f"  HTTP {e.code}, retry in {delay}s")
            time.sleep(delay)
            delay = min(delay * 2, 60)
        except Exception as e:  # noqa: BLE001
            log(f"  {type(e).__name__}: {e}, retry in {delay}s")
            time.sleep(delay)
            delay = min(delay * 2, 60)
    return []


def day_sets(auth, day):
    """(app_ids, dru_ids) for one IST day. IST = UTC+5:30, so the day is
    UTC prev-dayT18 .. same-dayT17 — matches fetch_amplitude.py."""
    prev = day - datetime.timedelta(days=1)
    chunks = [
        (prev.strftime("%Y%m%dT18"), prev.strftime("%Y%m%dT23")),
        (day.strftime("%Y%m%dT00"), day.strftime("%Y%m%dT05")),
        (day.strftime("%Y%m%dT06"), day.strftime("%Y%m%dT11")),
        (day.strftime("%Y%m%dT12"), day.strftime("%Y%m%dT17")),
    ]
    app, dru = set(), set()
    for s, e in chunks:
        for ev in fetch_chunk(auth, s, e):
            plat = str((ev.get("event_properties") or {}).get("platform") or "")
            if not plat.startswith("mobile-app"):
                continue
            plan = str((ev.get("user_properties") or {}).get("plan") or "").lower()
            if not plan.startswith(PLAN_PREFIX):
                continue
            uid = ev.get("amplitude_id") or ev.get("user_id")
            if uid is None:
                continue
            app.add(uid)
            if ev.get("event_type") == DRU_EVENT:
                dru.add(uid)
    return sorted(app), sorted(dru)


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, separators=(",", ":"), sort_keys=True)
    os.replace(tmp, path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=45,
                    help="trailing IST days to (re-)fetch (default 45)")
    ap.add_argument("--end", default=None, help="last day YYYY-MM-DD (default: yesterday IST)")
    args = ap.parse_args()

    ak = os.environ.get("AMPLITUDE_API_KEY")
    sk = os.environ.get("AMPLITUDE_SECRET_KEY")
    if not ak or not sk:
        sys.exit("AMPLITUDE_API_KEY / AMPLITUDE_SECRET_KEY must be set in the environment")
    auth = base64.b64encode(f"{ak}:{sk}".encode()).decode()

    today = datetime.datetime.now(IST).date()
    end = datetime.date.fromisoformat(args.end) if args.end else today - datetime.timedelta(days=1)
    days = [end - datetime.timedelta(days=i) for i in range(args.days - 1, -1, -1)]

    existing = {}
    if os.path.exists(OUT):
        existing = json.load(open(OUT)).get("days", {})
    log(f"{len(existing)} days cached; fetching {days[0]} .. {days[-1]}")

    for day in days:
        app, dru = day_sets(auth, day)
        existing[day.isoformat()] = {"app": app, "dru": dru}
        log(f"  {day}  app {len(app):4d}  dru {len(dru):4d}")

    write_json(OUT, {
        "generated_at": datetime.datetime.now(IST).isoformat(timespec="seconds"),
        "days": dict(sorted(existing.items())),
    })
    log(f"wrote {OUT}  ({len(existing)} days)")


if __name__ == "__main__":
    main()
