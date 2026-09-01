#!/usr/bin/env python3
"""
Build a self-contained dashboard.html from data/raw/*.json.

Unlike the previous build (which only embedded day-level rollups), this reads
every per-notification record so the dashboard can filter by period and drill
into a single brand's send pattern client-side, with no rebuild needed per view.

The data is embedded directly in the HTML, so the file works by double-clicking it
(no web server) and contains no API key. Chrome blocks file:// pages from fetching
sibling files, which is why the data is inlined rather than loaded.
"""

import datetime
import glob
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW_DIR = os.path.join(ROOT, "data", "raw")
FUNNEL_DIR = os.path.join(ROOT, "data", "funnel")
BRAND_NAMES_FILE = os.path.join(ROOT, "data", "brand_names.json")
OUT = os.path.join(ROOT, "dashboard.html")

# Top 8 signals by historical send volume, pinned to the 8 validated categorical
# hues in this fixed order. Pinned rather than re-ranked on every build: color
# must follow the entity, not its current rank, or a signal's line repaints
# every time send volume shuffles the leaderboard. Any signal not in this list
# (currently auto_campaign, campaign, orders_delta_up, orders_delta_down — each
# under 300 total sends vs. 700+ for the smallest signal here) folds into a
# single "Other" bucket rather than generating a 9th/10th/11th/12th hue.
SIGNAL_COLOR_ORDER = [
    "growth_driver",
    "historically_great",
    "significantly_down",
    "significantly_up",
    "aov",
    "loyalty",
    "customer_capture",
    "default",
]

SIGNAL_LABELS = {
    "growth_driver": "Growth driver",
    "historically_great": "Historically great",
    "significantly_down": "Significantly down",
    "significantly_up": "Significantly up",
    "aov": "AOV",
    "loyalty": "Loyalty",
    "customer_capture": "Customer capture",
    "default": "Default",
    "auto_campaign": "Auto campaign",
    "campaign": "Campaign",
    "orders_delta_up": "Orders up",
    "orders_delta_down": "Orders down",
}


def load_brand_names():
    """external_id -> (name, email), from data/brand_names.json (written by
    scripts/fetch_brands.py). Missing file just means every brand shows as
    unmapped ("—") rather than failing the build. Accepts both the richer
    {"id": {"name":..., "email":...}} shape and a plain {"id": "Name"} map."""
    if not os.path.exists(BRAND_NAMES_FILE):
        return {}, {}
    with open(BRAND_NAMES_FILE) as f:
        raw = json.load(f)
    names, emails = {}, {}
    for eid, v in raw.items():
        if isinstance(v, dict):
            if v.get("name"):
                names[eid] = v["name"]
            if v.get("email"):
                emails[eid] = v["email"]
        elif v:
            names[eid] = v
    return names, emails


def load_funnel():
    """All data/funnel/*.json snapshots, oldest first. Each is one day's
    Growth -> DRU funnel; stages 4-6 are that day, 1-3 are current state.
    No backfill — the history starts whenever scripts/fetch_funnel.py first
    ran. `opened_ids` (Amplitude ids that opened the DRU screen that day) are
    remapped to opaque 0..N indices here so the tab can de-duplicate the
    "Opened the DRU screen" stage over any selected range without shipping a
    real id. A missing dir just shows an empty-state on the tab."""
    snaps = []
    for f in sorted(glob.glob(os.path.join(FUNNEL_DIR, "*.json"))):
        try:
            s = json.load(open(f))
            if s.get("steps"):
                snaps.append(s)
        except (json.JSONDecodeError, OSError):
            continue
    snaps.sort(key=lambda s: s.get("date", ""))

    remap, nxt, opened_by_date = {}, 0, {}
    for s in snaps:
        row = []
        for uid in s.pop("opened_ids", []) or []:
            if uid not in remap:
                remap[uid] = nxt
                nxt += 1
            row.append(remap[uid])
        opened_by_date[s["date"]] = sorted(row)

    return {
        "snapshots": snaps,
        "latest": snaps[-1] if snaps else None,
        "openedByDate": opened_by_date,
    }


def load_all():
    brand_names, brand_emails = load_brand_names()
    files = sorted(glob.glob(os.path.join(RAW_DIR, "*.json")))
    if not files:
        raise SystemExit("no data in data/raw/ — run scripts/fetch_dru.py first")

    dates, unstable = [], []
    # brand_names.json now covers every growth-plan brand on file (see
    # fetch_brands.py), not just ones with send history - seeding brand_set
    # from it means a brand whose notifications have been off since day one
    # (never gets a send queued at all) still shows up and is searchable,
    # just with zero notifs.
    brand_set, signal_set = set(brand_names.keys()), set()
    rows = []  # (date, brand, signal, clicked, received)

    for f in files:
        d = json.load(open(f))
        date = d["date"]
        dates.append(date)
        if not d.get("stable", True):
            unstable.append(date)
        for n in d["notifications"]:
            b = n.get("external_id")
            s = n.get("signal")
            if not b or not s:
                continue
            brand_set.add(b)
            signal_set.add(s)
            # "received" is OneSignal's Confirmed Delivery (device-side receipt),
            # distinct from "successful" (push service accepted it) - every row
            # here is already a successful send, this just flags whether it's
            # also confirmed to have actually landed on the device.
            rows.append((date, b, s, 1 if n.get("clicked") else 0,
                        1 if (n.get("received") or 0) > 0 else 0))

    dates = sorted(set(dates))
    brands = sorted(brand_set)
    signals = sorted(signal_set)
    date_idx = {d: i for i, d in enumerate(dates)}
    brand_idx = {b: i for i, b in enumerate(brands)}
    signal_idx = {s: i for i, s in enumerate(signals)}

    notifs = [
        [date_idx[d], brand_idx[b], signal_idx[s], c, rv] for d, b, s, c, rv in rows
    ]

    return {
        "dates": dates,
        "unstable": sorted(unstable),
        "brands": brands,
        "signals": signals,
        "signalColorOrder": [s for s in SIGNAL_COLOR_ORDER if s in signal_idx],
        "signalLabels": SIGNAL_LABELS,
        "brandNames": brand_names,
        "brandEmails": brand_emails,
        "notifs": notifs,
        "funnel": load_funnel(),
        "generated": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
    }


