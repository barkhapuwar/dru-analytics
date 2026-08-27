#!/usr/bin/env python3
"""
Dated snapshots of the Growth -> DRU adoption funnel, one file per day in
data/funnel/YYYY-MM-DD.json, each covering that single day. No backfill: the
trend starts the day this first runs. The last few days are re-written each
run so late-arriving clicks / events settle.

  1. Growth businesses     ops Google Sheet (brand tab) — current roster
  2. Have the app          OneSignal — Growth contact with an iOS/Android push sub
  3. Notifications enabled  OneSignal — of those, notifications not disabled
     (1-3 are current counts — they can't be reconstructed for a past day, but
      barely move day to day)
  4. Received a DRU         data/raw/*.json — that day only
  5. Tapped a DRU           data/raw/*.json — that day only
  6. Opened the DRU screen  Amplitude — DailyRoundupStoryView, plan=growth,
     all platforms, that day only  (matches Amplitude's own daily number)

Run:
    set -a && source .env && set +a
    python3 scripts/fetch_funnel.py
"""

import base64
import csv
import datetime
import glob
import gzip
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW_DIR = os.path.join(ROOT, "data", "raw")
OUT_DIR = os.path.join(ROOT, "data", "funnel")

AMP_REGION = "https://amplitude.com"
OS_API = "https://onesignal.com/api/v1"
SHEET_ID = os.environ.get("BRAND_SHEET_ID", "11IVtAw8CemAi1C9zP4rdonnmBVWf11Rc1-HVbFACDUM")
BRAND_GID = os.environ.get("BRAND_BRAND_GID", "1306646119")

RESETTLE_DAYS = 5  # re-write this many trailing days each run (late clicks/events)
HEX24 = re.compile(r"^[0-9a-f]{24}$")
APP_DEVICE_TYPES = {"0", "1"}  # OneSignal device_type: 0 iOS, 1 Android
MOBILE_PLATFORMS = ["mobile-app-ios", "mobile-app-android"]


def log(msg):
    print(f"[{datetime.datetime.now(IST):%H:%M:%S}] {msg}", flush=True)


# ── 1. Growth roster (ops sheet) ────────────────────────────────────────────
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
    ids = set()
    for f in glob.glob(os.path.join(RAW_DIR, "*.json")):
        try:
            for n in json.load(open(f))["notifications"]:
                if n.get("external_id"):
                    ids.add(n["external_id"])
        except Exception:  # noqa: BLE001
            pass
    return ids


# ── 2 + 3. OneSignal (current subscription state) ───────────────────────────
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


def growth_app_and_enabled(rows, growth_ids):
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
    return len(with_app), len(enabled)


# ── 4 + 5. DRU delivery / clicks (one day) ──────────────────────────────────
def load_dru_days():
    """{date -> [notification, ...]} for every day on disk."""
    out = {}
    for f in sorted(glob.glob(os.path.join(RAW_DIR, "*.json"))):
        try:
            d = json.load(open(f))
            out[d["date"]] = d.get("notifications", [])
        except Exception:  # noqa: BLE001
            pass
    if not out:
        sys.exit("no data/raw/*.json — run scripts/fetch_dru.py first")
    return out


def dru_one_day(notifs):
    received, tapped = set(), set()
    for n in notifs:
        e = n.get("external_id")
        if not e:
            continue
        if (n.get("received") or 0) > 0:
            received.add(e)
        if n.get("clicked"):
            tapped.add(e)
    return len(received), len(tapped)


