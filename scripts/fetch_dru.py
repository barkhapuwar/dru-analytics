#!/usr/bin/env python3
"""
Fetch Daily Round-up push notification data from OneSignal.

One OneSignal notification == one send to one brand, so every record carries
external_id (who), template_id (signal + tone) and converted (did they click).

Pagination uses `time_offset` cursor paging, NOT `offset`:
  - `limit` is hard-capped at 50 regardless of what you ask for
  - `offset` is silently IGNORED when `time_offset` is present
  - `offset` alone degrades badly on old data (~8s at offset 45k)
  - `time_offset` costs the same per page no matter how old the day is
"""

import argparse
import datetime
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
UTC = datetime.timezone.utc
API = "https://api.onesignal.com"
PAGE = 50  # server-side hard cap

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW_DIR = os.path.join(ROOT, "data", "raw")
AGG_DIR = os.path.join(ROOT, "data", "agg")
TEMPLATE_MAP = os.path.join(ROOT, "data", "templates.json")

# Templates we count as Daily Round-up. Non-prod `dru_*` templates also exist and
# still emit a trickle of sends, so the prefix must be chosen deliberately.
DRU_PREFIX = "prod_dru_"


def log(msg):
    print(f"[{datetime.datetime.now(IST):%H:%M:%S}] {msg}", flush=True)


class OneSignal:
    def __init__(self, app_id, api_key):
        self.app_id = app_id
        self.api_key = api_key
        self.requests = 0
        self.rate_limited = 0

    def get(self, path, **params):
        params["app_id"] = self.app_id
        url = f"{API}{path}?{urllib.parse.urlencode(params)}"
        delay = 5
        for attempt in range(8):
            try:
                req = urllib.request.Request(
                    url, headers={"Authorization": f"Key {self.api_key}"}
                )
                with urllib.request.urlopen(req, timeout=90) as resp:
                    self.requests += 1
                    return json.load(resp)
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    self.rate_limited += 1
                    wait = int(e.headers.get("retry-after") or 30) + 3
                    log(f"  429 rate limited, waiting {wait}s")
                    time.sleep(wait)
                elif e.code in (500, 502, 503, 504):
                    log(f"  HTTP {e.code}, retrying in {delay}s")
                    time.sleep(delay)
                    delay = min(delay * 2, 60)
                else:
                    raise
            except Exception as e:  # network hiccup, timeout
                log(f"  {type(e).__name__}: {e}, retrying in {delay}s")
                time.sleep(delay)
                delay = min(delay * 2, 60)
        raise RuntimeError(f"gave up after 8 attempts: {url}")


def parse_template(name):
    """'prod_dru_growth_driver_v1' -> ('growth_driver', 'v1'). Names carry stray
    trailing spaces in OneSignal, hence the strip()."""
    s = (name or "").strip()
    if not s.startswith(DRU_PREFIX):
        return None, None
    s = s[len(DRU_PREFIX):]
    if len(s) > 3 and s[-3] == "_" and s[-2:] in ("v0", "v1", "v2"):
        return s[:-3], s[-2:]
    return s, None


def build_template_map(os_client):
    """template_id -> {name, signal, tone}. Keyed by id so a rename in the
    OneSignal dashboard surfaces as a mismatch instead of silently changing
    how a send is classified."""
    templates, offset = {}, 0
    while True:
        d = os_client.get("/templates", limit=50, offset=offset)
        batch = d.get("templates", [])
        if not batch:
            break
        for t in batch:
            signal, tone = parse_template(t.get("name"))
            templates[t["id"]] = {
                "name": (t.get("name") or "").strip(),
                "signal": signal,
                "tone": tone,
            }
        if len(batch) < 50:
            break
        offset += 50
        time.sleep(0.5)
    log(f"template map: {len(templates)} templates "
        f"({sum(1 for v in templates.values() if v['signal'])} are DRU)")
    with open(TEMPLATE_MAP, "w") as f:
        json.dump(templates, f, indent=2, sort_keys=True)
    return templates