CSS = """
*{margin:0;padding:0;box-sizing:border-box}
:root{
  color-scheme:light;
  --surface-1:#ffffff; --page:#f2efe7;
  --text-primary:#0b0b0b; --text-secondary:#52514e; --text-muted:#6f6d67;
  --grid:#e6e2d8; --axis:#c3c2b7; --border:rgba(11,11,11,0.10);
  --series-1:#2a78d6; --series-2:#eb6834; --series-3:#1baf7a; --series-4:#eda100;
  --series-5:#e87ba4; --series-6:#008300; --series-7:#4a3aa7; --series-8:#e34948;
  --other:#898781;
  --good:#0ca30c; --warning:#fab219; --critical:#d03b3b;
  /* Board palette: #3368A0 / #66A3BF / #C8DFDB / #F2EFE7.
     Contrast-checked, not eyeballed. #66A3BF carries white text at only
     2.78:1, so text-on-colour always uses the deep blue or darker. */
  --brand:#3368a0;
  --brand-grad-a:#214368; --brand-grad-b:#3368a0;   /* white text: 10.16 / 5.79 */
  --brand-solid-bg:#3368a0; --brand-solid-fg:#ffffff; /* 5.79 */
  --brand-ink:#2b5888;                               /* 6.40 cream / 5.27 sage */
  --brand-tint:#e4eef6; --brand-tint-border:#a9c8dd;
  /* Sage table header. --text-muted FAILS on it (2.57), so headers use
     --thead-fg (5.68) instead. */
  --thead-bg:#c8dfdb; --thead-fg:#52514e;
  --tip-bg:#eaf2f8; --tip-border:#a9c8dd;
  --chart-bar:#518bc8;   /* lighter step on the same hue; 3.57 on card.
                            #66A3BF itself fails the 3:1 graphical floor (2.78) */
}
@media (prefers-color-scheme:dark){
  :root:not([data-theme="light"]){
    color-scheme:dark;
    --surface-1:#1a1a19; --page:#0d0d0d;
    --text-primary:#fff; --text-secondary:#c3c2b7; --text-muted:#898781;
    --grid:#2c2c2a; --axis:#383835; --border:rgba(255,255,255,0.10);
    --series-1:#3987e5; --series-2:#d95926; --series-3:#199e70; --series-4:#c98500;
    --series-5:#d55181; --series-6:#008300; --series-7:#9085e9; --series-8:#e66767;
    --other:#898781;
    --brand-solid-bg:#66a3bf; --brand-solid-fg:#0b0b0b;  /* 7.08 */
    --brand-ink:#8fc0d6;                                  /* 8.86 on surface */
    --brand-tint:rgba(102,163,191,.16); --brand-tint-border:rgba(102,163,191,.40);
    --thead-bg:#1e2a33; --thead-fg:#c3c2b7;               /* 8.17 */
    --tip-bg:#16222b; --tip-border:rgba(102,163,191,.40);
    --chart-bar:#66a3bf;                                  /* 6.27 on surface */
  }
}
:root[data-theme="dark"]{
  color-scheme:dark;
  --surface-1:#1a1a19; --page:#0d0d0d;
  --text-primary:#fff; --text-secondary:#c3c2b7; --text-muted:#898781;
  --grid:#2c2c2a; --axis:#383835; --border:rgba(255,255,255,0.10);
  --series-1:#3987e5; --series-2:#d95926; --series-3:#199e70; --series-4:#c98500;
  --series-5:#d55181; --series-6:#008300; --series-7:#9085e9; --series-8:#e66767;
  --other:#898781;
  --brand-solid-bg:#66a3bf; --brand-solid-fg:#0b0b0b;
  --brand-ink:#8fc0d6;
  --brand-tint:rgba(102,163,191,.16); --brand-tint-border:rgba(102,163,191,.40);
  --thead-bg:#1e2a33; --thead-fg:#c3c2b7;
  --tip-bg:#16222b; --tip-border:rgba(102,163,191,.40);
  --chart-bar:#66a3bf;
}
body{background:var(--page);color:var(--text-primary);
  font-family:system-ui,-apple-system,"Segoe UI",sans-serif;font-size:14px;line-height:1.6;
  padding:32px 24px 64px}
.wrap{max-width:1180px;margin:0 auto}
h1{font-size:24px;font-weight:700;letter-spacing:-0.01em;color:#fff}
.sub{color:rgba(255,255,255,.82);font-size:13px;margin-top:4px}
.theme-btn{position:absolute;top:20px;right:22px;background:rgba(255,255,255,.16);
  border:1px solid rgba(255,255,255,.28);border-radius:99px;padding:6px 14px;font-size:12px;
  font-weight:600;color:#fff;cursor:pointer;font-family:inherit;z-index:60}
.theme-btn:hover{background:rgba(255,255,255,.26)}

/* ── hero band: the header is its own surface, not part of the page flow ── */
.hero{position:relative;overflow:hidden;border-radius:16px;margin-bottom:22px;
  padding:24px 26px 26px;
  background:linear-gradient(120deg,var(--brand-grad-a) 0%,var(--brand-grad-b) 100%)}
.hero::after{content:'';position:absolute;top:-90px;right:-60px;width:280px;height:280px;
  border-radius:50%;background:radial-gradient(circle,rgba(102,163,191,.45),transparent 68%);
  pointer-events:none}
.hero-head{margin-bottom:20px}
.hero-card{position:relative;z-index:1;display:grid;gap:14px 18px;
  grid-template-columns:repeat(auto-fit,minmax(150px,1fr))}
.kpi{background:rgba(255,255,255,.13);border:1px solid rgba(255,255,255,.2);
  border-radius:12px;padding:14px 16px}
.kpi .label{font-size:10.5px;color:rgba(255,255,255,.78);text-transform:uppercase;
  letter-spacing:.08em;line-height:1.4;margin-bottom:3px}
.kpi .value-row{display:flex;align-items:baseline;gap:8px;flex-wrap:wrap}
.kpi .value{font-size:26px;font-weight:700;line-height:1.2;color:#fff}
.kpi .note{font-size:11px;color:rgba(255,255,255,.66);margin-top:1px}
.kpi-delta{font-size:11.5px;font-weight:700;white-space:nowrap;cursor:help;
  padding:2px 7px;border-radius:99px;background:rgba(255,255,255,.16);color:#fff}
.kpi-delta:empty{display:none}

/* ── filter bar ─────────────────────────────────────────────────────────── */
.filterbar{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:20px}
.fbtn{background:var(--surface-1);border:1px solid var(--border);border-radius:99px;
  padding:7px 15px;font-size:12.5px;font-weight:600;color:var(--text-secondary);
  cursor:pointer;font-family:inherit;transition:all .12s}
.fbtn:hover{color:var(--brand-ink);border-color:var(--brand-tint-border);background:var(--brand-tint)}
.fbtn.active{background:var(--brand-solid-bg);color:var(--brand-solid-fg);
  border-color:var(--brand-solid-bg)}
.fbtn.active:hover{color:var(--brand-solid-fg);background:var(--brand-solid-bg)}
.customwrap{display:flex;align-items:center;gap:8px;font-size:12.5px;color:var(--text-secondary)}
.customwrap.hidden{display:none}
.customwrap input[type=date]{font-family:inherit;font-size:12.5px;color:var(--text-primary);
  background:var(--surface-1);border:1px solid var(--border);border-radius:7px;padding:5px 8px}
.search-wrap{position:relative;flex:1;min-width:170px;max-width:240px}
.search-ic{position:absolute;left:12px;top:50%;transform:translateY(-50%) rotate(90deg);
  font-size:13px;color:var(--text-muted);pointer-events:none}
.searchbox{width:100%;font-family:inherit;font-size:12.5px;
  background:var(--surface-1);border:1px solid var(--border);border-radius:99px;
  padding:7px 14px 7px 30px;color:var(--text-primary)}
.searchbox::placeholder{color:var(--text-muted)}

.banner{border-radius:10px;padding:10px 14px;margin-bottom:16px;font-size:12.5px;
  border:1px solid var(--border);background:var(--surface-1);display:flex;gap:9px;align-items:flex-start}
.banner .dot{width:7px;height:7px;border-radius:50%;margin-top:6px;flex-shrink:0;background:var(--warning)}

.card{background:var(--surface-1);border:1px solid var(--border);border-radius:16px;
  padding:20px 22px;margin-bottom:20px;overflow:hidden;
  box-shadow:0 1px 2px rgba(11,11,11,.04),0 1px 8px rgba(11,11,11,.03)}
.card h2{font-size:15px;font-weight:600;margin-bottom:2px}
.card .desc{font-size:12px;color:var(--text-muted);margin-bottom:14px}
.card-head{display:flex;justify-content:space-between;align-items:flex-start;gap:12px}
.toggle{background:none;border:1px solid var(--border);border-radius:6px;padding:3px 9px;
  font-size:11px;color:var(--text-secondary);cursor:pointer;font-family:inherit;white-space:nowrap}
.toggle:hover{color:var(--text-primary)}

/* ── signal legend / checkboxes ────────────────────────────────────────── */
.leg-controls{display:flex;align-items:center;gap:6px;margin-bottom:10px;flex-wrap:nowrap;
  padding-bottom:10px;border-bottom:1px solid var(--border)}
.leg-link{flex:1;background:var(--page);border:1px solid var(--border);border-radius:99px;
  padding:5px 12px;font-size:11.5px;font-weight:600;white-space:nowrap;
  color:var(--text-secondary);cursor:pointer;font-family:inherit;transition:all .12s}
.leg-link:hover:not(:disabled){color:var(--brand-ink);border-color:var(--brand-tint-border);
  background:var(--brand-tint)}
.leg-link:disabled{opacity:.35;cursor:default}
.leg-row{display:flex;flex-wrap:wrap;gap:6px 8px;margin-bottom:14px}
.leg-chip{display:inline-flex;align-items:center;gap:6px;font-size:12px;font-weight:600;
  color:var(--text-secondary);background:var(--page);border:1px solid var(--border);
  border-radius:99px;padding:5px 11px 5px 8px;cursor:pointer;user-select:none;transition:opacity .12s}
.leg-chip input{accent-color:var(--text-primary);width:13px;height:13px;cursor:pointer}
.leg-chip .sw{width:9px;height:9px;border-radius:2px;flex-shrink:0}
.leg-chip.off{opacity:.4}
.leg-note{font-size:11.5px;color:var(--text-muted);margin:-8px 0 14px}
.leg-row.vertical{flex-direction:column;flex-wrap:nowrap;gap:1px;margin-bottom:0}
.leg-row.vertical .leg-chip{width:100%;border:none;background:none;border-radius:7px;
  padding:6px 6px}
.leg-row.vertical .leg-chip:hover{background:var(--page)}

/* ── signal-filter dropdown (trend chart) ──────────────────────────────── */
.sig-filter{position:relative;flex-shrink:0}
.dropdown-btn{display:inline-flex;align-items:center;gap:7px;background:var(--surface-1);
  border:1px solid var(--border);border-radius:99px;padding:7px 14px;font-size:12.5px;
  font-weight:600;color:var(--text-secondary);cursor:pointer;font-family:inherit;white-space:nowrap}
.dropdown-btn:hover{color:var(--brand-ink);border-color:var(--brand-tint-border)}
.dropdown-btn .dd-chev{font-size:9px;color:var(--text-muted);transition:transform .12s}
.sig-filter.open .dropdown-btn{color:var(--brand-ink);border-color:var(--brand-tint-border);
  background:var(--brand-tint)}
.sig-filter.open .dropdown-btn .dd-chev{transform:rotate(180deg)}
.dropdown-panel{position:absolute;top:calc(100% + 8px);right:0;width:280px;
  background:var(--surface-1);border:1px solid var(--border);border-radius:10px;
  box-shadow:0 8px 28px rgba(11,11,11,.16);padding:12px 14px;z-index:50;
  max-height:320px;overflow-y:auto}
.dropdown-panel.hidden{display:none}

svg{display:block;width:100%;height:auto}
.gridline{stroke:var(--grid);stroke-width:1}
.axisline{stroke:var(--axis);stroke-width:1}
.tick{fill:var(--text-muted);font-size:11px;font-variant-numeric:tabular-nums}
.dlabel{fill:var(--text-secondary);font-size:11px;font-variant-numeric:tabular-nums}
.catlabel{fill:var(--text-secondary);font-size:12px}
.hit{fill:transparent;cursor:pointer}
.empty-note{font-size:12.5px;color:var(--text-muted);padding:24px 4px;text-align:center}

/* ── brand table ────────────────────────────────────────────────────────── */
table{width:100%;border-collapse:collapse;font-size:12.5px;font-variant-numeric:tabular-nums}
th{text-align:left;padding:14px 14px;font-size:11px;text-transform:uppercase;
  letter-spacing:.07em;color:var(--thead-fg);font-weight:700;
  border-bottom:1px solid var(--brand-tint-border);white-space:nowrap;
  cursor:pointer;user-select:none;position:sticky;top:0;background:var(--thead-bg);z-index:5}
th:first-child{border-top-left-radius:10px}
th:last-child{border-top-right-radius:10px}
th.sortable:hover{color:var(--brand-ink)}
th .arrow{color:var(--brand-ink);opacity:1}
th .arrow{margin-left:3px;opacity:.5}
td{padding:16px 14px;border-bottom:1px solid var(--grid);color:var(--text-secondary);
  vertical-align:middle}
td:first-child{color:var(--text-primary);font-weight:600}
th.num,td.num{text-align:right}
/* Auto table layout still respects max-width as a hint - this is the column
   that can most afford to give space back to the new Received column, since
   its badges already wrap onto extra lines instead of overflowing. */
th.sig-col,td.sig-col{max-width:230px}
.help{display:inline-flex;align-items:center;justify-content:center;width:14px;height:14px;
  border-radius:50%;border:1px solid var(--brand-ink);color:var(--brand-ink);font-size:9px;
  font-weight:700;margin-left:5px;cursor:help;vertical-align:middle;font-style:normal;
  text-transform:none;line-height:1}
.help:hover{background:var(--brand-solid-bg);color:var(--brand-solid-fg);
  border-color:var(--brand-solid-bg)}
.pill{display:inline-flex;align-items:center;justify-content:center;min-width:52px;
  padding:3px 9px;border-radius:99px;font-size:11.5px;font-weight:600;
  background:var(--page);border:1px solid var(--border);color:var(--text-secondary)}
.pill.hi{background:rgba(250,178,25,.16);border-color:rgba(250,178,25,.45);color:var(--text-primary)}
/* Top gap comes from .desc's own margin-bottom (they collapse to the larger
   of the two, so a margin-top here would be swallowed); the bottom margin is
   this row's alone and is what keeps the table off the capsules. */
.cat-filters{display:flex;flex-wrap:wrap;gap:10px;margin:0 0 16px}
.cat-filters.hidden{display:none}
.cap{font-family:inherit;cursor:pointer;display:inline-flex;align-items:center;
  padding:6px 14px;border-radius:99px;font-size:12px;font-weight:600;line-height:1.4;
  white-space:nowrap;
  background:var(--page);border:1px solid var(--border);color:var(--text-secondary);
  transition:all .12s}
.cap:hover{color:var(--brand-ink);border-color:var(--brand-tint-border);background:var(--brand-tint)}
.cap.active{background:var(--brand-solid-bg);color:var(--brand-solid-fg);border-color:var(--brand-solid-bg)}
.cap.active:hover{color:var(--brand-solid-fg);background:var(--brand-solid-bg)}
tbody tr.brand-row{cursor:pointer}
tbody tr.brand-row:hover{background:var(--page)}
/* Flex lives on an inner wrapper, never on the <td> itself — display:flex on a
   table cell drops it out of the table's column/border grid, which knocks its
   bottom border out of alignment with every other cell in the row. */
.cell-inline{display:flex;align-items:center;gap:7px}
.chev{display:inline-block;width:9px;height:9px;border-right:1.5px solid var(--text-muted);
  border-bottom:1.5px solid var(--text-muted);transform:rotate(-45deg);transition:transform .15s;flex-shrink:0}
tr.expanded .chev{transform:rotate(45deg)}
.eid{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11.5px;color:var(--text-secondary)}
.muted{color:var(--text-muted);font-weight:500}
.badges{display:flex;flex-wrap:wrap;gap:5px}
.badge{display:inline-flex;align-items:center;gap:5px;font-size:11px;font-weight:600;
  color:var(--text-secondary);background:var(--page);border:1px solid var(--border);
  border-radius:99px;padding:3px 8px}
.badge .sw{width:7px;height:7px;border-radius:2px;flex-shrink:0}
.badge.more{cursor:help;color:var(--text-muted)}
tr.detail-row{display:none}
tr.detail-row.show{display:table-row}
/* The open row and its detail panel are one unit: kill the divider between
   them and carry the same background across both, or the card reads as an
   unrelated block that happens to sit below the row. */
tbody tr.brand-row.expanded,tbody tr.brand-row.expanded:hover{background:var(--brand-tint)}
tbody tr.brand-row.expanded td{border-bottom-color:transparent}
tbody tr.brand-row.expanded td:first-child{box-shadow:inset 3px 0 0 var(--brand-solid-bg)}
tbody tr.brand-row.expanded .chev{border-color:var(--brand-ink)}
tr.detail-row.show td{background:var(--brand-tint);padding:0 16px 16px;
  border-bottom:1px solid var(--border);box-shadow:inset 3px 0 0 var(--brand-solid-bg)}
.detail-card{background:var(--surface-1);border:1px solid var(--border);border-radius:10px;
  padding:16px 18px;box-shadow:0 1px 2px rgba(11,11,11,.04),0 1px 6px rgba(11,11,11,.03)}
.detail-ident{margin-bottom:12px}
.ident-name{font-size:15px;font-weight:700;color:var(--text-primary);line-height:1.3}
.ident-name.muted{color:var(--text-muted);font-weight:600}
.ident-meta{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-top:3px}
.ident-email{font-size:12px;color:var(--brand-ink);font-weight:600;word-break:break-all}
.detail-head{display:flex;align-items:center;gap:0;flex-wrap:wrap;margin-bottom:14px;
  padding-bottom:12px;border-bottom:1px solid var(--border)}
.stat-chip{display:flex;flex-direction:column;gap:1px;padding:0 16px 0 0;margin-right:16px;
  border-right:1px solid var(--border)}
.stat-chip:last-of-type{border-right:none;margin-right:0}
.stat-chip .v{font-size:17px;font-weight:700;color:var(--text-primary);line-height:1.15}
.stat-chip .l{font-size:10px;color:var(--text-muted);text-transform:uppercase;letter-spacing:.06em}
.warn-chip{margin-left:auto;display:inline-flex;align-items:center;font-size:11.5px;font-weight:600;
  color:var(--text-secondary);background:rgba(250,178,25,.16);border:1px solid rgba(250,178,25,.4);
  border-radius:99px;padding:5px 11px;cursor:default;white-space:nowrap}
.detail-grid{display:grid;grid-template-columns:1fr 260px;gap:22px}
@media (max-width:760px){.detail-grid{grid-template-columns:1fr}}
.detail-h{font-size:11px;text-transform:uppercase;letter-spacing:.07em;color:var(--text-muted);
  font-weight:600;margin-bottom:10px}
.dotcal-wrap{overflow-x:auto}
.dotcal-note{font-size:11px;color:var(--text-muted);margin-top:8px}
.pie-legend{display:flex;flex-direction:column;gap:6px;margin-top:10px;font-size:12px}
.pie-legend .row{display:flex;align-items:center;gap:7px;color:var(--text-secondary)}
.pie-legend .sw{width:9px;height:9px;border-radius:2px;flex-shrink:0}
.pie-legend .n{margin-left:auto;font-variant-numeric:tabular-nums;color:var(--text-muted)}

.tbl-wrap{overflow-x:auto}
.pagin{display:flex;align-items:center;justify-content:space-between;gap:12px;
  margin-top:14px;font-size:12px;color:var(--text-muted);flex-wrap:wrap}
.pagin .btns{display:flex;gap:4px}
.pagin button{background:var(--surface-1);border:1px solid var(--border);border-radius:6px;
  padding:4px 10px;font-size:12px;color:var(--text-secondary);cursor:pointer;font-family:inherit}
.pagin button:disabled{opacity:.35;cursor:default}
.pagin button:hover:not(:disabled):not(.cur){color:var(--brand-ink);
  border-color:var(--brand-tint-border);background:var(--brand-tint)}
.pagin button.cur{background:var(--brand-solid-bg);color:var(--brand-solid-fg);
  border-color:var(--brand-solid-bg)}

/* Keyboard focus is brand-colored and always visible — the default ring is
   invisible against several of these custom backgrounds. */
.fbtn:focus-visible,.leg-link:focus-visible,.dropdown-btn:focus-visible,
.searchbox:focus-visible,.pagin button:focus-visible,.theme-btn:focus-visible,
input[type=date]:focus-visible{outline:2px solid var(--brand-solid-bg);outline-offset:2px}
.searchbox:focus{border-color:var(--brand-tint-border);outline:none}

#tip{position:fixed;pointer-events:none;background:var(--tip-bg);
  border:1px solid var(--tip-border);border-radius:11px;padding:11px 13px;font-size:12px;
  box-shadow:0 8px 26px rgba(11,11,11,.18);opacity:0;transition:opacity .1s;z-index:80;
  color:var(--text-primary);max-width:280px;line-height:1.55}
#tip .t{font-weight:700;margin-bottom:5px;padding-bottom:5px;
  border-bottom:1px solid var(--tip-border)}
#tip .r{color:var(--text-secondary);font-variant-numeric:tabular-nums}
footer{margin-top:32px;font-size:12px;color:var(--text-muted);line-height:1.7}
code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11.5px;
  background:var(--grid);padding:1px 5px;border-radius:4px}

/* ═══ view switcher ═══════════════════════════════════════════════════════ */
.topbar{display:flex;align-items:center;gap:14px;flex-wrap:wrap;margin-bottom:20px}
.topbar .mark{font-size:13px;font-weight:700;letter-spacing:-.01em;color:var(--text-primary)}
.topbar .mark span{color:var(--text-muted);font-weight:600}
.viewnav{display:inline-flex;background:var(--surface-1);border:1px solid var(--border);
  border-radius:99px;padding:3px;gap:2px}
.viewnav button{font-family:inherit;font-size:12.5px;font-weight:600;cursor:pointer;
  border:none;background:none;color:var(--text-secondary);padding:7px 16px;border-radius:99px;
  transition:all .13s;white-space:nowrap}
/* nav accent follows the active tab: notification blue by default, funnel green */
:root{--nav-solid-bg:var(--brand-solid-bg); --nav-solid-fg:var(--brand-solid-fg);
  --nav-ink:var(--brand-ink); --nav-tint:var(--brand-tint); --nav-tint-border:var(--brand-tint-border);}
:root[data-view="funnel"]{--nav-solid-bg:var(--f-solid-bg); --nav-solid-fg:var(--f-solid-fg);
  --nav-ink:var(--f-ink); --nav-tint:var(--f-tint); --nav-tint-border:var(--f-tint-border);}
.viewnav button:hover{color:var(--nav-ink)}
.viewnav button.active{background:var(--nav-solid-bg);color:var(--nav-solid-fg)}
.topbar .theme-btn{position:static;margin-left:auto;background:var(--surface-1);
  border:1px solid var(--border);color:var(--text-secondary)}
.topbar .theme-btn:hover{background:var(--nav-tint);color:var(--nav-ink);border-color:var(--nav-tint-border)}
.view.hidden{display:none}

/* ═══ Growth-adoption funnel — pastel palette, scoped to this view ════════ */
#view-funnel{
  --f-1:#4bb39a; --f-2:#e0a35c; --f-3:#9b8bd4;      /* trend series (light) */
  --f-bar:#3fa891;                                   /* funnel bars — one hue */
  --f-bar-track:#e6f2ee;
  --f-ink:#2f6f60;                                   /* pastel-family text accent */
  --f-solid-bg:#3fa891; --f-solid-fg:#ffffff;
  --f-tint:#e9f4f0; --f-tint-border:#bfe0d6;
  --f-hero-a:#3f8f7e; --f-hero-b:#5bb59f;
  --f-page-tint:#eef5f2;
  --drop-ok:#3f9d76; --drop-warn:#c98a2e; --drop-bad:#cf6f60;
}
@media (prefers-color-scheme:dark){
  :root:not([data-theme="light"]) #view-funnel{
    --f-1:#289578; --f-2:#bd8236; --f-3:#8370c4;
    --f-bar:#3aa78f; --f-bar-track:rgba(58,167,143,.14);
    --f-ink:#7fccb9;
    --f-solid-bg:#3aa78f; --f-solid-fg:#08120f;
    --f-tint:rgba(58,167,143,.14); --f-tint-border:rgba(58,167,143,.36);
    --f-hero-a:#1f4b41; --f-hero-b:#2f7263;
    --f-page-tint:#12201c;
    --drop-ok:#4fae86; --drop-warn:#d19a4c; --drop-bad:#dd8575;
  }
}
:root[data-theme="dark"] #view-funnel{
  --f-1:#289578; --f-2:#bd8236; --f-3:#8370c4;
  --f-bar:#3aa78f; --f-bar-track:rgba(58,167,143,.14);
  --f-ink:#7fccb9;
  --f-solid-bg:#3aa78f; --f-solid-fg:#08120f;
  --f-tint:rgba(58,167,143,.14); --f-tint-border:rgba(58,167,143,.36);
  --f-hero-a:#1f4b41; --f-hero-b:#2f7263;
  --f-page-tint:#12201c;
  --drop-ok:#4fae86; --drop-warn:#d19a4c; --drop-bad:#dd8575;
}
/* the base palette drives .viewnav / .theme-btn in the notif view too */
:root{--f-ink:#2f6f60; --f-solid-bg:#3fa891; --f-solid-fg:#fff;
  --f-tint:#e9f4f0; --f-tint-border:#bfe0d6;}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
  --f-ink:#7fccb9; --f-solid-bg:#3aa78f; --f-solid-fg:#08120f;
  --f-tint:rgba(58,167,143,.14); --f-tint-border:rgba(58,167,143,.36);}}
:root[data-theme="dark"]{--f-ink:#7fccb9; --f-solid-bg:#3aa78f; --f-solid-fg:#08120f;
  --f-tint:rgba(58,167,143,.14); --f-tint-border:rgba(58,167,143,.36);}

#view-funnel .fbtn.active{background:var(--f-solid-bg);color:var(--f-solid-fg);
  border-color:var(--f-solid-bg)}
#view-funnel .fbtn.active:hover{background:var(--f-solid-bg);color:var(--f-solid-fg)}
#view-funnel .fbtn:hover{color:var(--f-ink);border-color:var(--f-tint-border);background:var(--f-tint)}
.hero-funnel{background:linear-gradient(120deg,var(--f-hero-a) 0%,var(--f-hero-b) 100%)}
.hero-funnel::after{background:radial-gradient(circle,rgba(255,255,255,.22),transparent 68%)}
.hero-funnel .sub{color:rgba(255,255,255,.86)}
.hero-funnel .window-chip{display:inline-flex;align-items:center;gap:6px;margin-top:10px;
  font-size:12px;font-weight:600;color:#fff;background:rgba(255,255,255,.16);
  border:1px solid rgba(255,255,255,.22);border-radius:99px;padding:4px 12px}
#fnlFilterbar{margin-top:-6px}

/* funnel step list */
.fnl-steps{display:flex;flex-direction:column;gap:0}
.fnl-step{display:grid;grid-template-columns:210px 1fr 92px;gap:16px;align-items:center;
  padding:15px 4px}
.fnl-step + .fnl-step{border-top:1px solid var(--border)}
.fnl-label{display:flex;flex-direction:column;gap:3px}
.fnl-label .n{font-size:13.5px;font-weight:650;color:var(--text-primary);display:flex;
  align-items:center;gap:6px}
.fnl-label .u{font-size:10.5px;text-transform:uppercase;letter-spacing:.06em;
  color:var(--text-muted);font-weight:600;display:flex;align-items:center;gap:6px;flex-wrap:wrap}
.fnl-label .u.people{color:var(--f-3)}
.fnl-label,.fnl-bar-wrap{min-width:0}
.fnl-label .n{flex-wrap:wrap}
.asof-tag{font-size:9.5px;font-weight:700;text-transform:uppercase;letter-spacing:.04em;
  color:var(--f-3);background:color-mix(in srgb,var(--f-3) 15%,transparent);
  border-radius:99px;padding:1px 7px;cursor:help}
.fnl-bar-wrap{position:relative;height:34px;background:var(--f-bar-track);border-radius:8px;
  overflow:hidden}
.fnl-bar-wrap.empty{background:repeating-linear-gradient(135deg,var(--f-bar-track),
  var(--f-bar-track) 6px,transparent 6px,transparent 12px)}
.fnl-bar{position:absolute;left:0;top:0;height:100%;background:var(--f-bar);
  border-radius:8px;min-width:3px;transition:width .5s cubic-bezier(.3,.9,.3,1)}
.fnl-bar.fixed{background:repeating-linear-gradient(135deg,var(--f-bar),var(--f-bar) 9px,
  color-mix(in srgb,var(--f-bar) 78%,#fff) 9px,color-mix(in srgb,var(--f-bar) 78%,#fff) 18px)}
.fnl-bar-val{position:absolute;top:50%;transform:translateY(-50%);font-size:12.5px;
  font-weight:700;font-variant-numeric:tabular-nums;color:var(--text-primary)}
.fnl-pct{text-align:right;font-size:13px;font-weight:700;font-variant-numeric:tabular-nums;
  color:var(--text-secondary)}
.fnl-pct .sub{display:block;font-size:10.5px;font-weight:600;color:var(--text-muted)}
.fnl-conn{display:grid;grid-template-columns:210px 1fr 92px;gap:16px;padding:2px 4px}
.fnl-conn .mid{display:flex;align-items:center;gap:8px;font-size:11.5px;font-weight:600;
  color:var(--text-muted)}
.fnl-conn .drop-tag{display:inline-flex;align-items:center;gap:5px;padding:2px 9px;
  border-radius:99px;font-size:11px;font-weight:700;cursor:help}
.drop-tag.ok{background:color-mix(in srgb,var(--drop-ok) 16%,transparent);color:var(--drop-ok)}
.drop-tag.warn{background:color-mix(in srgb,var(--drop-warn) 18%,transparent);color:var(--drop-warn)}
.drop-tag.bad{background:color-mix(in srgb,var(--drop-bad) 18%,transparent);color:var(--drop-bad)}
.fnl-conn .rule{flex:1;height:1px;background:var(--border)}
.unit-flip{font-size:10.5px;font-weight:600;color:var(--f-3);cursor:help}

/* funnel KPI strip in the hero */
.fnl-kpis{grid-template-columns:repeat(auto-fit,minmax(140px,1fr))}

/* secondary metric chips under a step */
.fnl-2nd{display:flex;gap:8px;flex-wrap:wrap;margin:-6px 0 2px;padding-left:226px}
@media (max-width:720px){.fnl-2nd{padding-left:0}}
.fnl-2nd .chip{font-size:11px;font-weight:600;color:var(--text-secondary);
  background:var(--f-tint);border:1px solid var(--f-tint-border);border-radius:99px;
  padding:3px 10px;cursor:help}

@media (max-width:720px){
  .fnl-step{grid-template-columns:1fr auto;grid-template-areas:'label pct' 'bar bar';
    column-gap:10px;row-gap:8px}
  .fnl-label{grid-area:label}.fnl-bar-wrap{grid-area:bar}
  .fnl-pct{grid-area:pct;font-size:12px}.fnl-pct .sub{display:none}
  .fnl-conn{grid-template-columns:1fr}
  .fnl-2nd{grid-column:1}
  .fnl-bar-val{font-size:11.5px}
}
"""