# ── 6. Amplitude (one-day unique users) ────────────────────────────────────
def amp_uniques(event, start, end, *, platforms=None):
    api_key = os.environ["AMPLITUDE_API_KEY"]
    secret = os.environ["AMPLITUDE_SECRET_KEY"]
    auth = base64.b64encode(f"{api_key}:{secret}".encode()).decode()
    e = {"event_type": event}
    if platforms:
        e["filters"] = [{"subprop_type": "event", "subprop_key": "platform",
                         "subprop_op": "is", "subprop_value": platforms}]
    params = {
        "e": json.dumps(e), "m": "uniques", "i": "1",
        "start": start.strftime("%Y%m%d"), "end": end.strftime("%Y%m%d"),
        "s": json.dumps([{"prop": "gp:plan", "op": "is", "values": ["growth"]}]),
    }
    url = f"{AMP_REGION}/api/2/events/segmentation?{urllib.parse.urlencode(params)}"
    delay = 5
    for _ in range(4):
        try:
            req = urllib.request.Request(url, headers={"Authorization": f"Basic {auth}"})
            with urllib.request.urlopen(req, timeout=180) as resp:
                d = json.load(resp)
            collapsed = d.get("data", {}).get("seriesCollapsed") or [[{"value": 0}]]
            return int(collapsed[0][0].get("value", 0))
        except urllib.error.HTTPError as ex:
            if ex.code < 500:
                sys.exit(f"amplitude HTTP {ex.code}: {ex.read()[:300]}")
            time.sleep(delay)
            delay = min(delay * 2, 60)
    sys.exit("amplitude: gave up after retries")


def main():
    for k in ("AMPLITUDE_API_KEY", "AMPLITUDE_SECRET_KEY",
              "ONESIGNAL_APP_ID", "ONESIGNAL_API_KEY"):
        if not os.environ.get(k):
            sys.exit(f"{k} must be set in the environment")

    gen = datetime.datetime.now(IST).isoformat(timespec="seconds")
    dru_days = load_dru_days()
    recent = sorted(dru_days)[-RESETTLE_DAYS:]
    log(f"snapshots for {recent[0]} .. {recent[-1]}  (re-writing {len(recent)} trailing days)")

    log("current-state stages (1-3)")
    brand_count, plans, sheet_ids = fetch_growth_roster()
    growth_ids = sheet_ids | all_time_dru_recipients()
    rows = onesignal_rows()
    has_app, notif_on = growth_app_and_enabled(rows, growth_ids)
    log(f"   growth {brand_count} · have app {has_app} · notifications on {notif_on}")

    os.makedirs(OUT_DIR, exist_ok=True)
    for day in recent:
        d = datetime.date.fromisoformat(day)
        received, tapped = dru_one_day(dru_days[day])
        opened = amp_uniques("DailyRoundupStoryView", d, d, platforms=None)
        log(f"  {day}  received {received} · tapped {tapped} · opened DRU {opened}")
        snapshot = {
            "date": day,
            "generated_at": gen,
            "steps": [
                {"key": "growth", "label": "Growth businesses", "unit": "businesses",
                 "value": brand_count,
                 "source": "Ops roster sheet — current count (stages 1-3 can't be rebuilt for a past day)"},
                {"key": "with_app", "label": "Have the app", "unit": "businesses",
                 "value": has_app,
                 "source": "OneSignal — Growth contact with an iOS/Android push subscription on record (current)"},
                {"key": "notif_on", "label": "Notifications enabled", "unit": "businesses",
                 "value": notif_on,
                 "source": "OneSignal — of the app installs, notifications not disabled (current)"},
                {"key": "received", "label": "Received a DRU", "unit": "businesses",
                 "value": received,
                 "source": f"OneSignal Confirmed Delivery for a prod_dru_* push on {day}"},
                {"key": "tapped", "label": "Tapped a DRU", "unit": "businesses",
                 "value": tapped,
                 "source": f"OneSignal click on a prod_dru_* push on {day}"},
                {"key": "opened", "label": "Opened the DRU screen", "unit": "users",
                 "value": opened,
                 "source": f"Amplitude DailyRoundupStoryView, plan = growth, all platforms, {day}"},
            ],
            "plan_breakdown": plans,
        }
        out = os.path.join(OUT_DIR, f"{day}.json")
        tmp = out + ".tmp"
        with open(tmp, "w") as f:
            json.dump(snapshot, f, indent=2, sort_keys=True)
        os.replace(tmp, out)
    log(f"wrote {len(recent)} snapshot(s) to {OUT_DIR}")


if __name__ == "__main__":
    main()
