# DRU Analytics — session handoff

Written 2026-08-19 to resume this work in a new session. Paste/open this file
first; it has the context a fresh session won't have. Supersedes the
2026-08-18 handoff below in spirit — that session built the dashboard UI,
this one built brand-name resolution. Read `README.md` for the general
project shape; this file is about what's in-flight.

## ⚠ Current on-disk state is two different snapshots — read before doing anything

`data/brand_names.json` and `dashboard.html` are **out of sync right now**:

- `data/brand_names.json` on disk: **1,354/1,619 named (83.6%)** — this is
  from the *last* `fetch_brands.py` run this session, done to verify step 5
  degrades gracefully with no `data/amplitude_emails.json` present (it does).
- `dashboard.html`'s embedded data: **1,611/1,619 named (99.5%)**,
  `generated: 2026-08-19 09:54` — this is from an *earlier* run this session,
  back when step 5 still read the sibling repo's folder directly (since
  replaced). It was deliberately **not rebuilt** after the 83.6% run, so the
  live dashboard doesn't regress just from that test.

Net effect: what's committed to `brand_names.json` right now is *worse* than
what the dashboard currently shows. First real action next session should be
step 1–3 under "Next steps" below (get Amplitude running for real), which
will regenerate `brand_names.json` at 99.5%+ and make the two consistent
again — don't rebuild the dashboard from the current `brand_names.json` as-is,
it would visibly remove 257 brand names.

## What this session did

Started from: the brands table showed raw `external_id`s only, no names.
Built a 5-step pipeline (below) across three independent data sources that
gets to **1,611/1,619 (99.5%) named, 1,619/1,619 (100%) emailed** once fully
wired up — see the on-disk-state warning just above for why that's not
what's sitting in `data/brand_names.json` at this exact moment. Full chain,
in order, each one only filling gaps the previous steps left:

```
scripts/fetch_dru.py        OneSignal -> data/raw/*.json          (unchanged this session)
scripts/fetch_missing_emails.py   OneSignal (per-id lookup) -> data/brand_emails.json   [NEW]
scripts/fetch_amplitude.py        Amplitude Export API -> data/amplitude_emails.json    [NEW, not yet run for real]
scripts/fetch_brands.py           ops Sheet + brand_emails.json + amplitude_emails.json -> data/brand_names.json   [CHANGED]
scripts/build_dashboard.py        data/raw/*.json + data/brand_names.json -> dashboard.html   [unchanged this session]
```

### `fetch_brands.py`'s 5 steps (in `main()`, in order)

1. Ops Google Sheet, "MySQL Import" **contact tab** (`contact_id | email |
   name`) — direct match on `external_id`. ~83% alone.
2. Same sheet, **brand tab** (`contact_reelo_ids` is a comma-separated list
   per brand) — a few more.
