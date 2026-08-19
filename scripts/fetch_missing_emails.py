#!/usr/bin/env python3
"""
Fetch emails directly from OneSignal for external_ids the ops Google Sheet
couldn't resolve (see scripts/fetch_brands.py).

The sheet only carries contacts on Reelo's own ops export; a device
subscribed to push without a matching sheet row is invisible to it. OneSignal
itself, though, stores an Email-type subscription per user whenever one
exists, keyed by the same external_id used for push - GET
/apps/{app_id}/users/by/external_id/{id} returns it directly, no sheet
lookup needed.

Writes data/brand_emails.json as {external_id: email}, merging with any
existing entries. scripts/fetch_brands.py already reads this file (steps 3
and 4) to fill in emails and, via its email-tab match, sometimes names too -
so the normal flow after running this is to re-run fetch_brands.py.

Run:
    ONESIGNAL_APP_ID=... ONESIGNAL_API_KEY=... python3 scripts/fetch_missing_emails.py
"""

import argparse
import datetime
import glob
import json
import os
import sys
import time
import urllib.error
import urllib.request

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
API = "https://api.onesignal.com"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW_DIR = os.path.join(ROOT, "data", "raw")
BRAND_NAMES_FILE = os.path.join(ROOT, "data", "brand_names.json")
EMAIL_CACHE = os.path.join(ROOT, "data", "brand_emails.json")


def log(msg):
    print(f"[{datetime.datetime.now(IST):%H:%M:%S}] {msg}", flush=True)


def external_ids():
    ids = set()
    for f in sorted(glob.glob(os.path.join(RAW_DIR, "*.json"))):
        for n in json.load(open(f)).get("notifications", []):
            if n.get("external_id"):
                ids.add(n["external_id"])
    return ids


def unresolved_ids():
    ids = external_ids()
    resolved = json.load(open(BRAND_NAMES_FILE)) if os.path.exists(BRAND_NAMES_FILE) else {}
    return sorted(ids - set(resolved.keys()))


def get_user(app_id, api_key, external_id):
    """GET /apps/{app_id}/users/by/external_id/{id}. Retries on rate limit /
    transient errors; treats 404 (no such user) as a clean miss, not a fault."""
    url = f"{API}/apps/{app_id}/users/by/external_id/{external_id}"
    delay = 5
    for attempt in range(6):
        req = urllib.request.Request(url, headers={"Authorization": f"Key {api_key}"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if e.code == 429:
                wait = int(e.headers.get("retry-after") or 10) + 1
                log(f"  429 rate limited, waiting {wait}s")
                time.sleep(wait)
            elif e.code in (500, 502, 503, 504):
                time.sleep(delay)
                delay = min(delay * 2, 30)
            else:
                log(f"  {external_id}: HTTP {e.code} - {e.read()[:200]}")
                return None
        except Exception as e:
            log(f"  {external_id}: {type(e).__name__}: {e}, retrying in {delay}s")
            time.sleep(delay)
            delay = min(delay * 2, 30)
    log(f"  {external_id}: gave up after 6 attempts")
    return None


def extract_email(user):
    """First enabled Email-type subscription's token, if any."""
    if not user:
        return None
    for sub in user.get("subscriptions", []):
        if sub.get("type") == "Email" and sub.get("enabled") and sub.get("token"):
            return sub["token"].strip().lower()
    for sub in user.get("subscriptions", []):
        if sub.get("type") == "Email" and sub.get("token"):
            return sub["token"].strip().lower()
    return None


def write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rate", type=float, default=2.0, help="requests per second (default 2)")
    ap.add_argument("--limit", type=int, default=None, help="only process the first N unresolved ids (for testing)")
    args = ap.parse_args()

    app_id = os.environ.get("ONESIGNAL_APP_ID")
    api_key = os.environ.get("ONESIGNAL_API_KEY")
    if not app_id or not api_key:
        sys.exit("ONESIGNAL_APP_ID / ONESIGNAL_API_KEY must be set in the environment")

    ids = unresolved_ids()
    if args.limit:
        ids = ids[: args.limit]
    log(f"{len(ids)} unresolved external_ids to look up in OneSignal")

    cache = json.load(open(EMAIL_CACHE)) if os.path.exists(EMAIL_CACHE) else {}

    found, no_email, not_found = 0, 0, 0
    delay = 1.0 / args.rate
    for i, ext in enumerate(ids, 1):
        user = get_user(app_id, api_key, ext)
        if user is None:
            not_found += 1
        else:
            email = extract_email(user)
            if email:
                cache[ext] = email
                found += 1
            else:
                no_email += 1
        if i % 50 == 0 or i == len(ids):
            log(f"  {i}/{len(ids)}  found={found}  no_email={no_email}  not_found={not_found}")
        time.sleep(delay)

    write_json(EMAIL_CACHE, cache)
    log(f"wrote {EMAIL_CACHE} ({len(cache)} emails total)")
    log(f"  this run: found={found}  no_email={no_email}  not_found={not_found}  out of {len(ids)}")


if __name__ == "__main__":
    main()
