#!/usr/bin/env python3
"""
dawnofthedusk.py — sunrise/sunset lookup (SQLite-backed, fast).

Usage:
    ./dawnofthedusk.py 'Calgary AB'
    ./dawnofthedusk.py 'calgary alberta'
    ./dawnofthedusk.py calgary
    ./dawnofthedusk.py 'Edmonton AB' 2026-11-17
    ./dawnofthedusk.py --help

Requires dawnofthedusk.db (see build_db.py).
Times are NRC local *standard* time (no DST is applied).
"""
import argparse
import re
import sqlite3
import sys
from datetime import date, datetime, timedelta

# ---------- date parsing -----------------------------------------------------

_MONTHS = {
    'jan': 1, 'january': 1, 'feb': 2, 'february': 2, 'mar': 3, 'march': 3,
    'apr': 4, 'april': 4, 'may': 5, 'jun': 6, 'june': 6, 'jul': 7, 'july': 7,
    'aug': 8, 'august': 8, 'sep': 9, 'sept': 9, 'september': 9,
    'oct': 10, 'october': 10, 'nov': 11, 'november': 11,
    'dec': 12, 'december': 12,
}

def parse_date(s):
    s = s.strip()
    try:
        from dateutil import parser as _dp  # type: ignore
        return _dp.parse(s, fuzzy=False).date()
    except Exception:
        pass
    for fmt in ('%Y-%m-%d', '%Y/%m/%d', '%d-%m-%Y', '%m/%d/%Y'):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    m = re.match(r'^([A-Za-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})$', s)
    if m and m.group(1).lower() in _MONTHS:
        return date(int(m.group(3)), _MONTHS[m.group(1).lower()], int(m.group(2)))
    m = re.match(r'^(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})$', s)
    if m and m.group(2).lower() in _MONTHS:
        return date(int(m.group(3)), _MONTHS[m.group(2).lower()], int(m.group(1)))
    today = date.today()
    low = s.lower()
    if low == 'today':     return today
    if low == 'tomorrow':  return today + timedelta(days=1)
    if low == 'yesterday': return today - timedelta(days=1)
    raise ValueError(f"unrecognized date: {s!r}")

# ---------- province aliases -------------------------------------------------

_PROVINCE_ALIASES = {
    'ALBERTA': 'AB',
    'BRITISH COLUMBIA': 'BC', 'BC': 'BC',
    'MANITOBA': 'MB',
    'NEW BRUNSWICK': 'NB',
    'NEWFOUNDLAND AND LABRADOR': 'NL', 'NEWFOUNDLAND': 'NL', 'LABRADOR': 'NL',
    'NOVA SCOTIA': 'NS',
    'NORTHWEST TERRITORIES': 'NT',
    'NUNAVUT': 'NU',
    'ONTARIO': 'ON',
    'PRINCE EDWARD ISLAND': 'PE', 'PEI': 'PE',
    'QUEBEC': 'QC', 'QUÉBEC': 'QC',
    'SASKATCHEWAN': 'SK',
    'YUKON': 'YT',
}

def normalize_province(s):
    up = s.strip().upper()
    for alias in sorted(_PROVINCE_ALIASES, key=len, reverse=True):
        if up == alias:
            return _PROVINCE_ALIASES[alias]
        if up.endswith(' ' + alias):
            head = up[:-len(alias)].rstrip()
            return f"{head} {_PROVINCE_ALIASES[alias]}"
    return up

# ---------- site matching ----------------------------------------------------

_norm_ws = re.compile(r'\s+')
def norm(s):
    return _norm_ws.sub(' ', (s or '').strip().upper())

def find_site(user_text, site_names):
    u = norm(normalize_province(user_text))
    if u in site_names:
        return u, None
    candidates = [s for s in site_names if s in u]
    if candidates:
        return max(candidates, key=len), None
    candidates = [s for s in site_names if u in s]
    if len(candidates) == 1:
        return candidates[0], None
    if candidates:
        return None, sorted(candidates)
    return None, None

