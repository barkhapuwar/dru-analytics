#!/usr/bin/env python3
"""
Fetch brand identity (email -> group_name) directly from Amplitude.

fetch_brands.py's last resolution step used to read a sibling project's
checked-out data files (../reelo-habitual-users-dru-main/data/*.json) for
this. That only worked locally - a fresh checkout of just this repo (e.g. in
GitHub Actions) never has that sibling folder, so this repo needs to own its
data. This script hits the same Amplitude Export API that sibling dashboard's
fetch.py uses, and keeps only the identity mapping brand resolution needs:
which emails act for which brand (its group_name user property - the brand's
display name as tracked in-app, growth/starter plan only, same scope the
sibling dashboard uses).

Incremental like fetch_dru.py: each run re-fetches a trailing window (to
catch late-arriving events) and merges into data/amplitude_emails.json, which
only grows across runs.

Run:
    AMPLITUDE_API_KEY=... AMPLITUDE_SECRET_KEY=... python3 scripts/fetch_amplitude.py [--days N]
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
OUT = os.path.join(ROOT, "data", "amplitude_emails.json")

# Matches the brand scope reelo-habitual-users-dru-main/fetch.py uses -
# "growth"/"starter" and regional variants like "growth-inr-monthly".
REQUIRED_PLAN_PREFIXES = ("growth", "starter")


def log(msg):
    print(f"[{datetime.datetime.now(IST):%H:%M:%S}] {msg}", flush=True)


def fetch_chunk(api_key, secret_key, date, hour_start, hour_end):
    """One hour-range chunk of raw Amplitude events, with retries."""
    url = f"{REGION}/api/2/export"
    params = {
        "start": date.strftime(f"%Y%m%dT{hour_start:02d}"),
        "end": date.strftime(f"%Y%m%dT{hour_end:02d}"),
    }
    full_url = f"{url}?{urllib.parse.urlencode(params)}"
    auth = base64.b64encode(f"{api_key}:{secret_key}".encode()).decode()
    delay = 5
    for attempt in range(4):
        try:
            req = urllib.request.Request(full_url, headers={"Authorization": f"Basic {auth}"})
            with urllib.request.urlopen(req, timeout=300) as resp:
                data = resp.read()
            events = []
            zf = zipfile.ZipFile(io.BytesIO(data))
            for name in zf.namelist():
                with zf.open(name) as f, gzip.open(f, "rt", encoding="utf-8") as gz:
                    for line in gz:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            events.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
            return events
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return []  # no data for this chunk - normal for quiet hours
            log(f"  HTTP {e.code}, retrying in {delay}s")
            time.sleep(delay)
            delay = min(delay * 2, 60)
        except Exception as e:
            log(f"  {type(e).__name__}: {e}, retrying in {delay}s")
            time.sleep(delay)
            delay = min(delay * 2, 60)
    return []


def export_day(api_key, secret_key, day):
    """All events for one IST calendar day. IST = UTC+5:30, so the day spans
    UTC prev-day T18 through UTC same-day T17 (T18 itself crosses into the
    next IST day and is deliberately left out, same as the sibling fetch)."""
    prev = day - datetime.timedelta(days=1)
    chunks = [(prev, 18, 23), (day, 0, 5), (day, 6, 11), (day, 12, 17)]
    events = []
    for chunk_date, h_start, h_end in chunks:
        events.extend(fetch_chunk(api_key, secret_key, chunk_date, h_start, h_end))
    return events


def collect_identities(events):
    """email -> set(group_name), growth/starter-plan events only."""
    by_mail = {}
    for ev in events:
        up = ev.get("user_properties", {}) or {}
        plan = (up.get("plan") or "").lower()
        if not plan.startswith(REQUIRED_PLAN_PREFIXES):
            continue
        group = (up.get("group_name") or "").strip().strip('"')
        if not group or group.lower() == "none":
            continue
        email = (up.get("email_id") or up.get("email") or up.get("Email") or "").strip().lower()
        if not email:
            continue
        by_mail.setdefault(email, set()).add(group)
    return by_mail


def write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7,
                    help="trailing IST days to (re-)fetch each run (default 7)")
    ap.add_argument("--end", default=None,
                    help="last day to fetch, YYYY-MM-DD (default: yesterday IST)")
    args = ap.parse_args()

    api_key = os.environ.get("AMPLITUDE_API_KEY")
    secret_key = os.environ.get("AMPLITUDE_SECRET_KEY")
    if not api_key or not secret_key:
        sys.exit("AMPLITUDE_API_KEY / AMPLITUDE_SECRET_KEY must be set in the environment")

    today = datetime.datetime.now(IST).date()
    end = datetime.date.fromisoformat(args.end) if args.end else today - datetime.timedelta(days=1)
    days = [end - datetime.timedelta(days=i) for i in range(args.days - 1, -1, -1)]

    existing = {}
    if os.path.exists(OUT):
        existing = {k: set(v) for k, v in json.load(open(OUT)).get("emails", {}).items()}
    log(f"{len(existing)} emails already cached; fetching {len(days)} days: {days[0]} .. {days[-1]}")

    for day in days:
        events = export_day(api_key, secret_key, day)
        day_map = collect_identities(events)
        for email, groups in day_map.items():
            existing.setdefault(email, set()).update(groups)
        log(f"  {day}  {len(events):6d} events  {len(day_map):5d} emails")

    write_json(OUT, {
        "generated_at": datetime.datetime.now(IST).isoformat(timespec="seconds"),
        "days_covered": [d.isoformat() for d in days],
        "emails": {k: sorted(v) for k, v in existing.items()},
    })
    unique = sum(1 for v in existing.values() if len(v) == 1)
    log(f"wrote {OUT}: {len(existing)} emails total, {unique} map to exactly one brand")


if __name__ == "__main__":
    main()