def fetch_day(os_client, day):
    """All notifications queued during one IST calendar day, via cursor paging."""
    start = datetime.datetime.combine(day, datetime.time(0, 0), tzinfo=IST)
    end = start + datetime.timedelta(days=1)
    cursor = start.astimezone(UTC)
    end_ts = end.timestamp()

    seen, rows = set(), []
    pages = 0
    while True:
        d = os_client.get(
            "/notifications",
            limit=PAGE,
            time_offset=cursor.strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        batch = d.get("notifications", [])
        pages += 1
        if not batch:
            break

        fresh, past_end = 0, False
        for n in batch:
            q = n.get("queued_at") or 0
            if q >= end_ts:
                past_end = True
                continue
            if n["id"] in seen:
                continue
            seen.add(n["id"])
            rows.append(n)
            fresh += 1

        if past_end:
            break

        last = max(n.get("queued_at") or 0 for n in batch)
        # Advance to the last timestamp and rely on id-dedup. Only nudge +1s when
        # a whole page was duplicates, which means >50 sends share one second.
        cursor = datetime.datetime.fromtimestamp(
            last + (1 if fresh == 0 else 0), UTC
        )
        if fresh == 0 and len(batch) < PAGE:
            break
        time.sleep(0.8)

    return rows, pages


def slim(n, tmap):
    tid = n.get("template_id")
    meta = tmap.get(tid, {})
    name = meta.get("name") or (n.get("name") or "").strip()
    signal, tone = meta.get("signal"), meta.get("tone")
    if signal is None:
        signal, tone = parse_template(name)
    aliases = (n.get("include_aliases") or {}).get("external_id") or []
    q = n.get("queued_at") or 0
    return {
        "id": n["id"],
        "queued_at": q,
        "ist_date": datetime.datetime.fromtimestamp(q, IST).strftime("%Y-%m-%d"),
        "ist_time": datetime.datetime.fromtimestamp(q, IST).strftime("%H:%M:%S"),
        "template_id": tid,
        "template_name": name,
        "signal": signal,
        "tone": tone,
        "external_id": aliases[0] if aliases else None,
        # `successful` counts DEVICE subscriptions, not brands - a brand with two
        # phones scores 2. CTR here is brand-level: one row == one brand-send.
        "successful": n.get("successful", 0) or 0,
        "received": n.get("received", 0) or 0,
        "converted": n.get("converted", 0) or 0,
        "clicked": bool(n.get("converted", 0) or 0),
    }


def aggregate(rows, day, fetched_at, stable):
    sends = len(rows)
    clicks = sum(1 for r in rows if r["clicked"])
    brands = {r["external_id"] for r in rows if r["external_id"]}

    def bucket(key):
        out = defaultdict(lambda: {"sends": 0, "clicks": 0})
        for r in rows:
            k = r[key] or "(unknown)"
            out[k]["sends"] += 1
            out[k]["clicks"] += r["clicked"]
        for v in out.values():
            v["ctr"] = round(v["clicks"] / v["sends"] * 100, 2) if v["sends"] else 0.0
        return dict(sorted(out.items(), key=lambda kv: -kv[1]["sends"]))

    dupes = Counter(r["external_id"] for r in rows if r["external_id"])
    multi = {k: c for k, c in dupes.items() if c > 1}

    return {
        "date": day.isoformat(),
        "fetched_at": fetched_at,
        # Clicks keep accruing for hours after the send, so a freshly-fetched day
        # is always an undercount. Files settle once they stop changing.
        "stable": stable,
        "totals": {
            "sends": sends,
            "brands": len(brands),
            "clicks": clicks,
            "ctr": round(clicks / sends * 100, 2) if sends else 0.0,
            "sum_successful": sum(r["successful"] for r in rows),
            "sum_received": sum(r["received"] for r in rows),
        },
        "by_signal": bucket("signal"),
        "by_template": bucket("template_name"),
        "by_tone": bucket("tone"),
        "quality": {
            "unparsed_templates": sum(1 for r in rows if not r["signal"]),
            "missing_external_id": sum(1 for r in rows if not r["external_id"]),
            "brands_with_multiple_sends": len(multi),
            "max_sends_to_one_brand": max(dupes.values()) if dupes else 0,
            "received_gt_successful": sum(
                1 for r in rows if r["received"] > r["successful"]
            ),
            "converted_gt_received": sum(
                1 for r in rows if r["converted"] > r["received"]
            ),
        },
    }


def write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2)
    os.replace(tmp, path)  # atomic: a crash mid-write never leaves a partial file


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7,
                    help="number of complete IST days to fetch (default 7)")
    ap.add_argument("--end", default=None,
                    help="last day to fetch, YYYY-MM-DD (default: yesterday IST)")
    ap.add_argument("--stable-after", type=int, default=4,
                    help="days after which click counts are treated as settled")
    args = ap.parse_args()

    app_id = os.environ.get("ONESIGNAL_APP_ID")
    api_key = os.environ.get("ONESIGNAL_API_KEY")
    if not app_id or not api_key:
        sys.exit("ONESIGNAL_APP_ID / ONESIGNAL_API_KEY must be set in the environment")

    today = datetime.datetime.now(IST).date()
    end = (datetime.date.fromisoformat(args.end) if args.end
           else today - datetime.timedelta(days=1))
    days = [end - datetime.timedelta(days=i) for i in range(args.days - 1, -1, -1)]

    os.makedirs(RAW_DIR, exist_ok=True)
    os.makedirs(AGG_DIR, exist_ok=True)

    client = OneSignal(app_id, api_key)
    log(f"fetching {len(days)} days: {days[0]} .. {days[-1]}")
    tmap = build_template_map(client)

    t0 = time.time()
    summary = []
    for day in days:
        d0 = time.time()
        raw, pages = fetch_day(client, day)
        dru = [slim(n, tmap) for n in raw
               if (tmap.get(n.get("template_id"), {}).get("name")
                   or (n.get("name") or "").strip()).startswith(DRU_PREFIX)]

        fetched_at = datetime.datetime.now(IST).isoformat(timespec="seconds")
        stable = (today - day).days >= args.stable_after
        agg = aggregate(dru, day, fetched_at, stable)

        write_json(os.path.join(RAW_DIR, f"{day}.json"),
                   {"date": day.isoformat(), "fetched_at": fetched_at,
                    "stable": stable, "notifications": dru})
        write_json(os.path.join(AGG_DIR, f"{day}.json"), agg)

        t = agg["totals"]
        top = next(iter(agg["by_signal"]), "-")
        log(f"  {day}  all={len(raw):5d}  dru={len(dru):5d}  clicks={t['clicks']:4d}  "
            f"ctr={t['ctr']:5.2f}%  top={top}  "
            f"[{pages} pages, {time.time()-d0:.0f}s]{'' if stable else '  (unstable)'}")
        summary.append(agg)

    write_json(os.path.join(ROOT, "data", "summary.json"), {
        "generated_at": datetime.datetime.now(IST).isoformat(timespec="seconds"),
        "days": [s["date"] for s in summary],
        "daily": summary,
    })

    log(f"done: {client.requests} requests, {client.rate_limited} rate-limit waits, "
        f"{time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
