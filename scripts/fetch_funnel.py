#!/usr/bin/env python3
"""
Dated snapshots of the Growth -> DRU adoption funnel, one file per day in
data/funnel/YYYY-MM-DD.json. No backfill: tracking starts the day this first
runs. The last few days are re-written each run so late-arriving clicks /
events settle; each snapshot also carries `opened_ids` (the Amplitude ids
that opened the DRU screen that day) so the dashboard can de-duplicate the
"Opened the DRU screen" stage across any selected date range.

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
import zipfile

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
    brand_count, plans, contact_to_brand = 0, {}, {}
    for r in rows[hi + 1:]:
        if not any(c.strip() for c in r):
            continue
        bid = r[id_i].strip() if len(r) > id_i else ""
        if bid:
            brand_count += 1
            if plan_i is not None and len(r) > plan_i:
                p = r[plan_i].strip() or "(blank)"
                plans[p] = plans.get(p, 0) + 1
        for cell in r[ci:end_i]:
            for tok in cell.split(","):
                tok = tok.strip()
                if HEX24.match(tok) and bid:
                    contact_to_brand[tok] = bid
    return brand_count, plans, contact_to_brand


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


def growth_app_and_enabled(rows, contact_to_brand, growth_contacts):
    """Distinct Growth *businesses* (not contacts) with an app push
    subscription, and of those with notifications on. A contact whose brand
    isn't in the roster sheet (e.g. a very recent signup that has still
    received a DRU) is counted as its own business."""
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
        if ext not in growth_contacts or r[dt_i].strip() not in APP_DEVICE_TYPES:
            continue
        brand = contact_to_brand.get(ext, ext)
        with_app.add(brand)
        try:
            nt = int(r[nt_i] or 0)
        except ValueError:
            nt = 0
        invalid = inv_i is not None and len(r) > inv_i and r[inv_i].strip() == "t"
        if nt > 0 and not invalid:
            enabled.add(brand)
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
    sent, received, tapped = set(), set(), set()
    for n in notifs:
        e = n.get("external_id")
        if not e:
            continue
        sent.add(e)
        if (n.get("received") or 0) > 0:
            received.add(e)
        if n.get("clicked"):
            tapped.add(e)
    return len(sent), len(received), len(tapped)


# ── 6. Amplitude — DRU-open user ids per day (Export API) ───────────────────
# The dashboard needs the actual id set, not just a count, so it can
# de-duplicate "opened the DRU screen" across a selected date range the same
# way the notification tab de-duplicates brands. len(set) == Amplitude's own
# daily unique-user number for that day.
def _amp_auth():
    return base64.b64encode(
        f"{os.environ['AMPLITUDE_API_KEY']}:{os.environ['AMPLITUDE_SECRET_KEY']}".encode()
    ).decode()


def _amp_export_chunk(auth, start, end):
    url = f"{AMP_REGION}/api/2/export?" + urllib.parse.urlencode({"start": start, "end": end})
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
        except urllib.error.HTTPError as ex:
            if ex.code == 404:
                return []
            time.sleep(delay)
            delay = min(delay * 2, 60)
        except Exception:  # noqa: BLE001
            time.sleep(delay)
            delay = min(delay * 2, 60)
    return []


def dru_open_ids(day):
    """Set of Amplitude ids that fired DailyRoundupStoryView on this IST day,
    plan = growth, all platforms. IST = UTC+5:30."""
    auth = _amp_auth()
    prev = day - datetime.timedelta(days=1)
    chunks = [
        (prev.strftime("%Y%m%dT18"), prev.strftime("%Y%m%dT23")),
        (day.strftime("%Y%m%dT00"), day.strftime("%Y%m%dT05")),
        (day.strftime("%Y%m%dT06"), day.strftime("%Y%m%dT11")),
        (day.strftime("%Y%m%dT12"), day.strftime("%Y%m%dT17")),
    ]
    ids = set()
    for s, e in chunks:
        for ev in _amp_export_chunk(auth, s, e):
            if ev.get("event_type") != "DailyRoundupStoryView":
                continue
            plan = str((ev.get("user_properties") or {}).get("plan") or "").lower()
            if not plan.startswith("growth"):
                continue
            uid = ev.get("amplitude_id") or ev.get("user_id")
            if uid is not None:
                ids.add(uid)
    return sorted(ids)


def main():
    for k in ("AMPLITUDE_API_KEY", "AMPLITUDE_SECRET_KEY",
              "ONESIGNAL_APP_ID", "ONESIGNAL_API_KEY"):
        if not os.environ.get(k):
            sys.exit(f"{k} must be set in the environment")

    gen = datetime.datetime.now(IST).isoformat(timespec="seconds")
    dru_days = load_dru_days()
    all_days = sorted(dru_days)
    latest = all_days[-1]

    # Process a day if it is in the resettle window (always — covers brand-new
    # days too) OR it already has a snapshot that is missing the per-user open
    # ids (self-healing for snapshots written before that field existed).
    # Never create a snapshot for a day older than tracking start: days with no
    # snapshot and outside the resettle window are skipped, so there is no
    # accidental backfill down the whole data/raw history.
    resettle = set(all_days[-RESETTLE_DAYS:])
    to_do = []
    for day in all_days:
        snap_path = os.path.join(OUT_DIR, f"{day}.json")
        has_snap = os.path.exists(snap_path)
        needs_ids = False
        if has_snap:
            try:
                needs_ids = "opened_ids" not in json.load(open(snap_path))
            except Exception:  # noqa: BLE001
                needs_ids = True
        if day in resettle or (has_snap and needs_ids):
            to_do.append(day)
    if not to_do:
        to_do = [latest]
    log(f"processing {len(to_do)} day(s): {to_do[0]} .. {to_do[-1]}")

    log("current-state stages (1-3)")
    brand_count, plans, contact_to_brand = fetch_growth_roster()
    growth_contacts = set(contact_to_brand) | all_time_dru_recipients()
    rows = onesignal_rows()
    has_app, notif_on = growth_app_and_enabled(rows, contact_to_brand, growth_contacts)
    log(f"   growth {brand_count} · have app {has_app} · notifications on {notif_on}")

    os.makedirs(OUT_DIR, exist_ok=True)
    settling = set(all_days[-4:])  # last ~4 days keep rising as late data lands
    for day in to_do:
        d = datetime.date.fromisoformat(day)
        sent, received, tapped = dru_one_day(dru_days[day])
        opened_ids = dru_open_ids(d)
        opened = len(opened_ids)
        log(f"  {day}  sent {sent} · received {received} · tapped {tapped} · opened DRU {opened}")
        snapshot = {
            "date": day,
            "generated_at": gen,
            "settling": day in settling,
            "opened_ids": opened_ids,
            "steps": [
                {"key": "growth", "label": "Growth businesses", "unit": "businesses",
                 "value": brand_count,
                 "source": "Ops roster sheet, brand tab — every row is a growth_* plan. Current count."},
                {"key": "with_app", "label": "Have the app", "unit": "businesses",
                 "value": has_app,
                 "source": "OneSignal — distinct Growth businesses with an iOS/Android push "
                           "subscription on record. Current state, no per-day history."},
                {"key": "notif_on", "label": "Notifications enabled", "unit": "businesses",
                 "value": notif_on,
                 "source": "OneSignal — of those, businesses whose push subscription is not "
                           "disabled. Current state."},
                {"key": "received", "label": "Received a DRU", "unit": "businesses",
                 "value": received,
                 "source": f"OneSignal Confirmed Delivery for a prod_dru_* push on {day} "
                           f"({sent} were sent; the rest are not yet confirmed landed).",
                 "secondary": {"label": f"sent a DRU on {day}", "value": sent,
                               "unit": "businesses",
                               "source": "OneSignal — a prod_dru_* push was queued and accepted "
                                         "by the push service (delivery not necessarily confirmed)."}},
                {"key": "tapped", "label": "Tapped a DRU", "unit": "businesses",
                 "value": tapped,
                 "source": f"OneSignal — a prod_dru_* push clicked on {day}. Brand-level (converted > 0)."},
                {"key": "opened", "label": "Opened the DRU screen", "unit": "users",
                 "value": opened,
                 "source": f"Amplitude DailyRoundupStoryView, plan = growth, all platforms, {day}. "
                           f"Matches Amplitude's own daily unique-user count."},
            ],
            "plan_breakdown": plans,
        }
        out = os.path.join(OUT_DIR, f"{day}.json")
        tmp = out + ".tmp"
        with open(tmp, "w") as f:
            json.dump(snapshot, f, indent=2, sort_keys=True)
        os.replace(tmp, out)
    log(f"wrote {len(to_do)} snapshot(s) to {OUT_DIR}")


if __name__ == "__main__":
    main()
