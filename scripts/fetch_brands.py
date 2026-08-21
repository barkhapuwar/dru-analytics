#!/usr/bin/env python3
"""
Resolve OneSignal external_ids to brand names + emails, from the ops Google Sheet.

external_id is a CONTACT id (a person at a brand), not a brand id — it does not
match the sheet's "MySQL Entity ID"/"Customer ID" columns at all. Two tabs do
carry it, and both are joined here, best first:

  1. "MySQL Import" contact tab  — contact_id | email | name   (one row per
     contact; matches 82.9% of live external_ids on its own)
  2. "MySQL Import" brand tab    — reelo_id | name | email | contact_reelo_ids
     (contact_reelo_ids is a comma-separated list; fills a few more)

Together they resolve ~83%. For the rest, scripts/fetch_missing_emails.py hits
OneSignal directly (GET /users/by/external_id/{id}) to recover an email even
when the sheet has nothing on that contact - that script is the one that needs
an API key, not this one. Whatever it finds lands in data/brand_emails.json,
which this script reads for two things:

  3. an email-only match back into the contact tab (a brand's team members
     share a contact row's email even when their own id isn't in the sheet)
  4. filling the "email" field with no further lookup, for whatever's left

Finally, a brand's team members' emails also turn up directly in Amplitude,
tagged with the group_name the product itself shows them - scripts/
fetch_amplitude.py fetches that (Amplitude's own API, not a sibling repo's
checked-out files, so this keeps working from a bare checkout in CI) into
data/amplitude_emails.json. So:

  5. any still-nameless id gets one more shot: its email against that
     email -> group_name map, only when the email maps to exactly one brand
     there (some staff emails span dozens and are ambiguous by nature, so
     those are left alone rather than guessed)

Writes data/brand_names.json, which build_dashboard.py picks up automatically.
"""

import argparse
import csv
import datetime
import glob
import io
import json
import os
import sys
import urllib.request

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW_DIR = os.path.join(ROOT, "data", "raw")
EMAIL_CACHE = os.path.join(ROOT, "data", "brand_emails.json")
AMPLITUDE_EMAILS = os.path.join(ROOT, "data", "amplitude_emails.json")
OUT = os.path.join(ROOT, "data", "brand_names.json")

DEFAULT_SHEET_ID = "11IVtAw8CemAi1C9zP4rdonnmBVWf11Rc1-HVbFACDUM"
CONTACT_GID = "1045215489"   # contact_id | email | name
BRAND_GID = "1306646119"     # reelo_id | name | email | contact_reelo_ids


def log(msg):
    print(f"[{datetime.datetime.now(IST):%H:%M:%S}] {msg}", flush=True)


def external_ids():
    ids = set()
    for f in sorted(glob.glob(os.path.join(RAW_DIR, "*.json"))):
        for n in json.load(open(f)).get("notifications", []):
            if n.get("external_id"):
                ids.add(n["external_id"])
    return ids


def brand_tab_contact_ids(brand_rows):
    """Every contact_reelo_id across every brand-tab row - the full roster of
    real (growth-plan) brands Reelo has on file, not just ones a DRU send has
    ever gone out to. A brand whose push notifications are off from day one
    never gets a send queued at all (see fetch_missing_emails.py's docstring),
    so it's invisible to external_ids() above no matter how long we wait -
    this is the only way to know it exists."""
    ids = set()
    for r in brand_rows:
        for cid in (r.get("contact_reelo_ids") or "").split(","):
            cid = cid.strip()
            if cid:
                ids.add(cid)
    return ids


def fetch_tab(sheet_id, gid, want):
    """Rows of one tab as dicts. These tabs open with a banner row ("MySQL
    Import / Last updated ..."), so the header row is found by content rather
    than assumed to be row 0."""
    url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}"
    with urllib.request.urlopen(url, timeout=120) as resp:
        text = resp.read().decode("utf-8", "replace")
    rows = list(csv.reader(io.StringIO(text)))
    hdr_i = next((i for i, r in enumerate(rows[:5]) if all(w in r for w in want)), None)
    if hdr_i is None:
        sys.exit(f"gid {gid}: no header row containing {want} in the first 5 rows "
                 f"(tab layout changed?); saw {rows[:2]}")
    hdr = rows[hdr_i]
    out = [dict(zip(hdr, r)) for r in rows[hdr_i + 1:] if len(r) >= len(want)]
    log(f"  gid {gid}: {len(out)} rows, header on line {hdr_i + 1}")
    return out