# ---------- main -------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        prog='dawnofthedusk.py',
        description='Look up sunrise/sunset for a city on a given date.',
        epilog="""examples:
  ./dawnofthedusk.py 'Calgary AB'
  ./dawnofthedusk.py 'calgary alberta'
  ./dawnofthedusk.py calgary
  ./dawnofthedusk.py 'Edmonton AB' 2026-11-17
  ./dawnofthedusk.py calgary --short
""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument('args', nargs='+', metavar='CITY... [DATE]',
                    help='city (optionally with province), then optional date')
    ap.add_argument('--db', default='dawnofthedusk.db')
    ap.add_argument('--short', action='store_true',
                    help="print 'CITY,CIVIL_START,CIVIL_END' in 12h (no AM/PM)")
    a = ap.parse_args()

    # Split trailing date from leading city
    tokens = a.args
    target_date = None
    city_str = None
    for cut in range(len(tokens), 0, -1):
        if cut >= len(tokens):
            continue
        tail = ' '.join(tokens[cut-1:])
        try:
            d = parse_date(tail)
            head = ' '.join(tokens[:cut-1])
            if head:
                target_date = d
                city_str = head
                break
        except Exception:
            continue
    if target_date is None:
        city_str = ' '.join(tokens)
        target_date = date.today()

    con = sqlite3.connect(a.db)
    con.row_factory = sqlite3.Row

    site_names = {row[0] for row in con.execute("SELECT site FROM sites")}

    site_key, ambiguity = find_site(city_str, site_names)
    if ambiguity:
        sys.exit(f"error: {city_str!r} is ambiguous. Matches: " + ', '.join(ambiguity))
    if site_key is None:
        sys.exit(f"error: no site matched {city_str!r}")

    iso = target_date.isoformat()
    rows = con.execute(
        "SELECT event, time FROM sun WHERE site=? AND date_iso=?",
        (site_key, iso),
    ).fetchall()

    if not rows:
        rng = con.execute(
            "SELECT MIN(date_iso), MAX(date_iso) FROM sun WHERE site=?",
            (site_key,),
        ).fetchone()
        sys.exit(f"error: no data for {site_key} on {iso}. "
                 f"Available: {rng[0]} .. {rng[1]}.")
    events = {r['event']: r['time'] for r in rows}

    # ---- short mode: City,TwilightStart,TwilightEnd ----
    if a.short:
        dawn = events.get('Civil Twilight Start')
        dusk = events.get('Civil Twilight End')
        if not dawn or not dusk:
            sys.exit(f"error: civil twilight times missing for {site_key} on {iso}")

        def to_12h(hm):
            h, m = map(int, hm.split(':'))
            suffix = 'AM' if h < 12 else 'PM'
            h12 = h % 12 or 12
            return f"{h12:02d}:{m:02d}"
            # (Using %I:%M equivalent: zero-padded 12-hour)

        print(f"{site_key},{to_12h(dawn)},{to_12h(dusk)}")
        return

    # ---- full mode ----
    loc = con.execute(
        "SELECT name, longitude, latitude, timezone, utc_offset, elevation "
        "FROM locations WHERE name_key=?",
        (site_key,),
    ).fetchone()

    print(f"\n{site_key}  —  {target_date.strftime('%a, %d %b %Y')}")
    if loc:
        print(f"  lat {loc['latitude']}  lon {loc['longitude']}  "
              f"elev {loc['elevation']} m  "
              f"({loc['timezone'].strip()}, UTC{loc['utc_offset']}, no DST)")
    print()
    order = [
        'Nautical Twilight Start', 'Civil Twilight Start',
        'Sun rise', 'Local noon',
        'Sun set', 'Civil Twilight End', 'Nautical Twilight End',
    ]
    for ev in order:
        if ev in events:
            print(f"  {ev:<26} {events[ev]}")
    for ev in sorted(k for k in events if k.startswith('Hours of illumination')):
        print(f"  {ev:<26} {events[ev]}")
    print()

if __name__ == '__main__':
    main()