JS_LIB = r"""
const $=(s,r)=>(r||document).querySelector(s);
const fmt=n=>n.toLocaleString('en-IN');
const tip=$('#tip');
function showTip(e,title,rows){
  tip.innerHTML='';
  const t=document.createElement('div');t.className='t';t.textContent=title;tip.appendChild(t);
  rows.forEach(r=>{const d=document.createElement('div');d.className='r';d.textContent=r;tip.appendChild(d);});
  tip.style.opacity=1;moveTip(e);
}
function moveTip(e){
  const p=12,w=tip.offsetWidth,h=tip.offsetHeight;
  let x=e.clientX+p,y=e.clientY+p;
  if(x+w>innerWidth-8)x=e.clientX-w-p;
  if(y+h>innerHeight-8)y=e.clientY-h-p;
  tip.style.left=x+'px';tip.style.top=y+'px';
}
function hideTip(){tip.style.opacity=0;}
const SVG='http://www.w3.org/2000/svg';
function el(n,a){const e=document.createElementNS(SVG,n);
  for(const k in a)e.setAttribute(k,a[k]);return e;}
function txt(s){return document.createTextNode(s);}

function barPath(x,y,w,h,r){
  r=Math.min(r,w/2,h);
  return `M${x},${y+h} L${x},${y+r} Q${x},${y} ${x+r},${y} L${x+w-r},${y}
          Q${x+w},${y} ${x+w},${y+r} L${x+w},${y+h} Z`;
}
function niceTicks(max,n){
  if(max<=0)return[0,1];
  const raw=max/n,mag=Math.pow(10,Math.floor(Math.log10(raw)));
  const step=[1,2,2.5,5,10].map(m=>m*mag).find(s=>s>=raw)||10*mag;
  const out=[];for(let v=0;v<=max-step*.001;v+=step)out.push(v);
  out.push(out[out.length-1]+step);
  return out;
}

/* ── multi-line chart, one series per signal, toggleable via .g-hidden ──── */
function multiLineChart(node,dates,series,opts){
  node.innerHTML='';
  if(!dates.length||!series.length){
    const d=document.createElement('div');d.className='empty-note';
    d.textContent='No data for the selected filters.';node.appendChild(d);return;
  }
  const W=Math.max(node.clientWidth||0,320),H=opts.h||260,M={t:14,r:12,b:30,l:46};
  const iw=W-M.l-M.r, ih=H-M.t-M.b;
  const max=Math.max(1,...series.flatMap(s=>s.values));
  const ticks=niceTicks(max,4), top=ticks[ticks.length-1]||max;
  const svg=el('svg',{viewBox:`0 0 ${W} ${H}`});
  ticks.forEach(t=>{
    const y=M.t+ih-(t/top)*ih;
    svg.appendChild(el('line',{x1:M.l,x2:M.l+iw,y1:y,y2:y,class:'gridline'}));
    const lb=el('text',{x:M.l-8,y:y+4,class:'tick','text-anchor':'end'});
    lb.textContent=fmt(Math.round(t));svg.appendChild(lb);
  });
  const n=dates.length;
  const xAt=i=>n===1?M.l+iw/2:M.l+(i/(n-1))*iw;
  const step=Math.max(1,Math.ceil(n/12));
  dates.forEach((d,i)=>{
    if(i%step!==0 && i!==n-1)return;
    const lb=el('text',{x:xAt(i),y:H-10,class:'tick','text-anchor':'middle'});
    lb.textContent=d.slice(5);svg.appendChild(lb);
  });
  series.forEach(s=>{
    const g=el('g',{class:'series-g',['data-key']:s.key});
    if(s.hidden)g.setAttribute('style','display:none');
    const pts=s.values.map((v,i)=>[xAt(i),M.t+ih-(v/top)*ih]);
    if(n===1){
      g.appendChild(el('circle',{cx:pts[0][0],cy:pts[0][1],r:5,fill:s.color}));
    }else{
      const d=pts.map((p,i)=>(i===0?'M':'L')+p[0]+','+p[1]).join(' ');
      g.appendChild(el('path',{d,fill:'none',stroke:s.color,'stroke-width':2,
        'stroke-linejoin':'round','stroke-linecap':'round'}));
    }
    svg.appendChild(g);
  });
  // crosshair + one tooltip listing every visible series at that x
  const hit=el('rect',{x:M.l,y:M.t,width:iw,height:ih,class:'hit'});
  const cross=el('line',{x1:0,x2:0,y1:M.t,y2:M.t+ih,stroke:'var(--axis)','stroke-width':1,style:'display:none'});
  svg.appendChild(cross);
  hit.addEventListener('mousemove',e=>{
    const rect=node.querySelector('svg').getBoundingClientRect();
    const px=(e.clientX-rect.left)*(W/rect.width);
    let i=n===1?0:Math.round(((px-M.l)/iw)*(n-1));
    i=Math.max(0,Math.min(n-1,i));
    const x=xAt(i);
    cross.setAttribute('x1',x);cross.setAttribute('x2',x);cross.style.display='block';
    const rows=series.filter(s=>!s.hidden).map(s=>s.label+': '+fmt(s.values[i]));
    if(rows.length)showTip(e,dates[i],rows);else hideTip();
  });
  hit.addEventListener('mouseleave',()=>{cross.style.display='none';hideTip();});
  svg.appendChild(hit);
  svg.appendChild(el('line',{x1:M.l,x2:M.l+iw,y1:M.t+ih,y2:M.t+ih,class:'axisline'}));
  node.appendChild(svg);
}

/* ── ranked horizontal bars, one hue (magnitude, not identity) — labels do
   the identity work, so this scales past 8 categories with no color cap ── */
function hbarChart(node,data,opts){
  node.innerHTML='';
  if(!data.length){
    const d=document.createElement('div');d.className='empty-note';
    d.textContent='No data for the selected period.';node.appendChild(d);return;
  }
  const W=Math.max(node.clientWidth||0,320), lw=opts.labelW||150,
        rowH=opts.rowH||28, M={t:6,r:56,b:6,l:lw};
  const H=M.t+data.length*rowH+M.b;
  const iw=W-M.l-M.r;
  const max=Math.max(...data.map(d=>d.v),1);
  const svg=el('svg',{viewBox:`0 0 ${W} ${H}`});
  const bh=Math.min(20,rowH-8);
  data.forEach((d,i)=>{
    const y=M.t+i*rowH+(rowH-bh)/2, w=(d.v/max)*iw;
    const cl=el('text',{x:M.l-10,y:y+bh/2+4,class:'catlabel','text-anchor':'end'});
    cl.textContent=d.label;svg.appendChild(cl);
    if(w>0)svg.appendChild(el('path',{d:hbarPath(M.l,y,w,bh,4),fill:'var(--chart-bar)'}));
    const vl=el('text',{x:M.l+w+8,y:y+bh/2+4,class:'dlabel'});
    vl.textContent=fmt(d.v);svg.appendChild(vl);
    const hit=el('rect',{x:0,y:M.t+i*rowH,width:W,height:rowH,class:'hit'});
    hit.addEventListener('mouseenter',e=>showTip(e,d.label,d.rows||['Sends: '+fmt(d.v)]));
    hit.addEventListener('mousemove',moveTip);
    hit.addEventListener('mouseleave',hideTip);
    svg.appendChild(hit);
  });
  svg.appendChild(el('line',{x1:M.l,x2:M.l,y1:M.t,y2:M.t+data.length*rowH,class:'axisline'}));
  node.appendChild(svg);
}
function hbarPath(x,y,w,h,r){
  r=Math.min(r,h/2,w);
  return `M${x},${y} L${x+w-r},${y} Q${x+w},${y} ${x+w},${y+r}
          L${x+w},${y+h-r} Q${x+w},${y+h} ${x+w-r},${y+h} L${x},${y+h} Z`;
}

/* ── per-brand activity grid: one ROW per signal, one COLUMN per date ──────
   Every signal the brand actually used gets its own labeled row, so identity
   is carried by position (never by squeezing >8 categories into one hue set),
   and nothing folds into "Other" here — this is a small, per-brand slice. ── */
function dotCalendar(node,dates,byDate,colorFor){
  node.innerHTML='';
  const sigCount=new Map(), cellMap=new Map(), labelOf=new Map();
  dates.forEach(d=>{
    (byDate.get(d)||[]).forEach(e=>{
      sigCount.set(e.signal,(sigCount.get(e.signal)||0)+1);
      labelOf.set(e.signal,e.label);
      const k=d+'|'+e.signal;
      const c=cellMap.get(k)||{count:0,clicks:0,received:0};
      c.count++; if(e.clicked)c.clicks++; if(e.received)c.received++;
      cellMap.set(k,c);
    });
  });
  const sigList=[...sigCount.entries()].sort((a,b)=>b[1]-a[1]).map(([s])=>s);
  if(!sigList.length){
    const d=document.createElement('div');d.className='empty-note';
    d.textContent='No sends in this period.';node.appendChild(d);return;
  }
  const labelW=136, cellW=48, rowH=26, M={t:6,l:labelW,b:22,r:10};
  const W=M.l+dates.length*cellW+M.r, H=M.t+sigList.length*rowH+M.b;
  // Fixed pixel width, not the usual width:100% — this chart's unit is a
  // cellW-wide day column, so it must render 1:1 and scroll (.dotcal-wrap
  // has overflow-x:auto), not stretch to fill the card and blow up every
  // dot and label past the container's actual width.
  const svg=el('svg',{viewBox:`0 0 ${W} ${H}`,style:`width:${W}px;max-width:none;height:${H}px`});
  const gridBottom=M.t+sigList.length*rowH;
  // Tint the whole column on a day with more than one signal sent — the
  // per-cell dots already show it, but a reader shouldn't have to spot two
  // dots in two different rows unaided to notice a duplicate-send day.
  dates.forEach((d,ci)=>{
    const dayCount=(byDate.get(d)||[]).length;
    if(dayCount>1){
      svg.appendChild(el('rect',{x:M.l+ci*cellW,y:M.t,width:cellW,height:gridBottom-M.t,
        fill:'var(--warning)',opacity:0.12}));
    }
  });
  sigList.forEach((sig,ri)=>{
    const y=M.t+ri*rowH+rowH/2;
    svg.appendChild(el('circle',{cx:14,cy:y,r:4,fill:colorFor(sig)}));
    const lb=el('text',{x:24,y:y+4,class:'catlabel','text-anchor':'start'});
    lb.textContent=labelOf.get(sig);svg.appendChild(lb);
    svg.appendChild(el('line',{x1:M.l,x2:W-M.r,y1:y,y2:y,class:'gridline'}));
  });
  dates.forEach((d,ci)=>{
    const x=M.l+ci*cellW+cellW/2;
    const dayCount=(byDate.get(d)||[]).length;
    sigList.forEach((sig,ri)=>{
      const y=M.t+ri*rowH+rowH/2;
      const cell=cellMap.get(d+'|'+sig);
      const hit=el('rect',{x:M.l+ci*cellW,y:M.t+ri*rowH,width:cellW,height:rowH,class:'hit'});
      if(cell){
        // Amber ring instead of the usual white one flags a dot where at
        // least one send here never confirmed delivery - the exact question
        // a multi-signal day raises: were both actually received, or just sent?
        const notAllReceived=cell.received<cell.count;
        svg.appendChild(el('circle',{cx:x,cy:y,r:7,fill:colorFor(sig),
          stroke:notAllReceived?'var(--warning)':'var(--surface-1)','stroke-width':2}));
        if(cell.clicks>0)svg.appendChild(el('circle',{cx:x,cy:y,r:2.4,fill:'var(--surface-1)'}));
        hit.addEventListener('mouseenter',e=>showTip(e,d,[
          'Signal: '+labelOf.get(sig),
          cell.count>1?('Sends: '+cell.count):'Sent',
          'Received: '+(cell.count>1?(cell.received+' of '+cell.count):(cell.received?'yes':'no')),
          'Clicked: '+(cell.clicks>0?('yes'+(cell.clicks>1?' ('+cell.clicks+')':'')):'no')]));
      }else{
        svg.appendChild(el('circle',{cx:x,cy:y,r:1.8,fill:'var(--axis)'}));
        hit.addEventListener('mouseenter',e=>showTip(e,d,['No '+labelOf.get(sig)+' sent']));
      }
      hit.addEventListener('mousemove',moveTip);
      hit.addEventListener('mouseleave',hideTip);
      svg.appendChild(hit);
    });
    if(ci%Math.max(1,Math.ceil(dates.length/14))===0 || ci===dates.length-1){
      const lb=el('text',{x,y:H-6,class:'tick','text-anchor':'middle'});
      if(dayCount>1)lb.setAttribute('fill','var(--warning)');
      lb.textContent=d.slice(5);svg.appendChild(lb);
    }
  });
  node.appendChild(svg);
}

/* ── small pie, capped at 6 wedges (top 5 + Other), 2px surface-color gap ── */
function pieChart(node,data,legendNode){
  node.innerHTML='';if(legendNode)legendNode.innerHTML='';
  const total=data.reduce((s,d)=>s+d.v,0);
  if(!total){const d=document.createElement('div');d.className='empty-note';
    d.textContent='No sends.';node.appendChild(d);return;}
  const size=150,r=62,cx=size/2,cy=size/2;
  const svg=el('svg',{viewBox:`0 0 ${size} ${size}`,style:'max-width:150px;margin:0 auto'});
  let a0=-Math.PI/2;
  data.forEach(d=>{
    const frac=d.v/total, a1=a0+frac*Math.PI*2;
    const large=(a1-a0)>Math.PI?1:0;
    const x0=cx+r*Math.cos(a0), y0=cy+r*Math.sin(a0);
    const x1=cx+r*Math.cos(a1), y1=cy+r*Math.sin(a1);
    const path=frac>=0.999
      ? `M${cx},${cy-r} A${r},${r} 0 1 1 ${cx-0.01},${cy-r} Z`
      : `M${cx},${cy} L${x0},${y0} A${r},${r} 0 ${large} 1 ${x1},${y1} Z`;
    const wedge=el('path',{d:path,fill:d.color,stroke:'var(--surface-1)','stroke-width':2});
    wedge.addEventListener('mouseenter',e=>showTip(e,d.label,
      [fmt(d.v)+' sends','('+(frac*100).toFixed(1)+'%)']));
    wedge.addEventListener('mousemove',moveTip);
    wedge.addEventListener('mouseleave',hideTip);
    svg.appendChild(wedge);
    a0=a1;
  });
  node.appendChild(svg);
  if(legendNode){
    data.forEach(d=>{
      const row=document.createElement('div');row.className='row';
      const sw=document.createElement('span');sw.className='sw';sw.style.background=d.color;
      const lbl=document.createElement('span');lbl.textContent=d.label;
      const n=document.createElement('span');n.className='n';
      n.textContent=fmt(d.v)+' · '+(d.v/total*100).toFixed(0)+'%';
      row.append(sw,lbl,n);legendNode.appendChild(row);
    });
  }
}

function wireToggle(btn,chartEl,tblEl){
  btn.addEventListener('click',()=>{
    const showTbl=chartEl.classList.toggle('hidden');
    tblEl.classList.toggle('hidden',!showTbl);
    btn.textContent=showTbl?'Chart view':'Table view';
    hideTip();
  });
}
"""

