#!/usr/bin/env python3
"""
Dated snapshots of the Growth -> DRU adoption funnel, one file per day in
data/funnel/YYYY-MM-DD.json. No backfill: tracking starts the day this first
runs. The last few days are re-written each run so late-arriving clicks /
events settle; each snapshot also carries `opened_ids` (the Amplitude ids
that opened the DRU screen that day) so the dashboard can de-duplicate the
"Total DRU users" stage across any selected date range.

  1. Growth businesses     ops Google Sheet (brand tab) — current roster
  2. Have the app          OneSignal — Growth contact with an iOS/Android push sub
  3. Notifications enabled  OneSignal — of those, notifications not disabled
     (1-3 are current counts — they can't be reconstructed for a past day, but
      barely move day to day)
  4. Delivered a DRU        data/raw/*.json — `successful` (OneSignal's
     Delivered metric); `received` / Confirmed Delivery kept as a secondary
     floor since OneSignal's docs say it undercounts
  5. Tapped the notification data/raw/*.json — the push was clicked
  6. Total DRU users        Amplitude — DailyRoundupStoryView, plan=growth,
     all platforms; everyone who viewed the DRU screen whether via push tap
     or in-app (matches Amplitude's own daily number)

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
    """Distinct businesses for one day:
      delivered — OneSignal marked the push delivered (`successful`), i.e. the
                  push service (APNs / FCM) accepted it. This is OneSignal's
                  "Delivered" metric.
      confirmed — `received`, OneSignal's Confirmed Delivery (a device-side
                  receipt). Their docs note it undercounts, so it's a floor.
      tapped    — the push was clicked (`converted` > 0).
    """
    delivered, confirmed, tapped = set(), set(), set()
    for n in notifs:
        e = n.get("external_id")
        if not e:
            continue
        if (n.get("successful") or 0) > 0:
            delivered.add(e)
        if (n.get("received") or 0) > 0:
            confirmed.add(e)
        if n.get("clicked"):
            tapped.add(e)
    return len(delivered), len(confirmed), len(tapped)


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

    # Every day that already has a snapshot is re-written each run, so schema
    # and stage labels stay consistent. New days enter only through the
    # resettle window (the last RESETTLE_DAYS) — a day with no snapshot and
    # outside that window is skipped, so there is no accidental backfill down
    # the whole data/raw history.
    resettle = set(all_days[-RESETTLE_DAYS:])
    existing = {d for d in all_days if os.path.exists(os.path.join(OUT_DIR, f"{d}.json"))}
    to_do = sorted(existing | resettle) or [latest]

    def _load_snap(day):
        try:
            return json.load(open(os.path.join(OUT_DIR, f"{day}.json")))
        except Exception:  # noqa: BLE001
            return None

    # opened_ids: an Amplitude export per day (~10s). Only fetch it for days in
    # the resettle window (still settling) or days whose snapshot has none yet;
    # otherwise reuse what's on disk so the daily run stays fast.
    def cached_opened_ids(day):
        s = _load_snap(day)
        return s.get("opened_ids") if s else None

    # Stages 1-3 (roster, have-app, notifications-on) are current-state readings
    # with no per-day history in any source. We freeze them the first time a
    # day's snapshot is written and never touch them again — so a day you look
    # at next week still shows what those counts were around that day, not
    # today's numbers.
    def frozen_state(day):
        s = _load_snap(day)
        if not s:
            return None
        st = s.get("state")
        if st and all(k in st for k in ("growth", "with_app", "notif_on")):
            return st
        v = {x["key"]: x["value"] for x in s.get("steps", [])
             if x["key"] in ("growth", "with_app", "notif_on")}
        if len(v) == 3:
            v["captured"] = (s.get("generated_at") or "")[:10] or day
            return v
        return None

    log(f"processing {len(to_do)} snapshot(s): {to_do[0]} .. {to_do[-1]}")

    # Only read the (expensive) current state if at least one day still needs it.
    today = datetime.datetime.now(IST).date().isoformat()
    need_state = [d for d in to_do if frozen_state(d) is None]
    live = None
    if need_state:
        log(f"reading current state for {len(need_state)} new day(s)")
        brand_count, plans, contact_to_brand = fetch_growth_roster()
        growth_contacts = set(contact_to_brand) | all_time_dru_recipients()
        rows = onesignal_rows()
        has_app, notif_on = growth_app_and_enabled(rows, contact_to_brand, growth_contacts)
        live = {"growth": brand_count, "with_app": has_app, "notif_on": notif_on,
                "captured": today, "plans": plans}
        log(f"   growth {brand_count} · have app {has_app} · notifications on {notif_on}")

    os.makedirs(OUT_DIR, exist_ok=True)
    settling = set(all_days[-4:])  # last ~4 days keep rising as late data lands
    for day in to_do:
        d = datetime.date.fromisoformat(day)
        delivered, confirmed, tapped = dru_one_day(dru_days[day])
        cached = cached_opened_ids(day)
        if day in resettle or cached is None:
            opened_ids = dru_open_ids(d)
        else:
            opened_ids = cached
        opened = len(opened_ids)

        st = frozen_state(day) or live
        prev = _load_snap(day) or {}
        plans = st.get("plans") or prev.get("plan_breakdown") or {}
        cap = st.get("captured", day)
        st_note = (f"as of {cap}" if cap == day else f"logged {cap}")

        log(f"  {day}  delivered {delivered} · confirmed {confirmed} · tapped {tapped} · DRU users {opened}")
        snapshot = {
            "date": day,
            "generated_at": gen,
            "settling": day in settling,
            "opened_ids": opened_ids,
            "state": {k: st[k] for k in ("growth", "with_app", "notif_on", "captured")},
            "steps": [
                {"key": "growth", "label": "Growth businesses", "unit": "businesses",
                 "value": st["growth"],
                 "source": f"Ops roster sheet, brand tab — every row is a growth_* plan ({st_note})."},
                {"key": "with_app", "label": "Have the app", "unit": "businesses",
                 "value": st["with_app"],
                 "source": f"OneSignal — distinct Growth businesses with an iOS/Android push "
                           f"subscription on record ({st_note}; frozen once written, not a live count)."},
                {"key": "notif_on", "label": "Notifications enabled", "unit": "businesses",
                 "value": st["notif_on"],
                 "source": f"OneSignal — of those, businesses whose push subscription is not "
                           f"disabled ({st_note}; frozen once written)."},
                {"key": "delivered", "label": "Delivered a DRU", "unit": "businesses",
                 "value": delivered,
                 "source": f"OneSignal — the DRU push was delivered (accepted by APNs / FCM) "
                           f"on {day}. This is OneSignal's Delivered metric.",
                 "secondary": {"label": f"confirmed on device {day}", "value": confirmed,
                               "unit": "businesses",
                               "source": "OneSignal Confirmed Delivery — a device-side receipt. "
                                         "OneSignal's docs note it undercounts (offline devices, "
                                         "OS restrictions), so treat it as a floor."}},
                {"key": "tapped", "label": "Tapped the notification", "unit": "businesses",
                 "value": tapped,
                 "source": f"OneSignal — the DRU push was clicked on {day}. Brand-level (converted > 0)."},
                {"key": "opened", "label": "Total DRU users", "unit": "users",
                 "value": opened,
                 "source": f"Amplitude — everyone who viewed the DRU screen on {day}, whether they "
                           f"got there by tapping the push or opening it in the app "
                           f"(DailyRoundupStoryView, plan = growth, all platforms). "
                           f"Equals Amplitude's own daily unique-user count."},
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
