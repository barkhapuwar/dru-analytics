#!/usr/bin/env python3
"""
Current-state numbers for the top of the Growth -> DRU funnel.

Two of the six funnel stages have no history anywhere we can reach:

  1. Growth businesses     ops Google Sheet (brand tab) — a live roster, no "as of" column
  3. Notifications enabled  OneSignal — only the current subscription state is exposed

So those two are written here as a single "as of today" snapshot and shown on
the dashboard as reference lines. The other four stages (has/uses app, received,
tapped, opened DRU) are recomputed for any date range client-side from
data/amplitude_funnel.json + data/raw/*.json.

Run:
    set -a && source .env && set +a
    python3 scripts/fetch_funnel.py
"""

import csv
import datetime
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW_DIR = os.path.join(ROOT, "data", "raw")
OUT = os.path.join(ROOT, "data", "funnel_state.json")

OS_API = "https://onesignal.com/api/v1"
SHEET_ID = os.environ.get("BRAND_SHEET_ID", "11IVtAw8CemAi1C9zP4rdonnmBVWf11Rc1-HVbFACDUM")
BRAND_GID = os.environ.get("BRAND_BRAND_GID", "1306646119")

HEX24 = re.compile(r"^[0-9a-f]{24}$")
APP_DEVICE_TYPES = {"0", "1"}  # OneSignal device_type: 0 iOS, 1 Android


def log(msg):
    print(f"[{datetime.datetime.now(IST):%H:%M:%S}] {msg}", flush=True)


def fetch_growth_roster():
    url = (f"https://docs.google.com/spreadsheets/d/{SHEET_ID}"
           f"/export?format=csv&gid={BRAND_GID}")
    text = urllib.request.urlopen(url, timeout=120).read().decode("utf-8", "replace")
    rows = list(csv.reader(io.StringIO(text)))
    hi = next((i for i, r in enumerate(rows[:5]) if "reelo_id" in r), None)
    if hi is None:
        sys.exit("brand tab: no header row with 'reelo_id' in the first 5 lines")
    hdr = rows[hi]
    id_i = hdr.index("reelo_id")
    plan_i = hdr.index("subscription_plan") if "subscription_plan" in hdr else None
    ci = hdr.index("contact_reelo_ids") if "contact_reelo_ids" in hdr else len(hdr)
    end_i = next((hdr.index(c) for c in ("NEW or Existing", "CSM", "TL") if c in hdr),
                 len(hdr))
    brand_count, plans, contact_ids = 0, {}, set()
    for r in rows[hi + 1:]:
        if not any(c.strip() for c in r):
            continue
        if len(r) > id_i and r[id_i].strip():
            brand_count += 1
            if plan_i is not None and len(r) > plan_i:
                p = r[plan_i].strip() or "(blank)"
                plans[p] = plans.get(p, 0) + 1
        for cell in r[ci:end_i]:
            for tok in cell.split(","):
                tok = tok.strip()
                if HEX24.match(tok):
                    contact_ids.add(tok)
    return brand_count, plans, contact_ids


def all_time_dru_recipients():
    import glob
    ids = set()
    for f in glob.glob(os.path.join(RAW_DIR, "*.json")):
        try:
            for n in json.load(open(f))["notifications"]:
                if n.get("external_id"):
                    ids.add(n["external_id"])
        except Exception:  # noqa: BLE001
            pass
    return ids


def onesignal_rows():
    app_id = os.environ["ONESIGNAL_APP_ID"]
    api_key = os.environ["ONESIGNAL_API_KEY"]
    headers = {"Authorization": f"Basic {api_key}", "Content-Type": "application/json"}
    req = urllib.request.Request(
        f"{OS_API}/players/csv_export?app_id={app_id}", method="POST", headers=headers,
        data=json.dumps({"extra_fields": ["external_id", "notification_types"]}).encode())
    with urllib.request.urlopen(req, timeout=60) as resp:
        csv_url = json.load(resp)["csv_file_url"]
    log(f"  export queued: {csv_url.rsplit('/', 1)[-1]}")
    import gzip
    for attempt in range(30):
        time.sleep(10)
        try:
            raw = urllib.request.urlopen(csv_url, timeout=120).read()
        except urllib.error.HTTPError as e:
            if e.code in (403, 404):
                continue
            raise
        text = gzip.decompress(raw).decode("utf-8", "replace")
        rows = list(csv.reader(io.StringIO(text)))
        log(f"  export ready after ~{(attempt + 1) * 10}s: {len(rows) - 1} rows")
        return rows
    sys.exit("onesignal: export never became ready")


def growth_enabled(rows, growth_ids):
    hdr = rows[0]
    col = {n: i for i, n in enumerate(hdr)}
    dt_i, nt_i, ext_i = col.get("device_type"), col.get("notification_types"), col.get("external_id")
    inv_i = col.get("invalid_identifier")
    if None in (dt_i, nt_i, ext_i):
        sys.exit(f"onesignal csv: unexpected header {hdr}")
    with_app, enabled = set(), set()
    for r in rows[1:]:
        if len(r) <= max(dt_i, nt_i, ext_i):
            continue
        ext = r[ext_i].strip()
        if ext not in growth_ids or r[dt_i].strip() not in APP_DEVICE_TYPES:
            continue
        with_app.add(ext)
        try:
            nt = int(r[nt_i] or 0)
        except ValueError:
            nt = 0
        invalid = inv_i is not None and len(r) > inv_i and r[inv_i].strip() == "t"
        if nt > 0 and not invalid:
            enabled.add(ext)
    return len(enabled), len(with_app)


def main():
    for k in ("ONESIGNAL_APP_ID", "ONESIGNAL_API_KEY"):
        if not os.environ.get(k):
            sys.exit(f"{k} must be set in the environment")

    log("ops sheet — Growth roster")
    brand_count, plans, sheet_ids = fetch_growth_roster()
    growth_ids = sheet_ids | all_time_dru_recipients()
    log(f"  {brand_count} Growth businesses")

    log("OneSignal — subscription export")
    rows = onesignal_rows()
    enabled, has_app = growth_enabled(rows, growth_ids)
    log(f"  notifications enabled {enabled} · app device on record {has_app}")

    out = {
        "as_of": datetime.datetime.now(IST).isoformat(timespec="seconds"),
        "growth_businesses": brand_count,
        "notifications_enabled": enabled,
        "onesignal_has_app_device": has_app,
        "plan_breakdown": plans,
    }
    tmp = OUT + ".tmp"
    with open(tmp, "w") as f:
        json.dump(out, f, indent=2, sort_keys=True)
    os.replace(tmp, OUT)
    log(f"wrote {OUT}")


if __name__ == "__main__":
    main()