JS_APP = r"""
const D=DATA;
const EXTRA_SIGNALS=D.signals.filter(s=>!D.signalColorOrder.includes(s));
const SLOT=['var(--series-1)','var(--series-2)','var(--series-3)','var(--series-4)',
            'var(--series-5)','var(--series-6)','var(--series-7)','var(--series-8)'];
function prettySignal(s){return D.signalLabels[s]||s;}
function colorFor(sig){
  const i=D.signalColorOrder.indexOf(sig);
  return i>=0?SLOT[i]:'var(--other)';
}
function chartKeyFor(sig){return D.signalColorOrder.includes(sig)?sig:'__other__';}
function brandNameOf(eid){return D.brandNames[eid]||'';}
function brandEmailOf(eid){return D.brandEmails[eid]||'';}

// notifs -> plain objects once, indices resolved
const NOTIFS=D.notifs.map(([di,bi,si,c,rv])=>({
  date:D.dates[di],brand:D.brands[bi],signal:D.signals[si],clicked:!!c,received:!!rv}));

$('#sub').innerHTML='';
$('#sub').append(txt('Data '+D.dates[0]+' to '+D.dates[D.dates.length-1]+' · generated '+D.generated));
if(D.unstable.length){
  const b=document.createElement('div');b.className='banner';
  const dot=document.createElement('span');dot.className='dot';
  const msg=document.createElement('div');
  const strong=document.createElement('b');
  strong.textContent=D.unstable.length+' day(s) not yet settled';
  msg.append(strong,txt(' ('+D.unstable.join(', ')+'). Clicks keep arriving for a few days after send, so recent CTR is an undercount.'));
  b.append(dot,msg);$('#banners').appendChild(b);
}

// ── period state ──────────────────────────────────────────────────────────
const ANCHOR=D.dates[D.dates.length-1];
// All date math is UTC-only. Building the date at LOCAL midnight and reading it
// back with toISOString() (UTC) shifts it a day behind in any timezone ahead of
// UTC — in IST addDays(d,+1) returned d unchanged, which hung the coverage loop.
function addDays(dstr,n){const d=new Date(dstr+'T00:00:00Z');d.setUTCDate(d.getUTCDate()+n);
  return d.toISOString().slice(0,10);}
function daysBetween(a,b){
  return Math.round((new Date(b+'T00:00:00Z')-new Date(a+'T00:00:00Z'))/86400000);}
let periodStart=addDays(ANCHOR,-6), periodEnd=ANCHOR;

// ── brands with 0 sends in the trailing 15 days, or none ever ──────────────
// Global, not period-filtered: a brand going quiet (or never starting) needs
// to stay findable under the "Notifications off" toggle regardless of which
// period the rest of the page is showing. D.brands now covers every
// growth-plan brand on file (see fetch_brands.py's brand-tab roster), not
// just ones with send history, so this also catches brands whose push was
// off from day one - those never get a send queued at all, so they'd have
// zero rows in NOTIFS forever, not just for the last 15 days.
const STALE_WINDOW_DAYS=15;
// category: 'never' (zero sends ever) or 'quiet' (sent before, zero in the
// trailing window) - kept alongside the flat id list so the table can label
// which of the two each flagged brand is, not just that it's flagged.
function computeStaleBrands(){
  const cutoff=addDays(ANCHOR,-(STALE_WINDOW_DAYS-1));
  // If the pipeline itself sent nothing on the latest day, "went quiet"
  // isn't a per-brand signal - don't flag anyone on that basis. Brands with
  // zero sends ever are unaffected by this guard: that fact doesn't depend
  // on today's fetch succeeding.
  const pipelineHealthy=D.dates.length>=STALE_WINDOW_DAYS&&NOTIFS.some(n=>n.date===ANCHOR);
  const by=new Map();
  NOTIFS.forEach(n=>{
    let r=by.get(n.brand);
    if(!r){r={first:n.date,recent:0};by.set(n.brand,r);}
    if(n.date<r.first)r.first=n.date;
    if(n.date>=cutoff)r.recent++;
  });
  const out=[], category=new Map();
  D.brands.forEach(brand=>{
    const r=by.get(brand);
    if(!r){out.push(brand);category.set(brand,'never');return;}
    if(pipelineHealthy&&r.recent===0&&r.first<cutoff){
      out.push(brand);category.set(brand,'quiet');
    }
  });
  return {ids:out,category};
}
const {ids:STALE_BRANDS,category:STALE_CATEGORY}=computeStaleBrands();
const STALE_SET=new Set(STALE_BRANDS);
let notifOffActive=false;
let categoryFilter=new Set(['never','quiet']);

// "Notifications off" shows lifetime data regardless of period - picking any
// period afterward (preset button, custom range, or single date) needs to
// exit that mode, or the table silently keeps ignoring the newly-selected
// period even after its button stops looking active.
function exitNotifOff(){
  notifOffActive=false;
  $('#notifOffBtn').classList.remove('active');
  $('#catFilters').classList.add('hidden');
}

function setPreset(p){
  exitNotifOff();
  document.querySelectorAll('.fbtn').forEach(b=>b.classList.toggle('active',b.dataset.p===p));
  $('#customWrap').classList.toggle('hidden',p!=='custom');
  $('#singleWrap').classList.toggle('hidden',p!=='single');
  if(p==='week'){periodStart=addDays(ANCHOR,-6);periodEnd=ANCHOR;render();}
  else if(p==='month'){periodStart=ANCHOR.slice(0,8)+'01';periodEnd=ANCHOR;render();}
  else if(p==='custom'){
    $('#rangeStart').value=periodStart;$('#rangeEnd').value=periodEnd;
  } else if(p==='single'){
    $('#singleDate').value=periodEnd;periodStart=periodEnd;render();
  }
}
document.querySelectorAll('.fbtn[data-p]').forEach(b=>b.addEventListener('click',()=>setPreset(b.dataset.p)));
[$('#rangeStart'),$('#rangeEnd')].forEach(inp=>inp.addEventListener('change',()=>{
  if($('#rangeStart').value&&$('#rangeEnd').value){
    exitNotifOff();
    periodStart=$('#rangeStart').value;periodEnd=$('#rangeEnd').value;
    if(periodStart>periodEnd)[periodStart,periodEnd]=[periodEnd,periodStart];
    render();
  }
}));
$('#singleDate').addEventListener('change',()=>{
  exitNotifOff();
  periodStart=periodEnd=$('#singleDate').value;render();
});
[$('#rangeStart'),$('#rangeEnd'),$('#singleDate')].forEach(inp=>{
  inp.min=D.dates[0];inp.max=D.dates[D.dates.length-1];
});

// ── signal checkbox legend ──────────────────────────────────────────────
const checked=new Set([...D.signalColorOrder,'__other__']);
function legendKeys(){return [...D.signalColorOrder,...(EXTRA_SIGNALS.length?['__other__']:[])];}
function buildLegend(){
  const row=$('#legRow');row.innerHTML='';
  const items=[...D.signalColorOrder.map(s=>({key:s,label:prettySignal(s),color:colorFor(s)}))];
  if(EXTRA_SIGNALS.length) items.push({key:'__other__',
    label:'Other ('+EXTRA_SIGNALS.map(prettySignal).join(', ')+')',color:'var(--other)'});
  items.forEach(it=>{
    const lab=document.createElement('label');lab.className='leg-chip';
    const cb=document.createElement('input');cb.type='checkbox';cb.checked=checked.has(it.key);
    const sw=document.createElement('span');sw.className='sw';sw.style.background=it.color;
    const t=document.createElement('span');t.textContent=it.label;
    lab.append(cb,sw,t);
    if(!cb.checked)lab.classList.add('off');
    cb.addEventListener('change',()=>{
      if(cb.checked)checked.add(it.key);else checked.delete(it.key);
      lab.classList.toggle('off',!cb.checked);
      const g=document.querySelector('.series-g[data-key="'+it.key+'"]');
      if(g)g.style.display=cb.checked?'':'none';
      updateLegControls();
      renderTable();
    });
    row.appendChild(lab);
  });
  updateLegControls();
}
function updateLegControls(){
  const keys=legendKeys();
  $('#legAllBtn').disabled=checked.size===keys.length;
  $('#legNoneBtn').disabled=checked.size===0;
  $('#sigFilterLabel').textContent=checked.size===keys.length
    ?'All signals':checked.size+' of '+keys.length+' signals';
}
function setAllChecked(state){
  const keys=legendKeys();
  keys.forEach(k=>state?checked.add(k):checked.delete(k));
  buildLegend();
  renderChart();
  renderTable();
}
$('#legAllBtn').addEventListener('click',()=>setAllChecked(true));
$('#legNoneBtn').addEventListener('click',()=>setAllChecked(false));

// ── signal-filter dropdown open/close ───────────────────────────────────
const sigFilterEl=$('#sigFilter'), sigFilterPanel=$('#sigFilterPanel');
$('#sigFilterBtn').addEventListener('click',e=>{
  e.stopPropagation();
  const open=sigFilterPanel.classList.toggle('hidden');
  sigFilterEl.classList.toggle('open',!open);
});
document.addEventListener('click',e=>{
  if(!sigFilterEl.contains(e.target)){
    sigFilterPanel.classList.add('hidden');
    sigFilterEl.classList.remove('open');
  }
});
function expandChecked(){
  const out=new Set();
  checked.forEach(k=>{
    if(k==='__other__')EXTRA_SIGNALS.forEach(s=>out.add(s));
    else out.add(k);
  });
  return out;
}

// ── filtering + aggregation ─────────────────────────────────────────────
let periodNotifs=[], periodDates=[];
function recomputePeriod(){
  periodDates=D.dates.filter(d=>d>=periodStart&&d<=periodEnd);
  const dset=new Set(periodDates);
  periodNotifs=NOTIFS.filter(n=>dset.has(n.date));
}

function computeChartSeries(){
  const keys=[...D.signalColorOrder,...(EXTRA_SIGNALS.length?['__other__']:[])];
  const counts=new Map(keys.map(k=>[k,new Array(periodDates.length).fill(0)]));
  const dpos=new Map(periodDates.map((d,i)=>[d,i]));
  periodNotifs.forEach(n=>{
    const k=chartKeyFor(n.signal);
    counts.get(k)[dpos.get(n.date)]++;
  });
  return keys.map(k=>({
    key:k,
    label:k==='__other__'?'Other':prettySignal(k),
    color:k==='__other__'?'var(--other)':colorFor(k),
    values:counts.get(k),
    hidden:!checked.has(k),
  }));
}

function computeBrandStats(){
  const by=new Map();
  // "Notifications off" shows lifetime data for flagged brands (they have
  // nothing in any recent period by definition) - seeded up front so a
  // brand with literally zero sends ever still gets a (blank) row, since
  // there's no notif to derive one from.
  if(notifOffActive){
    STALE_BRANDS.forEach(brand=>{
      const cat=STALE_CATEGORY.get(brand);
      if(!categoryFilter.has(cat))return;
      by.set(brand,{brand,sends:0,clicks:0,received:0,bySig:new Map(),byDate:new Map(),matches:true,
        category:cat});
    });
    NOTIFS.forEach(n=>{
      const r=by.get(n.brand);
      if(!r)return;
      r.sends++; if(n.clicked)r.clicks++; if(n.received)r.received++;
      r.bySig.set(n.signal,(r.bySig.get(n.signal)||0)+1);
      if(!r.byDate.has(n.date))r.byDate.set(n.date,[]);
      r.byDate.get(n.date).push({signal:n.signal,label:prettySignal(n.signal),
        clicked:n.clicked,received:n.received});
    });
    return [...by.values()];
  }
  const active=expandChecked();
  periodNotifs.forEach(n=>{
    let r=by.get(n.brand);
    if(!r){r={brand:n.brand,sends:0,clicks:0,received:0,bySig:new Map(),byDate:new Map(),matches:false};by.set(n.brand,r);}
    r.sends++; if(n.clicked)r.clicks++; if(n.received)r.received++;
    r.bySig.set(n.signal,(r.bySig.get(n.signal)||0)+1);
    if(!r.byDate.has(n.date))r.byDate.set(n.date,[]);
    r.byDate.get(n.date).push({signal:n.signal,label:prettySignal(n.signal),
      clicked:n.clicked,received:n.received});
    if(active.has(n.signal))r.matches=true;
  });
  return [...by.values()].filter(r=>r.matches);
}

// ── state: sort, search, page, expanded ─────────────────────────────────
let sortKey='sends', sortDir=-1, page=1, expandedBrand=null;
const PAGE_SIZE=50;

function topSignals(bySig,n){
  return [...bySig.entries()].sort((a,b)=>b[1]-a[1]).slice(0,n);
}

function renderChart(){
  multiLineChart($('#sigChart'),periodDates,computeChartSeries(),{h:260});
}

function renderSigRank(){
  const agg=new Map();
  periodNotifs.forEach(n=>{
    const a=agg.get(n.signal)||{sends:0,clicks:0};
    a.sends++; if(n.clicked)a.clicks++;
    agg.set(n.signal,a);
  });
  const data=[...agg.entries()].sort((a,b)=>b[1].sends-a[1].sends).map(([sig,a])=>({
    label:prettySignal(sig),v:a.sends,
    rows:['Sends: '+fmt(a.sends),'Clicks: '+fmt(a.clicks),
      'CTR: '+(a.sends?(a.clicks/a.sends*100).toFixed(2):'0.00')+'%'],
  }));
  hbarChart($('#sigRank'),data,{labelW:150,rowH:26});
}

function renderTable(){
  let rows=computeBrandStats();
  if(notifOffActive){
    const multiCount=rows.filter(r=>[...r.byDate.values()].some(list=>list.length>1)).length;
    $('#brandsDesc').textContent='Brands with 0 sends in the last 15 days, or none ever — lifetime data, '
      +'ignores the period filter above. '+fmt(multiCount)+' of these had a day with more than one '
      +'notification while active.';
  }else{
    $('#brandsDesc').textContent='Click any row for its send calendar and signal mix.';
  }
  const q=$('#search').value.trim().toLowerCase();
  if(q)rows=rows.filter(r=>(r.brand+' '+brandNameOf(r.brand)+' '+brandEmailOf(r.brand))
    .toLowerCase().includes(q));
  rows.forEach(r=>{
    r.ctr=r.sends?r.clicks/r.sends*100:0;
    r.receivedRate=r.sends?r.received/r.sends*100:0;
    const top=topSignals(r.bySig,1);
    r.topShare=top.length?top[0][1]/r.sends*100:0;
  });
  rows.sort((a,b)=>sortDir*((a[sortKey]??0)-(b[sortKey]??0)) || a.brand.localeCompare(b.brand));

  $('#rowCount').textContent=fmt(rows.length)+' brand'+(rows.length===1?'':'s');
  const totalPages=Math.max(1,Math.ceil(rows.length/PAGE_SIZE));
  page=Math.min(page,totalPages);
  const pageRows=rows.slice((page-1)*PAGE_SIZE,page*PAGE_SIZE);

  const tbody=$('#tbody');tbody.innerHTML='';
  if(!pageRows.length){
    const tr=document.createElement('tr');const td=document.createElement('td');
    td.colSpan=7;td.className='empty-note';td.textContent='No brands match the current filters.';
    tr.appendChild(td);tbody.appendChild(tr);
  }
  pageRows.forEach((r,ri)=>{
    const tr=document.createElement('tr');tr.className='brand-row';
    const tdBrand=document.createElement('td');
    const brandInner=document.createElement('div');brandInner.className='cell-inline';
    const chev=document.createElement('span');chev.className='chev';
    const bn=brandNameOf(r.brand);
    const bnSpan=document.createElement('span');
    bnSpan.textContent=bn||'—';
    if(!bn)bnSpan.className='muted';
    brandInner.append(chev,bnSpan);tdBrand.appendChild(brandInner);

    const tdB=document.createElement('td');
    const eid=document.createElement('span');eid.className='eid';eid.textContent=r.brand;
    tdB.appendChild(eid);

    const tdSends=document.createElement('td');tdSends.className='num';tdSends.textContent=fmt(r.sends);
    // Confirmed Delivery undercounts even genuine deliveries (offline devices,
    // OS-level restrictions) - highlighted only when it's meaningfully low,
    // not treated as a hard failure signal on its own.
    const tdRecv=document.createElement('td');tdRecv.className='num';
    const recvPill=document.createElement('span');
    recvPill.className='pill'+(r.sends&&r.receivedRate<80?' hi':'');
    recvPill.textContent=fmt(r.received);tdRecv.appendChild(recvPill);
    const tdCtr=document.createElement('td');tdCtr.className='num';
    const ctrPill=document.createElement('span');ctrPill.className='pill';
    ctrPill.textContent=r.ctr.toFixed(1)+'%';tdCtr.appendChild(ctrPill);
    const tdShare=document.createElement('td');tdShare.className='num';
    const sharePill=document.createElement('span');
    sharePill.className='pill'+(r.topShare>=70?' hi':'');
    sharePill.textContent=r.topShare.toFixed(0)+'%';tdShare.appendChild(sharePill);

    // Two badges, then a "+N" chip — three full badges per row was the bulk of
    // the visual noise, and the overflow detail is one hover away.
    const tdSig=document.createElement('td');tdSig.className='sig-col';
    const badges=document.createElement('div');badges.className='badges';
    const allSigs=topSignals(r.bySig,99);
    allSigs.slice(0,2).forEach(([sig,c])=>{
      const b=document.createElement('span');b.className='badge';
      const sw=document.createElement('span');sw.className='sw';sw.style.background=colorFor(sig);
      const t=document.createElement('span');t.textContent=prettySignal(sig)+' ×'+c;
      b.append(sw,t);badges.appendChild(b);
    });
    if(allSigs.length>2){
      const rest=allSigs.slice(2);
      const more=document.createElement('span');more.className='badge more';
      more.textContent='+'+rest.length;
      more.addEventListener('mouseenter',e=>showTip(e,'Other signals',
        rest.map(([sig,c])=>prettySignal(sig)+' ×'+c)));
      more.addEventListener('mousemove',moveTip);
      more.addEventListener('mouseleave',hideTip);
      badges.appendChild(more);
    }
    // "Notifications off" shows lifetime data, so this is the one place a
    // brand's whole multi-signal-day history is visible without expanding
    // every row by hand.
    if(notifOffActive){
      const multiDays=[...r.byDate.values()].filter(list=>list.length>1);
      if(multiDays.length){
        const maxDay=Math.max(...multiDays.map(l=>l.length));
        const warn=document.createElement('span');warn.className='warn-chip';
        warn.textContent='⚠ '+multiDays.length+'/'+r.byDate.size+' days >1/day';
        warn.addEventListener('mouseenter',e=>showTip(e,'Duplicate sends',[
          'Spec is one Daily Round-up push per brand per day.',
          multiDays.length+' of '+r.byDate.size+' active day(s) here sent more than one'+
            (maxDay>2?' (up to '+maxDay+' in a single day)':''),
        ]));
        warn.addEventListener('mousemove',moveTip);
        warn.addEventListener('mouseleave',hideTip);
        badges.appendChild(warn);
      }
    }
    tdSig.appendChild(badges);

    tr.append(tdBrand,tdB,tdSends,tdRecv,tdCtr,tdShare,tdSig);
    tbody.appendChild(tr);

    const detail=document.createElement('tr');detail.className='detail-row';
    const dtd=document.createElement('td');dtd.colSpan=7;
    detail.appendChild(dtd);
    tbody.appendChild(detail);

    const open=r.brand===expandedBrand;
    if(open){tr.classList.add('expanded');detail.classList.add('show');buildDetail(dtd,r);}

    tr.addEventListener('click',()=>{
      expandedBrand=(expandedBrand===r.brand)?null:r.brand;
      renderTable();
    });
  });
  renderPagination(totalPages);
}

function buildDetail(node,r){
  node.innerHTML='';
  const card=document.createElement('div');card.className='detail-card';

  // Identity lives in the card, not in a table column — an email column would
  // widen every row for a value you only need once you've drilled in.
  const ident=document.createElement('div');ident.className='detail-ident';
  const nm=document.createElement('div');nm.className='ident-name';
  nm.textContent=brandNameOf(r.brand)||'Unmapped brand';
  if(!brandNameOf(r.brand))nm.classList.add('muted');
  const meta=document.createElement('div');meta.className='ident-meta';
  const em=brandEmailOf(r.brand);
  if(em){
    const a=document.createElement('span');a.className='ident-email';a.textContent=em;
    meta.appendChild(a);
  }
  const idSpan=document.createElement('span');idSpan.className='eid';idSpan.textContent=r.brand;
  meta.appendChild(idSpan);
  ident.append(nm,meta);card.appendChild(ident);

  // Compact header: key stats as chips, plus a hover-only warning for the
  // duplicate-send case instead of a paragraph eating card space up front.
  const head=document.createElement('div');head.className='detail-head';
  [['Sends',fmt(r.sends)],['Received',fmt(r.received)],['CTR',r.ctr.toFixed(1)+'%'],
    ['Repeats',r.topShare.toFixed(0)+'%']]
    .forEach(([label,val])=>{
      const chip=document.createElement('div');chip.className='stat-chip';
      const v=document.createElement('span');v.className='v';v.textContent=val;
      const l=document.createElement('span');l.className='l';l.textContent=label;
      chip.append(v,l);head.appendChild(chip);
    });
  // Spec is one DRU push per brand per day. Most days hold to that, but some
  // brands get several signals the same day — flag it here instead of letting
  // a crowded day read as a rendering glitch.
  const multiDays=[...r.byDate.values()].filter(list=>list.length>1);
  if(multiDays.length){
    const maxDay=Math.max(...multiDays.map(l=>l.length));
    const warn=document.createElement('span');warn.className='warn-chip';
    warn.textContent='⚠ '+multiDays.length+'/'+r.byDate.size+' days multi-signal';
    warn.addEventListener('mouseenter',e=>showTip(e,'Duplicate sends',[
      'Spec is one Daily Round-up push per brand per day.',
      multiDays.length+' of '+r.byDate.size+' day(s) here sent more than one'+
        (maxDay>2?' (up to '+maxDay+' in a single day)':''),
      'Data-quality issue upstream, not a rendering artifact.',
    ]));
    warn.addEventListener('mousemove',moveTip);
    warn.addEventListener('mouseleave',hideTip);
    head.appendChild(warn);
  }
  card.appendChild(head);

  const grid=document.createElement('div');grid.className='detail-grid';

  const left=document.createElement('div');
  const lh=document.createElement('div');lh.className='detail-h';
  lh.textContent='Which signal, which day';
  left.appendChild(lh);
  const calWrap=document.createElement('div');calWrap.className='dotcal-wrap';
  const calSvg=document.createElement('div');calWrap.appendChild(calSvg);
  left.appendChild(calWrap);
  const calNote=document.createElement('div');calNote.className='dotcal-note';
  calNote.textContent='Hollow center = clicked. Amber ring = not all sends here confirmed delivery. '
    +'Amber column = more than one signal that day.';
  left.appendChild(calNote);

  const right=document.createElement('div');
  const rh=document.createElement('div');rh.className='detail-h';rh.textContent='Signal mix';
  const pieHost=document.createElement('div');
  const pieLegend=document.createElement('div');pieLegend.className='pie-legend';
  right.append(rh,pieHost,pieLegend);

  grid.append(left,right);
  card.appendChild(grid);
  node.appendChild(card);

  dotCalendar(calSvg,notifOffActive?D.dates:periodDates,r.byDate,colorFor);

  const top=topSignals(r.bySig,5);
  const restCount=[...r.bySig.values()].reduce((s,v)=>s+v,0)-top.reduce((s,[,v])=>s+v,0);
  const pieData=top.map(([sig,v])=>({label:prettySignal(sig),v,color:colorFor(sig)}));
  if(restCount>0)pieData.push({label:'Other',v:restCount,color:'var(--other)'});
  pieChart(pieHost,pieData,pieLegend);
}

function renderPagination(totalPages){
  const bar=$('#pagin');bar.innerHTML='';
  const info=document.createElement('div');info.textContent='Page '+page+' of '+totalPages;
  const btns=document.createElement('div');btns.className='btns';
  function mkBtn(label,p,disabled,cur){
    const b=document.createElement('button');b.textContent=label;
    if(disabled)b.disabled=true;if(cur)b.classList.add('cur');
    b.addEventListener('click',()=>{page=p;renderTable();});
    return b;
  }
  btns.appendChild(mkBtn('‹',Math.max(1,page-1),page===1,false));
  const span=3;
  let lo=Math.max(1,page-span),hi=Math.min(totalPages,page+span);
  if(lo>1)btns.appendChild(mkBtn('1',1,false,page===1));
  if(lo>2){const d=document.createElement('span');d.textContent='…';d.style.padding='0 4px';btns.appendChild(d);}
  for(let p=lo;p<=hi;p++)btns.appendChild(mkBtn(String(p),p,false,p===page));
  if(hi<totalPages-1){const d=document.createElement('span');d.textContent='…';d.style.padding='0 4px';btns.appendChild(d);}
  if(hi<totalPages)btns.appendChild(mkBtn(String(totalPages),totalPages,false,page===totalPages));
  btns.appendChild(mkBtn('›',Math.min(totalPages,page+1),page===totalPages,false));
  bar.append(info,btns);
}

document.querySelectorAll('th.sortable').forEach(th=>{
  th.addEventListener('click',()=>{
    const k=th.dataset.key;
    if(sortKey===k)sortDir*=-1;else{sortKey=k;sortDir=-1;}
    document.querySelectorAll('th.sortable .arrow').forEach(a=>a.textContent='');
    th.querySelector('.arrow').textContent=sortDir===-1?'↓':'↑';
    page=1;renderTable();
  });
});

const receivedHelp=$('#receivedHelp');
receivedHelp.addEventListener('mouseenter',e=>showTip(e,'Received',[
  'OneSignal’s Confirmed Delivery — a receipt sent back by the device',
  'once the push actually lands, not just accepted by the push service.',
  'Undercounts even genuine deliveries (offline devices, OS-level',
  'restrictions), so a gap from Sends isn’t proof of a failed send.',
  'Highlighted when under 80% of Sends.',
]));
receivedHelp.addEventListener('mousemove',moveTip);
receivedHelp.addEventListener('mouseleave',hideTip);
receivedHelp.addEventListener('click',e=>e.stopPropagation());

// Header help: explain "Repeats" on hover. Click must not reach the <th> or it
// would toggle the sort as a side effect of reading the definition.
const repeatsHelp=$('#repeatsHelp');
repeatsHelp.addEventListener('mouseenter',e=>showTip(e,'Repeats',[
  'The top signal’s share of that brand’s sends in this period.',
  '100% = every push was the same signal.',
  'Highlighted at 70%+ — the brand is getting a near-identical push most days.',
]));
repeatsHelp.addEventListener('mousemove',moveTip);
repeatsHelp.addEventListener('mouseleave',hideTip);
repeatsHelp.addEventListener('click',e=>e.stopPropagation());

let searchTimer;
$('#search').addEventListener('input',()=>{
  clearTimeout(searchTimer);
  searchTimer=setTimeout(()=>{page=1;renderTable();},150);
});

const notifOffHelp=$('#notifOffHelp');
notifOffHelp.addEventListener('mouseenter',e=>showTip(e,'Notifications off',[
  'Brand had sends before, but zero in the last 15 days — OR —',
  'brand has had zero sends since it was added to the roster.',
  'Lifetime data, ignores the period filter above.',
]));
notifOffHelp.addEventListener('mousemove',moveTip);
notifOffHelp.addEventListener('mouseleave',hideTip);

const notifOffBtn=$('#notifOffBtn');
notifOffBtn.textContent='Notifications off ('+fmt(STALE_BRANDS.length)+')';
notifOffBtn.addEventListener('click',()=>{
  notifOffActive=!notifOffActive;
  notifOffBtn.classList.toggle('active',notifOffActive);
  $('#catFilters').classList.toggle('hidden',!notifOffActive);
  page=1;renderTable();
});

const CAT_COUNT={
  never:STALE_BRANDS.filter(b=>STALE_CATEGORY.get(b)==='never').length,
  quiet:STALE_BRANDS.filter(b=>STALE_CATEGORY.get(b)==='quiet').length,
};
document.querySelectorAll('#catFilters .cap').forEach(btn=>{
  const cat=btn.dataset.cat;
  btn.textContent=(cat==='never'?'Never turned on':'Off 15+ days')+' ('+fmt(CAT_COUNT[cat])+')';
  btn.classList.toggle('active',categoryFilter.has(cat));
  btn.addEventListener('click',()=>{
    if(categoryFilter.has(cat))categoryFilter.delete(cat);else categoryFilter.add(cat);
    btn.classList.toggle('active',categoryFilter.has(cat));
    page=1;renderTable();
  });
});

// ── KPIs, with a vs-prior-period delta on each tile ─────────────────────
function statsFor(dates){
  const dset=new Set(dates);
  const rows=NOTIFS.filter(n=>dset.has(n.date));
  const sends=rows.length, clicks=rows.filter(n=>n.clicked).length,
    brands=new Set(rows.map(n=>n.brand)).size;
  return {sends,clicks,brands,ctr:sends?clicks/sends*100:0};
}
const DATE_SET=new Set(D.dates);
// Every calendar day in [start,end], and how many of them the dataset actually
// holds. A window is only comparable when it is FULLY covered: comparing a
// 17-day period against a prior window the data only half-covers reported
// "+96%" growth that was really just four missing days plus the pilot ramp.
function coverage(start,end){
  const all=[];
  // Hard bound: a non-advancing date helper must never be able to freeze the tab.
  for(let d=start,guard=0;d<=end&&guard<4000;d=addDays(d,1),guard++)all.push(d);
  const have=all.filter(d=>DATE_SET.has(d));
  return {all,have,full:all.length>0&&have.length===all.length};
}
function prevRange(){
  const days=daysBetween(periodStart,periodEnd)+1;
  return {start:addDays(periodStart,-days),end:addDays(periodStart,-1),days};
}
// isPoints=true compares as an absolute-point delta (for CTR%); otherwise a
// relative % change. Rendered only when both windows are fully covered —
// otherwise the tile stays blank rather than showing an inflated number.
function applyDelta(id,curV,prevV,comparable,isPoints,windowLabel){
  const node=$('#'+id);
  node.onmouseenter=node.onmousemove=node.onmouseleave=null;
  if(!comparable){node.textContent='';node.className='kpi-delta';return;}
  let diff,label;
  if(isPoints){
    diff=curV-prevV;
    label=(diff>0?'+':'')+diff.toFixed(1)+'pp';
  }else if(prevV===0){
    if(curV===0){node.textContent='';node.className='kpi-delta';return;}
    diff=1;label='new';
  }else{
    diff=(curV-prevV)/prevV*100;
    label=(diff>0?'+':'')+diff.toFixed(1)+'%';
  }
  const dir=diff>0?'up':diff<0?'down':'flat';
  node.className='kpi-delta '+dir;
  node.textContent=(dir==='up'?'↑ ':dir==='down'?'↓ ':'')+label;
  node.onmouseenter=e=>showTip(e,'Compared with',[windowLabel,
    'Same length as the selected period, ending the day before it starts.']);
  node.onmousemove=moveTip;
  node.onmouseleave=hideTip;
}
function renderKpis(){
  const cur=statsFor(periodDates);
  const pr=prevRange();
  const curCov=coverage(periodStart,periodEnd), prevCov=coverage(pr.start,pr.end);
  const comparable=curCov.full&&prevCov.full;
  const prev=statsFor(prevCov.have);
  const windowLabel=pr.start+' → '+pr.end;
  $('#kSends').textContent=fmt(cur.sends);
  $('#kBrands').textContent=fmt(cur.brands);
  $('#kClicks').textContent=fmt(cur.clicks);
  $('#kCtr').textContent=cur.ctr.toFixed(2)+'%';
  applyDelta('kSendsDelta',cur.sends,prev.sends,comparable,false,windowLabel);
  applyDelta('kBrandsDelta',cur.brands,prev.brands,comparable,false,windowLabel);
  applyDelta('kClicksDelta',cur.clicks,prev.clicks,comparable,false,windowLabel);
  applyDelta('kCtrDelta',cur.ctr,prev.ctr,comparable,true,windowLabel);
}

function render(){
  recomputePeriod();
  buildLegend();
  renderKpis();
  renderSigRank();
  renderChart();
  page=1;
  renderTable();
}

setPreset('week');

addEventListener('resize',()=>{clearTimeout(window._rt);
  window._rt=setTimeout(()=>{renderSigRank();renderChart();if(expandedBrand){
    const r=computeBrandStats().find(x=>x.brand===expandedBrand);
    if(r){const dtd=document.querySelector('tr.detail-row.show td');if(dtd)buildDetail(dtd,r);}
  }},150);});

const btn=$('#themeBtn');
const setT=t=>{document.documentElement.setAttribute('data-theme',t);
  btn.textContent=t==='dark'?'Light':'Dark';setTimeout(render,20);};
setT('light');
btn.addEventListener('click',()=>setT(
  document.documentElement.getAttribute('data-theme')==='dark'?'light':'dark'));
"""


