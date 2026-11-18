#!/usr/bin/env python3
"""
dawnofthedusk.py — sunrise/sunset lookup (SQLite-backed, fast).

Usage:
    ./dawnofthedusk.py                     # guess city from public IP
    ./dawnofthedusk.py 'Calgary AB'
    ./dawnofthedusk.py 'calgary alberta'
    ./dawnofthedusk.py calgary
    ./dawnofthedusk.py 'Edmonton AB' 2026-11-17
    ./dawnofthedusk.py --help

Requires dawnofthedusk.db (see build_db.py) — resolved relative to this script.
Times stored in the DB are NRC local *standard* time (no DST).
"""
import argparse
import json
import math
import os
import re
import sqlite3
import sys
import unicodedata
import urllib.request
from datetime import date, datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# ---------- paths ------------------------------------------------------------

_SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
_DEFAULT_DB = os.path.join(_SCRIPT_DIR, 'dawnofthedusk.db')

# ---------- geoip fallback ---------------------------------------------------

GEOIP_URL = "https://ipinfo.io/json"
FALLBACK_CITY = "CALGARY AB"
FALLBACK_TZ = "America/Edmonton"

PROVINCES = {
    "alberta": "AB", "british columbia": "BC", "manitoba": "MB",
    "new brunswick": "NB", "newfoundland and labrador": "NL",
    "nova scotia": "NS", "northwest territories": "NT", "nunavut": "NU",
    "ontario": "ON", "prince edward island": "PE", "quebec": "QC",
    "saskatchewan": "SK", "yukon": "YT",
}

# Permanent-time-override zones (post-2026-03-08); if the system tzdata
# isn't aware yet, we force the offset here. Only used for display/epoch.
PERMANENT_SINCE = date(2026, 3, 8)
PERMANENT_ZONES = {
    "America/Edmonton": ("ABT", -6),
    "America/Vancouver": ("PCT", -7),
    "America/Winnipeg": ("CDT", -5),
}

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

# ---------- name normalization ----------------------------------------------

_norm_ws = re.compile(r'\s+')

def norm(s):
    """Uppercase, strip accents, collapse whitespace."""
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return _norm_ws.sub(' ', s.strip().upper())

# ---------- site matching ----------------------------------------------------

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

# ---------- geoip -> site ----------------------------------------------------

def _http_json(url, timeout=10):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))

def guess_site_from_ip(site_names, locations):
    """
    Return (site_key, tz_name).

    `locations` maps site_key -> sqlite3.Row with lat/lon/timezone.
    Falls back to CALGARY AB / America/Edmonton on any failure.
    """
    try:
        info = _http_json(GEOIP_URL)
    except Exception:
        return FALLBACK_CITY, FALLBACK_TZ

    if info.get("country") != "CA":
        return FALLBACK_CITY, FALLBACK_TZ

    tz_name = info.get("timezone") or FALLBACK_TZ

    # 1. Exact name + province match.
    city = norm(info.get("city", ""))
    prov = PROVINCES.get((info.get("region") or "").lower(), "")
    candidate = f"{city} {prov}".strip()
    if candidate in site_names:
        return candidate, tz_name

    # 2. Nearest listed site by coordinates.
    try:
        lat, lon = map(float, info["loc"].split(","))
    except (KeyError, ValueError, TypeError):
        return FALLBACK_CITY, FALLBACK_TZ

    def dist(site):
        row = locations.get(site)
        if row is None or row["latitude"] is None or row["longitude"] is None:
            return math.inf
        # Equirectangular approximation, fine for nearest-neighbor.
        x = math.radians(row["longitude"] - lon) * math.cos(
            math.radians((row["latitude"] + lat) / 2)
        )
        y = math.radians(row["latitude"] - lat)
        return math.hypot(x, y)

    best = min(site_names, key=dist, default=None)
    if best is None or dist(best) is math.inf:
        return FALLBACK_CITY, FALLBACK_TZ
    return best, tz_name

# ---------- timezone helpers -------------------------------------------------

