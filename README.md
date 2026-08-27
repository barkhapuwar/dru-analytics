# DRU Analytics

Daily Round-up push notification analytics, pulled from OneSignal.

## Quick start

```bash
export ONESIGNAL_APP_ID=...
export ONESIGNAL_API_KEY=...

python3 scripts/fetch_dru.py --days 7     # ~2 min per day
python3 scripts/build_dashboard.py        # writes dashboard.html
open dashboard.html
```

`dashboard.html` is fully self-contained — the data is embedded in the file. It
holds no API key, needs no server, and works offline.

## Why a script instead of an HTML page that calls OneSignal

A page that fetched the API directly would have to ship the REST key in its
source, and that key can send push to every brand and delete users. It would also
spend ~2 minutes per day of data on rate-limited pagination on every open. And
Chrome blocks `file://` pages from reading sibling files, so the data has to be
embedded regardless.

## What the OneSignal data looks like

Each DRU send is its own single-recipient notification, which is what makes any of
this measurable:

| field | meaning |
|---|---|
| `include_aliases.external_id` | the brand it went to |
| `template_id` | signal + tone (e.g. `prod_dru_growth_driver_v1`) |
| `queued_at` | send time |
| `successful` | device subscriptions reached (**not** brands) |
| `received` | confirmed deliveries |
| `converted` | clicks |

## Gotchas the fetcher handles

- **`limit` is capped at 50** server-side, whatever you ask for.
- **`offset` is ignored when `time_offset` is set.** Paging is done by advancing a
  timestamp cursor and de-duplicating on notification `id`. Plain `offset` paging
  also degrades badly on old data (~8s at offset 45,000); the cursor does not.
- **HTTP 429** comes back with `retry-after` (~52s) and no `ratelimit-*` headers,
  so there is nothing to pace against pre-emptively — the client backs off on the
  response.
- **Template names carry stray trailing spaces** (`prod_dru_aov_v0 `), so signal
  and tone are keyed off `template_id`, not the name.
- **Non-prod `dru_*` templates still emit a trickle of sends** and are excluded
  deliberately via `DRU_PREFIX`.

## Click rate is brand-level

One row = one brand-send; `clicked = converted > 0`. This matters: `successful`
counts *devices*, and about a quarter of brands have two or more. The same clicks
give 7.53% per brand, 7.11% per confirmed delivery, or 5.66% per device. The
dashboard uses the brand-level figure throughout.

## Days settle over time

Clicks keep arriving for hours after a send, so a freshly-fetched day always
undercounts. Each file carries `fetched_at` and `stable`; days newer than
`--stable-after` (default 4) are flagged in the dashboard. The scheduled job
re-fetches a rolling 5-day window and overwrites, so recent days converge.

## Growth → DRU adoption funnel

The dashboard has a second tab (pill nav, top of the page) tracking how far
Growth businesses get on the path **have the app → notifications on → received a
DRU → tapped it → opened the DRU screen**. `scripts/fetch_funnel.py` writes one
snapshot per run to `data/funnel/YYYY-MM-DD.json`, stitched from three sources
that don't share an identity key yet:

| stage | source |
|---|---|
| Growth businesses | ops Google Sheet, brand tab (live roster) |
| Have the app / Opened DRU | Amplitude segmentation API (`plan = growth`) |
| Notifications enabled | OneSignal subscription CSV export |
| Received / Tapped a DRU | `data/raw/*.json` (already fetched) |

Rolling 30-day window. Stages 1–5 count **businesses**; "Opened the DRU screen"
comes from Amplitude and counts **app logins** (~1.0–1.3 per business), so that
ratio is approximate — the in-dashboard notes say which is which. **No backfill**
for the top of the funnel: the sheet and OneSignal only expose current state, so
that history starts the day the daily job first runs. Needs `AMPLITUDE_API_KEY`,
`AMPLITUDE_SECRET_KEY`, `ONESIGNAL_APP_ID`, `ONESIGNAL_API_KEY` in the env.

## Layout

```
scripts/fetch_dru.py        OneSignal -> data/raw + data/agg
scripts/fetch_funnel.py     sheet + Amplitude + OneSignal -> data/funnel/YYYY-MM-DD.json
scripts/build_dashboard.py  data/agg + data/funnel -> dashboard.html
data/templates.json         pinned template_id -> (signal, tone)
data/raw/YYYY-MM-DD.json    per-notification records
data/agg/YYYY-MM-DD.json    per-day aggregates
data/funnel/YYYY-MM-DD.json per-day Growth -> DRU funnel snapshot
data/summary.json           rolling rollup
```

## Before enabling the scheduled workflow

Add `ONESIGNAL_APP_ID` and `ONESIGNAL_API_KEY` as repository secrets, then run
the workflow manually once from the Actions tab.

Keep this repository **private** — `data/raw/` contains `external_id` per brand.
Note that GitHub Pages will not serve a private repo on the free plan; open
`dashboard.html` locally instead.