FUNNEL_VIEW = (
    '<div id="view-funnel" class="view hidden">\n'
    '  <div class="hero hero-funnel">\n'
    '    <div class="hero-head">\n'
    "      <h1>Growth &rarr; DRU adoption funnel</h1>\n"
    '      <div class="sub" id="fnlSub"></div>\n'
    "    </div>\n"
    "  </div>\n"
    '  <div class="filterbar card" id="fnlFilterbar">\n'
    '    <button class="fbtn" data-fp="week">This week</button>\n'
    '    <button class="fbtn" data-fp="month">This month</button>\n'
    '    <button class="fbtn" data-fp="custom">Custom range</button>\n'
    '    <button class="fbtn" data-fp="single">Single date</button>\n'
    '    <div class="customwrap hidden" id="fnlCustomWrap">\n'
    '      <input type="date" id="fnlRangeStart"> <span>to</span> <input type="date" id="fnlRangeEnd">\n'
    "    </div>\n"
    '    <div class="customwrap hidden" id="fnlSingleWrap">\n'
    '      <input type="date" id="fnlSingleDate">\n'
    "    </div>\n"
    "  </div>\n"
    '  <div id="fnlBanners"></div>\n'
    '  <div class="card">\n'
    '    <div class="card-head"><div>\n'
    '      <h2>The funnel <span class="help" id="fnlHelp">i</span></h2>\n'
    '      <div class="desc">Where Growth businesses get to on the path from having the app to '
    "reading their Daily Round-up, for the selected period. Stages 4&ndash;6 count businesses / users "
    "that reached that stage; stages 1&ndash;3 are current counts.</div>\n"
    "    </div></div>\n"
    '    <div class="fnl-steps" id="fnlSteps"></div>\n'
    "  </div>\n"
    '  <div class="card">\n'
    '    <div class="card-head"><div>\n'
    "      <h2>How each number is measured</h2>\n"
    '      <div class="desc">Three systems, no shared identity key yet &mdash; read this before quoting a number.</div>\n'
    "    </div></div>\n"
    '    <div class="tbl-wrap"><table>\n'
    "      <thead><tr>\n"
    "        <th>Stage</th><th class=\"num\">Count</th><th class=\"num\">of Growth</th>"
    "<th>Counts</th><th>Source</th>\n"
    "      </tr></thead>\n"
    '      <tbody id="fnlTableBody"></tbody>\n'
    "    </table></div>\n"
    "  </div>\n"
    '  <footer id="fnlFoot"></footer>\n'
    "</div>\n"
)