3. Same contact tab again, matched by **email** instead of id (a brand's
   other team members share a contact row's email) — using whatever's in
   `data/brand_emails.json` at this point.
4. **Free gap-fill**: any email in `data/brand_emails.json` not yet used gets
   attached even with no name, so nothing with a known email shows as fully
   blank.
5. **Amplitude** (`data/amplitude_emails.json`, see below) — last shot at a
   name by email, only when that email maps to exactly one brand there.

### What's new and why

- **`scripts/fetch_missing_emails.py`** — for `external_id`s the sheet has
  nothing on, hits OneSignal directly (`GET
  /apps/{app_id}/users/by/external_id/{id}`) and reads the `Email`-type
  subscription's `token` off the user record. Tested against all 270 ids that
  were fully unresolved at the start of this session: **270/270 hit rate**.
  Already run for real — `data/brand_emails.json` has all 270.
- **`scripts/fetch_amplitude.py`** — mirrors
  `../reelo-habitual-users-dru-main/fetch.py`'s aggregation (Amplitude Export
  API, growth/starter-plan events, grouped by the `group_name` user
  property = the brand's in-app display name), but keeps only `email ->
  group_name` identity, and writes it into *this* repo
  (`data/amplitude_emails.json`) instead of depending on that sibling repo's
  checked-out files. **Written but never run with real credentials** — see
  "Next steps" below. When tested manually against the sibling repo's
  existing 155 days of data (a one-off check, not code), it resolved **256 of
  264** then-remaining nameless brands, so once this is live the numbers
  above should hold or improve.
- **`fetch_brands.py` step 5 rewritten twice this session**: first to read
  `../reelo-habitual-users-dru-main/data/*.json` directly (worked locally,
  257 matches), then — once we realized that folder won't exist in a bare
  CI checkout — rewritten again to read `data/amplitude_emails.json` instead
  (see `fetch_amplitude.py` above). The current code is the second version;
  it degrades gracefully (logs a skip, doesn't crash) when that file is
  absent, which is the state right now.

### Dead ends this session ruled out

- **SuccessOS** (Reelo's internal CRM, `reelo.successos.app` — CLI at
  `~/go/bin/successos`, spec at `~/printing-press/successos/successos.yaml`)
  was the first idea for a live, complete brand source. Blocked: the only
  login tried (`barkhap` / Barkha Puwar) has `invitationStatus: "pending"`
  with an `inviteExpiresAt` that already passed a month ago — no real
  password was ever set, so `successos login` returns a 200 with account
  *lookup* data but never actually authenticates
  (`GET /api/user` still 401s with `hasPassport: false` after login). Needs
  either that invite renewed and completed, or a different already-active
  SuccessOS account, before this route is worth revisiting.
- **`../reelo-habitual-users-dru-main/growth_brand_sheet_data/`** (a second,
  separate public Google Sheet that project fetches) was tried against the
  265 emails-but-no-name brands: **1 match** (Fountain Hospitality). Not
  worth wiring into the pipeline — mentioned here only so a future session
  doesn't re-try it expecting more.
- Amplitude does **not** carry OneSignal's `external_id` anywhere. Checked
  `entityId` (also 24-char hex, tracked on `DailyRoundupStoryView`) against
  all 1,619 real `external_id`s — zero matches. It's the same brand/account
  level "MySQL Entity ID" the sheet's brand tab already has, not the
  contact-level id OneSignal uses. Amplitude's only usable identity fields
  are `email_id` and `group_name` — the email-bridge approach `fetch_amplitude.py`
  uses is the ceiling here, not a limitation of that one check.

### Also cleaned up

- Deleted `dru-analytics/dru-analytics/` — an accidental self-nested stale
  duplicate (no `.git` anywhere, so not a real clone/submodule; just an old
  copy with matching file contents but earlier timestamps, missing
  `fetch_brands.py` entirely). Confirmed nothing in it wasn't already in the
  outer copy before deleting.

## Next steps, in order

1. **Add Amplitude credentials.** Put `AMPLITUDE_API_KEY` and
   `AMPLITUDE_SECRET_KEY` in the local `.env` (already gitignored). Get them
   from Amplitude project settings — the sibling repo's GitHub Actions
   secrets already have a working pair for the same project, but secrets
   can't be read back once set, so pull fresh values from Amplitude itself
   (Settings → Projects → General) rather than trying to recover the
   existing ones.
2. **Run the backfill.** `data/amplitude_emails.json` doesn't exist yet, so
   the first run should cover much more than the daily default:
   ```
   set -a && source .env && set +a
   python3 scripts/fetch_amplitude.py --days 60
   ```
   Sanity check the output (`generated_at`, `emails` count, and how many map
   to exactly one brand vs. are ambiguous — the script logs both) before
   trusting it.
3. **Re-run brand resolution and rebuild:**
   ```
   python3 scripts/fetch_brands.py
   python3 scripts/build_dashboard.py
   ```
   Expect step 5's log line (`+N from Amplitude`) to be nonzero now. Compare
   the final `named X/1619` line against this session's 1,611/1,619 baseline
   — should land at or above it. Open `dashboard.html` in a browser and spot
   check a few previously-nameless brands actually show a name now.
4. **`.github/workflows/fetch.yml` needs real changes, not just new
   secrets** — it currently only runs `fetch_dru.py` then
   `build_dashboard.py`. It has **never called `fetch_brands.py` at all**,
   so as committed today, brand names would never refresh in CI regardless
   of Amplitude — they'd just ride on whatever `data/brand_names.json`
   happens to be checked into the repo at push time. Decide: add
   `fetch_missing_emails.py` + `fetch_amplitude.py` + `fetch_brands.py` as
   steps before "Rebuild dashboard" (daily-fresh names, more OneSignal/Amplitude
   API calls every run), or run brand resolution manually/less often and
   just let CI rebuild the dashboard from whatever's committed (cheaper, but
   names go stale between manual runs). Either way it'll need
   `AMPLITUDE_API_KEY`/`AMPLITUDE_SECRET_KEY` added as repo secrets alongside
   the existing `ONESIGNAL_APP_ID`/`ONESIGNAL_API_KEY`.
5. **This folder still isn't a git repo** (carried over from last session,
   still true): `git init`, push to a **private** GitHub repo (`data/raw/`
   has brand `external_id`s and now real emails — this must not be public),
   add all four secrets above, then trigger the workflow once manually from
   the Actions tab before trusting the cron.
6. **The remaining ~8 unresolved brands** (post-Amplitude, was 8 before this
   session's Amplitude work was tested manually) are likely close to the
   floor — 2 are internal `@reelo.io` staff, not real brands. Check
   `data/unresolved_external_ids.txt` after step 3 above for the current
   list; probably fine to hand-resolve the handful that remain rather than
   chase a 6th data source.

## Stray files still worth a decision before the first commit

`.DS_Store`, `settings.local (1).json` (local Claude Code permissions file,
not project config) — carried over from last session, still unaddressed.
`data/unresolved_external_ids.txt` is a scratch/reference file from this
session's investigation, regenerated each time brand resolution is re-run —
fine to keep or gitignore, not load-bearing for the pipeline itself.

---

# Previous handoff (2026-08-18) — dashboard UI rewrite

Kept for reference; the pipeline diagram and "not done yet" list above are
more current than the corresponding sections below.

## What this project is

`dru-analytics` measures **Daily Round-up push notification performance**
(sends → clicks) by reading directly from **OneSignal**. Each DRU push is its
own single-recipient notification, so every record carries `external_id`
(the brand), `template_id` → signal + tone, and `converted` (did they click).

## What changed that session

`scripts/build_dashboard.py` was **fully rewritten**. The old version only
embedded day-level rollups and rendered six mostly-independent charts (sends/day,
CTR/day, top signals, CTR-by-signal, top templates, tone mix). The new version
embeds every per-notification record (compact-encoded) and renders a single
connected view driven by one period filter:

1. **Filter bar** — This week / This month / Custom range / Single date.
2. **KPI strip** — sends, brands reached, clicks, CTR for the selected period.
3. **"Top signals — ranked"** — horizontal bar chart, all 12 real signals.
4. **"Top signals — daily trend"** — multi-line chart with a checkbox legend
   that also filters the brand table below.
5. **Brands table** — External ID, Sends, CTR, Repeats, top-3 signal badges.
   Sortable, searchable, paginated. (Brand *names* were not resolved yet as
   of this handoff — that's the work the 2026-08-19 session above did.)
6. **Row → accordion** — per-signal dot grid + signal-mix pie.

### Design decisions worth not re-litigating

- **12 real signals, 8 safe categorical colors.** `SIGNAL_COLOR_ORDER` in
  `build_dashboard.py` pins the top 8 by volume to the 8 validated palette
  hues; the remaining 4 fold into "Other" **in the trend line chart only**.
  The ranked bar chart and per-brand dot grid keep all 12 distinct — they
  carry identity via position + label, not a shared hue budget.
- **Duplicate same-day sends are real data, not a bug.** 66.7% of brand-days
  get exactly 1 send, 32.3% get exactly 2; the dot grid tints multi-signal
  days amber rather than hiding the anomaly.
- **SVG sizing gotcha:** the global `svg{width:100%;height:auto}` rule
  stretches every chart's SVG to its container's width.
  `multiLineChart`/`hbarChart` compute `viewBox` from `clientWidth` so this
  is a no-op; `dotCalendar` needs an explicit inline
  `style="width:${W}px;max-width:none;height:${H}px"` or it blows up ~3x.

### Verification approach (no browser tool in that session)

Headless jsdom smoke tests (`node your_test.js` with `runScripts:'dangerously'`)
covering: every period preset incl. reversed custom range, toggling all
signal checkboxes off, accordion expand, sort/search, theme toggle, KPI
cross-check against a plain Python recount, `node --check` on extracted
`<script>` blocks. `window.matchMedia` needs a polyfill in jsdom.

## File map (`scripts/build_dashboard.py`)

| Lines | What |
|---|---|
| 30–55 | `SIGNAL_COLOR_ORDER`, `SIGNAL_LABELS` |
| 57–101 | `load_all()` |
| 104–261 | `CSS` |
| 262–520 | `JS_LIB` — chart primitives |
| 521–861 | `JS_APP` — filtering, `render()`, table/accordion |
| 863–953 | `build()` |
| 954+ | `main()` |

(Line numbers will have shifted somewhat after this session's changes to
`load_brand_names()` — re-check before quoting them.)

## Data payload shape (embedded in `dashboard.html`)

```js
DATA = {
  dates: [...], brands: [...], signals: [...], signalColorOrder: [...],
  signalLabels: {...}, brandNames: {...}, brandEmails: {...},
  notifs: [[dateIdx, brandIdx, signalIdx, clicked01], ...],
  generated: "YYYY-MM-DD HH:MM", unstable: [...],
}
```

`notifs` is currently ~40k rows. Scales to roughly 10MB/year at current
volume if the project runs a full year without pruning.