def resolve_tz(tz_name, day, std_offset_hours):
    """Return (tzinfo, abbreviation, utc_label)."""
    if tz_name in PERMANENT_ZONES and day >= PERMANENT_SINCE:
        abbr, hours = PERMANENT_ZONES[tz_name]
        tz = timezone(timedelta(hours=hours))
    else:
        try:
            zone = ZoneInfo(tz_name)
            # Compute the abbreviation at noon so DST state is correct.
            abbr = datetime(day.year, day.month, day.day, 12,
                            tzinfo=zone).strftime("%Z")
            tz = zone
        except (ZoneInfoNotFoundError, ValueError):
            tz = timezone(timedelta(hours=std_offset_hours))
            abbr = "LST"

    mins = int(tz.utcoffset(datetime(day.year, day.month, day.day, 12).replace(tzinfo=tz))
               .total_seconds() // 60)
    sign = "-" if mins < 0 else "+"
    h, m = divmod(abs(mins), 60)
    label = f"UTC{sign}{h}" + (f":{m:02d}" if m else "")
    return tz, abbr, label

def hm_to_dt(day, hm, std_offset_hours, tz):
    """Interpret 'H:MM' as city-standard-time on `day`, convert to `tz`."""
    h, m = map(int, hm.split(":"))
    src = timezone(timedelta(hours=std_offset_hours))
    return datetime(day.year, day.month, day.day, h, m, tzinfo=src).astimezone(tz)

# ---------- main -------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        prog='dawnofthedusk.py',
        description='Look up sunrise/sunset for a city on a given date. '
                    'With no city given, the city is guessed from your public IP.',
        epilog="""examples:
  ./dawnofthedusk.py                     # use IP geolocation
  ./dawnofthedusk.py 'Calgary AB'
  ./dawnofthedusk.py 'calgary alberta'
  ./dawnofthedusk.py calgary
  ./dawnofthedusk.py 'Edmonton AB' 2026-11-17
  ./dawnofthedusk.py calgary --short     # CITY,TZ,UTC±X,DAWN_EPOCH,DUSK_EPOCH
""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument('args', nargs='*', metavar='CITY... [DATE]',
                    help='city (optionally with province), then optional date; '
                         'if omitted, city is guessed from your IP')
    ap.add_argument('--db', default=_DEFAULT_DB,
                    help=f'path to SQLite DB (default: {_DEFAULT_DB})')
    ap.add_argument('--short', action='store_true',
                    help="print 'CITY,TZ,UTC_OFFSET,DAWN_EPOCH,DUSK_EPOCH'")
    a = ap.parse_args()

    con = sqlite3.connect(a.db)
    con.row_factory = sqlite3.Row

    site_names = {row[0] for row in con.execute("SELECT site FROM sites")}
    if not site_names:
        sys.exit(f"error: sites table is empty in {a.db}")

    # ---------- resolve city / date ----------
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
        # No trailing date -> all tokens are the city (may be empty).
        city_str = ' '.join(tokens) if tokens else None
        target_date = date.today()

    # Build a site->location map for nearest-neighbor matching.
    locations = {
        row["name_key"]: row
        for row in con.execute(
            "SELECT name_key, name, longitude, latitude, timezone, "
            "utc_offset, elevation FROM locations"
        )
    }

    ip_tz = None
    if city_str:
        site_key, ambiguity = find_site(city_str, site_names)
        if ambiguity:
            sys.exit(f"error: {city_str!r} is ambiguous. Matches: "
                     + ', '.join(ambiguity))
        if site_key is None:
            sys.exit(f"error: no site matched {city_str!r}")
    else:
        site_key, ip_tz = guess_site_from_ip(site_names, locations)

    # ---------- look up sun times ----------
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

    loc = locations.get(site_key)

    # Choose timezone: IP-provided if we guessed; else the city's standard tz.
    if ip_tz:
        tz_name = ip_tz
    elif loc and loc["timezone"]:
        tz_name = loc["timezone"].strip()
    else:
        tz_name = FALLBACK_TZ

    std_offset_hours = float(loc["utc_offset"]) if loc and loc["utc_offset"] is not None else -7.0
    # NRC stores "hours west of UTC" as an integer (e.g. -6 or 6?).
    # In this DB it's already in the format UTC+X, e.g. "UTC6" -> +6.
    # If your DB uses west-positive, adjust here. Current data shows "UTC6"
    # which we interpret as +6 (i.e. UTC-6 for Calgary).
    # See note below.

    tz, abbr, utc_label = resolve_tz(tz_name, target_date, std_offset_hours)

    # ---------- short mode ----------
    if a.short:
        dawn_hm = events.get('Civil Twilight Start')
        dusk_hm = events.get('Civil Twilight End')
        if not dawn_hm or not dusk_hm:
            sys.exit(f"error: civil twilight times missing for {site_key} on {iso}")
        dawn_dt = hm_to_dt(target_date, dawn_hm, std_offset_hours, tz)
        dusk_dt = hm_to_dt(target_date, dusk_hm, std_offset_hours, tz)
        print(f"{site_key},{abbr},{utc_label},"
              f"{int(dawn_dt.timestamp())},{int(dusk_dt.timestamp())}")
        return

    # ---------- full mode ----------
    print(f"\n{site_key}  —  {target_date.strftime('%a, %d %b %Y')}  ({abbr}, {utc_label})")
    if loc:
        print(f"  lat {loc['latitude']}  lon {loc['longitude']}  "
              f"elev {loc['elevation']} m")
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