JS_FUNNEL = r"""
const FN=DATA.funnel||{snapshots:[],latest:null,openedByDate:{}};
const FSNAPS=FN.snapshots||[];
const FOPEN=FN.openedByDate||{};                 // date -> [opaque id idx]
const FDATES=FSNAPS.map(s=>s.date);              // tracked days, ascending
const FMIN=FDATES[0], FANCHOR=FDATES[FDATES.length-1];
const FSETTLING=new Set(FSNAPS.filter(s=>s.settling).map(s=>s.date));

// DRU received / tapped / sent per day, brand-level, from the notif payload
const FDRU=new Map();   // date -> {sent:Set, recv:Set, tap:Set}
NOTIFS.forEach(n=>{
  let r=FDRU.get(n.date);
  if(!r){r={sent:new Set(),recv:new Set(),tap:new Set()};FDRU.set(n.date,r);}
  r.sent.add(n.brand);
  if(n.received)r.recv.add(n.brand);
  if(n.clicked)r.tap.add(n.brand);
});

let fStart=FANCHOR, fEnd=FANCHOR;   // default: latest day

function dropClass(pct){return pct>=70?'ok':pct>=45?'warn':'bad';}

function renderFunnelSteps(snap){
  const host=$('#fnlSteps');host.innerHTML='';
  const steps=snap.steps||[];
  const base=(steps[0]&&steps[0].value)||1;
  steps.forEach((s,i)=>{
    if(i>0){
      const prev=steps[i-1];
      const conn=document.createElement('div');conn.className='fnl-conn';
      const rule1=document.createElement('span');rule1.className='rule';
      const mid=document.createElement('div');mid.className='mid';
      const tag=document.createElement('span');
      const pct=prev.value?s.value/prev.value*100:0;
      const unitFlip=s.unit!==prev.unit;
      tag.className='drop-tag '+(pct>100?'ok':dropClass(pct));
      tag.textContent=(pct>100?'↑ ':'')+pct.toFixed(0)+'% of “'+prev.label+'”';
      tag.addEventListener('mouseenter',e=>showTip(e,prev.label+'  →  '+s.label,[
        fmt(s.value)+' of '+fmt(prev.value)+'  ('+pct.toFixed(1)+'%)',
        pct>100?'Above 100%: this stage is reached by routes that skip the previous one (opening DRU in-app without the push).':'',
        unitFlip?('Unit changes here: “'+prev.label+'” counts '+prev.unit+', “'+s.label+'” counts '+s.unit+'. Ratio is approximate.'):'',
      ].filter(Boolean)));
      tag.addEventListener('mousemove',moveTip);tag.addEventListener('mouseleave',hideTip);
      const rule2=document.createElement('span');rule2.className='rule';
      mid.append(rule1,tag,rule2);
      conn.append(document.createElement('div'),mid,document.createElement('div'));
      host.appendChild(conn);
    }
    const row=document.createElement('div');row.className='fnl-step';
    const lab=document.createElement('div');lab.className='fnl-label';
    const nm=document.createElement('div');nm.className='n';
    nm.append(txt((i+1)+'. '+s.label));
    const help=document.createElement('span');help.className='help';help.textContent='i';
    help.addEventListener('mouseenter',e=>showTip(e,s.label,[s.source,
      (s.unit==='users'||s.unit==='app users')?'Counted as individual people/logins, not businesses.':'Counted as businesses (any qualifying contact counts the business).']));
    help.addEventListener('mousemove',moveTip);help.addEventListener('mouseleave',hideTip);
    nm.appendChild(help);
    const u=document.createElement('div');u.className='u'+((s.unit==='users'||s.unit==='app users')?' people':'');
    u.textContent=s.unit;
    lab.append(nm,u);

    const barWrap=document.createElement('div');barWrap.className='fnl-bar-wrap';
    const bar=document.createElement('div');bar.className='fnl-bar';
    const val=document.createElement('div');val.className='fnl-bar-val';
    const w=base?Math.max(s.value/base*100,1.5):1.5;
    bar.style.width=w+'%';
    val.textContent=fmt(s.value);
    if(w>=88){val.style.right='10px';val.style.color='var(--f-solid-fg)';}
    else{val.style.left='calc('+w+'% + 8px)';}
    barWrap.append(bar,val);
    barWrap.addEventListener('mouseenter',e=>showTip(e,s.label,[
      fmt(s.value)+' '+s.unit,
      (base?(s.value/base*100).toFixed(1):'0')+'% of all Growth businesses',s.source]));
    barWrap.addEventListener('mousemove',moveTip);barWrap.addEventListener('mouseleave',hideTip);

    const pctCol=document.createElement('div');pctCol.className='fnl-pct';
    pctCol.textContent=(base?(s.value/base*100).toFixed(0):'0')+'%';
    const psub=document.createElement('span');psub.className='sub';psub.textContent='of Growth';
    pctCol.appendChild(psub);

    row.append(lab,barWrap,pctCol);
    host.appendChild(row);

    if(s.secondary&&s.secondary.value!=null){
      const sec=document.createElement('div');sec.className='fnl-2nd';
      const chip=document.createElement('span');chip.className='chip';
      chip.textContent=fmt(s.secondary.value)+' '+s.secondary.label;
      chip.addEventListener('mouseenter',e=>showTip(e,s.label+' — also',
        [fmt(s.secondary.value)+' '+(s.secondary.unit||''),s.secondary.source]));
      chip.addEventListener('mousemove',moveTip);chip.addEventListener('mouseleave',hideTip);
      sec.appendChild(chip);host.appendChild(sec);
    }
  });
}

function renderFunnelTable(snap){
  const body=$('#fnlTableBody');body.innerHTML='';
  const base=(snap.steps[0]&&snap.steps[0].value)||0;
  snap.steps.forEach((s,i)=>{
    const tr=document.createElement('tr');
    const c0=document.createElement('td');c0.textContent=(i+1)+'. '+s.label;
    const c1=document.createElement('td');c1.className='num';c1.textContent=fmt(s.value);
    const c2=document.createElement('td');c2.className='num';
    c2.textContent=base?(s.value/base*100).toFixed(1)+'%':'—';
    const c3=document.createElement('td');
    const pill=document.createElement('span');pill.className='pill';pill.textContent=s.unit;
    if(s.unit==='users'||s.unit==='app users')pill.style.color='var(--f-3)';
    c3.appendChild(pill);
    const c4=document.createElement('td');
    c4.style.cssText='max-width:380px;white-space:normal;color:var(--text-muted);font-size:11.5px';
    c4.textContent=s.source;
    tr.append(c0,c1,c2,c3,c4);body.appendChild(tr);
  });
}

// ── build the 6 steps for a [start,end] window ──────────────────────────────
function fWindowDates(){return FDATES.filter(d=>d>=fStart&&d<=fEnd);}

function funnelForWindow(){
  const wd=fWindowDates();
  const one=fStart===fEnd;
  const label=one?fStart:(fStart+' → '+fEnd);
  // stages 1-3: current state, from the newest snapshot in (or before) the window
  let base=FSNAPS[FSNAPS.length-1];
  for(let i=FSNAPS.length-1;i>=0;i--){ if(FSNAPS[i].date<=fEnd){base=FSNAPS[i];break;} }
  const cur={};(base.steps||[]).forEach(s=>cur[s.key]=s);

  const sent=new Set(),recv=new Set(),tap=new Set();
  wd.forEach(d=>{const r=FDRU.get(d);if(r){r.sent.forEach(x=>sent.add(x));r.recv.forEach(x=>recv.add(x));r.tap.forEach(x=>tap.add(x));}});
  const opened=new Set();
  wd.forEach(d=>(FOPEN[d]||[]).forEach(x=>opened.add(x)));

  const per=one?'on '+label:'at least once in '+label;
  return {date:label,one:one,
    settling:wd.some(d=>FSETTLING.has(d)),
    steps:[
      {...cur.growth},
      {...cur.with_app},
      {...cur.notif_on},
      {key:'received',label:'Received a DRU',unit:'businesses',value:recv.size,
       source:'OneSignal Confirmed Delivery for a prod_dru_* push '+per+' ('+fmt(sent.size)+' were sent).',
       secondary:{label:'sent a DRU '+per,value:sent.size,unit:'businesses',
         source:'OneSignal — a prod_dru_* push queued and accepted by the push service.'}},
      {key:'tapped',label:'Tapped a DRU',unit:'businesses',value:tap.size,
       source:'OneSignal — a prod_dru_* push clicked '+per+'. Brand-level (converted > 0).'},
      {key:'opened',label:'Opened the DRU screen',unit:'users',value:opened.size,
       source:'Amplitude DailyRoundupStoryView, plan = growth, all platforms, '+per+'. '
         +(one?'Matches Amplitude’s own daily unique-user count.':'Distinct users across the range.')},
    ]};
}

function renderFunnel(){
  if(!FSNAPS.length){
    $('#fnlSub').textContent='No snapshots yet.';
    $('#fnlSteps').innerHTML='<div class="empty-note">Run scripts/fetch_funnel.py to write the first snapshot.</div>';
    return;
  }
  // clamp to tracked range
  if(fStart<FMIN)fStart=FMIN; if(fEnd>FANCHOR)fEnd=FANCHOR; if(fStart>fEnd)fStart=fEnd;
  const snap=funnelForWindow();
  const nd=fWindowDates().length;
  $('#fnlSub').textContent=(snap.one?('For '+snap.date):(nd+' days · '+snap.date))
    +' · tracking since '+FMIN;

  const bn=$('#fnlBanners');bn.innerHTML='';
  if(snap.settling){
    const b=document.createElement('div');b.className='banner';
    b.append(Object.assign(document.createElement('span'),{className:'dot'}));
    const m=document.createElement('div');
    m.innerHTML='<b>Recent days are still settling.</b> Late clicks and Amplitude events keep '+
      'arriving for a few days, so Tapped and Opened for the newest days will still rise.';
    b.appendChild(m);bn.appendChild(b);
  }
  renderFunnelSteps(snap);
  renderFunnelTable(snap);
  $('#fnlFoot').innerHTML='Stages 4–6 are '+(snap.one?'the activity for <b>'+snap.date+'</b>':
    '<b>distinct</b> businesses / users over <b>'+snap.date+'</b>')+'; stages 1–3 are current counts '+
    '(they can’t be rebuilt for a past day). Stages 1–5 count <b>businesses</b>, stage 6 counts <b>users</b>. '+
    'Not a strict funnel — a business can open DRU in-app without a push, so step 6 can exceed step 5. '+
    'No backfill: tracking started '+FMIN+'.';
}

// ── filter bar (same behaviour as the notification tab) ─────────────────────
function fSetPreset(p){
  document.querySelectorAll('#fnlFilterbar .fbtn').forEach(b=>b.classList.toggle('active',b.dataset.fp===p));
  $('#fnlCustomWrap').classList.toggle('hidden',p!=='custom');
  $('#fnlSingleWrap').classList.toggle('hidden',p!=='single');
  if(p==='week'){fStart=addDays(FANCHOR,-6);fEnd=FANCHOR;renderFunnel();}
  else if(p==='month'){fStart=FANCHOR.slice(0,8)+'01';fEnd=FANCHOR;renderFunnel();}
  else if(p==='custom'){$('#fnlRangeStart').value=fStart<FMIN?FMIN:fStart;$('#fnlRangeEnd').value=fEnd;}
  else if(p==='single'){$('#fnlSingleDate').value=fEnd;fStart=fEnd;renderFunnel();}
}
document.querySelectorAll('#fnlFilterbar .fbtn').forEach(b=>b.addEventListener('click',()=>fSetPreset(b.dataset.fp)));
[$('#fnlRangeStart'),$('#fnlRangeEnd')].forEach(inp=>inp.addEventListener('change',()=>{
  if($('#fnlRangeStart').value&&$('#fnlRangeEnd').value){
    fStart=$('#fnlRangeStart').value;fEnd=$('#fnlRangeEnd').value;
    if(fStart>fEnd)[fStart,fEnd]=[fEnd,fStart];
    renderFunnel();
  }
}));
$('#fnlSingleDate').addEventListener('change',()=>{fStart=fEnd=$('#fnlSingleDate').value;renderFunnel();});
[$('#fnlRangeStart'),$('#fnlRangeEnd'),$('#fnlSingleDate')].forEach(inp=>{inp.min=FMIN;inp.max=FANCHOR;});

const fnlHelp=$('#fnlHelp');
if(fnlHelp){
  fnlHelp.addEventListener('mouseenter',e=>showTip(e,'Reading this funnel',[
    'Bar length = share of all Growth businesses.',
    'The tag between two steps = the second as a % of the first.',
    'For a range, stages 4–6 count businesses/users that hit that stage at least once.',
    'Stages 1–3 are current counts. No backfill — pick any day since '+FMIN+'.',
    'Not strictly nested — see the note at the bottom.']));
  fnlHelp.addEventListener('mousemove',moveTip);
  fnlHelp.addEventListener('mouseleave',hideTip);
}

function setActiveView(v){
  document.documentElement.setAttribute('data-view',v);
  document.querySelectorAll('.viewnav button').forEach(x=>x.classList.toggle('active',x.dataset.view===v));
  $('#view-notif').classList.toggle('hidden',v!=='notif');
  $('#view-funnel').classList.toggle('hidden',v!=='funnel');
  hideTip();
  if(v==='funnel'){if(!$('#fnlFilterbar .fbtn.active'))fSetPreset('single');else renderFunnel();}
  else{renderSigRank();renderChart();}
}
document.querySelectorAll('.viewnav button').forEach(b=>{
  b.addEventListener('click',()=>setActiveView(b.dataset.view));
});
setActiveView('notif');

addEventListener('resize',()=>{clearTimeout(window._frt);
  window._frt=setTimeout(()=>{if(!$('#view-funnel').classList.contains('hidden'))renderFunnel();},160);});
btn.addEventListener('click',()=>{
  if(!$('#view-funnel').classList.contains('hidden'))setTimeout(renderFunnel,30);
});
"""