def write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sheet-id", default=os.environ.get("BRAND_SHEET_ID", DEFAULT_SHEET_ID))
    ap.add_argument("--contact-gid", default=os.environ.get("BRAND_CONTACT_GID", CONTACT_GID))
    ap.add_argument("--brand-gid", default=os.environ.get("BRAND_BRAND_GID", BRAND_GID))
    args = ap.parse_args()

    raw_ids = external_ids()
    log(f"{len(raw_ids)} external_ids in data/raw/ (ever sent a DRU)")
    log("reading sheet")

    resolved = {}  # ext -> {"name","email"}

    # 1. contact tab — one row per contact, the most direct match
    contact_rows = fetch_tab(args.sheet_id, args.contact_gid,
                             ["contact_id", "email", "name"])

    # 2. brand tab — contact_reelo_ids is a comma-separated list per brand.
    #    Fetched before the id universe is finalized: it's also the source of
    #    every growth-plan brand's contact ids, sent a DRU before or not - a
    #    brand with notifications off from day one never gets a send queued,
    #    so it would otherwise never appear anywhere in this pipeline.
    brand_rows = fetch_tab(args.sheet_id, args.brand_gid,
                           ["reelo_id", "name", "email", "contact_reelo_ids"])
    roster_ids = brand_tab_contact_ids(brand_rows)
    ids = raw_ids | roster_ids
    log(f"  +{len(ids) - len(raw_ids)} more from the brand tab's full roster "
        f"(never sent a DRU) — {len(ids)} total")

    for r in contact_rows:
        cid = (r.get("contact_id") or "").strip()
        if cid in ids and cid not in resolved:
            resolved[cid] = {"name": (r.get("name") or "").strip(),
                             "email": (r.get("email") or "").strip().lower()}
    log(f"  matched {len(resolved)} from the contact tab")

    before = len(resolved)
    for r in brand_rows:
        cids = [c.strip() for c in (r.get("contact_reelo_ids") or "").split(",") if c.strip()]
        for cid in cids:
            if cid in ids and cid not in resolved:
                resolved[cid] = {"name": (r.get("name") or "").strip(),
                                 "email": (r.get("email") or "").strip().lower()}
    log(f"  +{len(resolved) - before} more from the brand tab")

    # 3. same tab, matched by EMAIL instead of id. external_id belongs to a
    #    person, and a brand's team members each get their own contact row, so
    #    an id we can't find may still share an email with a row we can. Worth
    #    ~5 brands; costs nothing since the tab is already in memory.
    by_email = {}
    for r in contact_rows:
        em = (r.get("email") or "").strip().lower()
        if em and (r.get("name") or "").strip():
            by_email.setdefault(em, (r["name"].strip(), em))
    cached = json.load(open(EMAIL_CACHE)) if os.path.exists(EMAIL_CACHE) else {}
    before = len(resolved)
    for ext in ids:
        if ext in resolved and resolved[ext]["name"]:
            continue
        em = (cached.get(ext) or "").strip().lower()
        if em and em in by_email:
            name, _ = by_email[em]
            resolved[ext] = {"name": name, "email": em}
    log(f"  +{len(resolved) - before} more by email within the contact tab")

    # 4. free gap-fill: emails a previous OneSignal run already cached
    filled = 0
    if cached:
        for ext in ids:
            email = cached.get(ext)
            if not email:
                continue
            if ext in resolved:
                if not resolved[ext]["email"]:
                    resolved[ext]["email"] = email
                    filled += 1
            else:
                resolved[ext] = {"name": "", "email": email}
                filled += 1
        log(f"  +{filled} emails from the local cache (no API calls)")

    # 5. Amplitude — one more shot at a name, by email, for whatever's still
    #    nameless. data/amplitude_emails.json (written by fetch_amplitude.py)
    #    maps every email Amplitude has seen acting for a brand to that
    #    brand's group_name - the label the product itself shows. Only used
    #    when an email maps to exactly one brand; some staff emails span
    #    dozens and are ambiguous by nature, so those are left alone rather
    #    than guessed.
    if os.path.exists(AMPLITUDE_EMAILS):
        amp = json.load(open(AMPLITUDE_EMAILS)).get("emails", {})
        unique = {em: names[0] for em, names in amp.items() if len(names) == 1}
        log(f"  amplitude: {len(amp)} emails cached "
            f"({len(amp) - len(unique)} ambiguous, skipped)")

        amp_used = 0
        for ext in ids:
            if resolved.get(ext, {}).get("name"):
                continue
            em = resolved.get(ext, {}).get("email") or cached.get(ext) or ""
            em = em.strip().lower()
            if em and em in unique:
                resolved[ext] = {"name": unique[em], "email": em}
                amp_used += 1
        log(f"  +{amp_used} from Amplitude")
    else:
        log(f"  {AMPLITUDE_EMAILS} not found — run scripts/fetch_amplitude.py "
            f"first to enable this source — skipping")

    write_json(OUT, resolved)
    named = sum(1 for v in resolved.values() if v["name"])
    mailed = sum(1 for v in resolved.values() if v["email"])
    log(f"wrote {OUT}")
    log(f"  named  {named}/{len(ids)} ({named/len(ids)*100:.1f}%)")
    log(f"  emails {mailed}/{len(ids)} ({mailed/len(ids)*100:.1f}%)")
    log(f"  unresolved {len(ids)-named}")


if __name__ == "__main__":
    main()