def build(payload):
    head = (
        '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width,initial-scale=1">\n'
        "<title>Daily Round-up — Notification Tracking</title>\n"
        "<link rel=\"icon\" href=\"data:image/svg+xml,"
        "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'>"
        "<text y='.9em' font-size='90'>%F0%9F%94%94</text></svg>\">\n"
        "<style>" + CSS + "</style></head><body>\n"
        '<div class="wrap">\n'
        '<div class="topbar">\n'
        '  <div class="mark">Daily Round-up <span>· Reelo</span></div>\n'
        '  <div class="viewnav">\n'
        '    <button data-view="notif" class="active" type="button">Notification tracking</button>\n'
        '    <button data-view="funnel" type="button">Growth adoption funnel</button>\n'
        "  </div>\n"
        '  <button class="theme-btn" id="themeBtn">Dark</button>\n'
        "</div>\n"
        '<div id="view-notif" class="view">\n'
        '<div class="hero">\n'
        '  <div class="hero-head">\n'
        "    <h1>Daily Round-up — Notification Tracking</h1>\n"
        '    <div class="sub" id="sub"></div>\n'
        "  </div>\n"
        '  <div class="hero-card">\n'
        '    <div class="kpi"><div class="label">Notifications sent</div>'
        '<div class="value-row"><span class="value" id="kSends"></span>'
        '<span class="kpi-delta" id="kSendsDelta"></span></div>'
        '<div class="note">selected period</div></div>\n'
        '    <div class="kpi"><div class="label">Brands reached</div>'
        '<div class="value-row"><span class="value" id="kBrands"></span>'
        '<span class="kpi-delta" id="kBrandsDelta"></span></div>'
        '<div class="note">selected period</div></div>\n'
        '    <div class="kpi"><div class="label">Clicks</div>'
        '<div class="value-row"><span class="value" id="kClicks"></span>'
        '<span class="kpi-delta" id="kClicksDelta"></span></div>'
        '<div class="note">unique brand opens</div></div>\n'
        '    <div class="kpi"><div class="label">Click rate</div>'
        '<div class="value-row"><span class="value" id="kCtr"></span>'
        '<span class="kpi-delta" id="kCtrDelta"></span></div>'
        '<div class="note">clicks ÷ sends</div></div>\n'
        "  </div>\n"
        "</div>\n"
        '<div id="banners"></div>\n'
        '<div class="filterbar card">\n'
        '  <button class="fbtn" data-p="week">This week</button>\n'
        '  <button class="fbtn" data-p="month">This month</button>\n'
        '  <button class="fbtn" data-p="custom">Custom range</button>\n'
        '  <button class="fbtn" data-p="single">Single date</button>\n'
        '  <div class="customwrap hidden" id="customWrap">\n'
        '    <input type="date" id="rangeStart"> <span>to</span> <input type="date" id="rangeEnd">\n'
        "  </div>\n"
        '  <div class="customwrap hidden" id="singleWrap">\n'
        '    <input type="date" id="singleDate">\n'
        "  </div>\n"
        '  <div class="search-wrap" style="max-width:340px"><span class="search-ic">⌕</span>'
        '<input class="searchbox" id="search" placeholder="Search brand, email, or external ID…"></div>\n'
        '  <button class="fbtn" id="notifOffBtn" type="button" style="margin-left:auto">Notifications off</button>\n'
        '  <span class="help" id="notifOffHelp">i</span>\n'
        "</div>\n"
        '<div class="card">\n'
        '  <div class="card-head"><div>\n'
        "    <h2>Top signals — ranked</h2>\n"
        '    <div class="desc">Every signal, most-sent first, totalled over the selected period.</div>\n'
        "  </div></div>\n"
        '  <div id="sigRank"></div>\n'
        "</div>\n"
        '<div class="card">\n'
        '  <div class="card-head">\n'
        "    <div>\n"
        "      <h2>Top signals — daily trend</h2>\n"
        '      <div class="desc">Notifications sent per day, by signal, over the selected period. '
        "Also filters the brand table below.</div>\n"
        "    </div>\n"
        '    <div class="sig-filter" id="sigFilter">\n'
        '      <button class="dropdown-btn" id="sigFilterBtn" type="button">\n'
        '        <span id="sigFilterLabel"></span><span class="dd-chev">▾</span>\n'
        "      </button>\n"
        '      <div class="dropdown-panel hidden" id="sigFilterPanel">\n'
        '        <div class="leg-controls">\n'
        '          <button class="leg-link" id="legAllBtn" type="button">Select all</button>\n'
        '          <button class="leg-link" id="legNoneBtn" type="button">Deselect all</button>\n'
        "        </div>\n"
        '        <div class="leg-row vertical" id="legRow"></div>\n'
        "      </div>\n"
        "    </div>\n"
        "  </div>\n"
        '  <div id="sigChart"></div>\n'
        "</div>\n"
        '<div class="card">\n'
        '  <div class="card-head"><div>\n'
        "    <h2>Brands</h2>\n"
        '    <div class="desc" id="brandsDesc">Click any row for its send calendar and signal mix.</div>\n'
        '    <div class="cat-filters hidden" id="catFilters">\n'
        '      <button class="cap" data-cat="never" type="button">Never turned on</button>\n'
        '      <button class="cap" data-cat="quiet" type="button">Off 15+ days</button>\n'
        "    </div>\n"
        "  </div><div id=\"rowCount\" style=\"font-size:12px;color:var(--text-muted);white-space:nowrap\"></div></div>\n"
        '  <div class="tbl-wrap"><table>\n'
        "    <thead><tr>\n"
        "      <th>Brand</th>\n"
        "      <th>External ID</th>\n"
        '      <th class="num sortable" data-key="sends">Sends<span class="arrow"> ↓</span></th>\n'
        '      <th class="num sortable" data-key="received">Received'
        '<span class="help" id="receivedHelp">i</span><span class="arrow"></span></th>\n'
        '      <th class="num sortable" data-key="ctr">CTR<span class="arrow"></span></th>\n'
        '      <th class="num sortable" data-key="topShare">Repeats'
        '<span class="help" id="repeatsHelp">i</span><span class="arrow"></span></th>\n'
        '      <th class="sig-col">Top signals</th>\n'
        "    </tr></thead>\n"
        '    <tbody id="tbody"></tbody>\n'
        "  </table></div>\n"
        '  <div class="pagin" id="pagin"></div>\n'
        "</div>\n"
        "<footer>\n"
        "  Generated by <code>scripts/build_dashboard.py</code> from <code>data/raw/*.json</code>. "
        "Re-run <code>scripts/fetch_dru.py</code> to refresh.<br>"
        "CTR is brand-level: one row = one brand-send, clicked = <code>converted &gt; 0</code>. "
        "“Other” groups signals outside the top 8 by volume "
        "(auto_campaign, campaign, orders_delta_up, orders_delta_down) to keep the chart's colors "
        "distinguishable — see the legend for the full breakdown.\n"
        "</footer>\n"
        "</div>\n"  # /#view-notif
        + FUNNEL_VIEW +
        "</div>\n"  # /.wrap
        '<div id="tip"></div>\n'
    )
    script = (
        "<script>const DATA=" + json.dumps(payload) + ";</script>\n"
        "<script>" + JS_LIB + "\n" + JS_APP + "\n" + JS_FUNNEL + "</script>\n"
        "</body></html>"
    )
    return head + script


def main():
    payload = load_all()
    with open(OUT, "w") as f:
        f.write(build(payload))
    print(
        f"wrote {OUT}  ({len(payload['dates'])} days, "
        f"{len(payload['notifs']):,} notifications, {len(payload['brands']):,} brands)"
    )


if __name__ == "__main__":
    main()
