#!/usr/bin/env python3
"""
build_db.py — build the SQLite cache from sunsets.csv + locations.csv.

sunsets.csv may be in either shape:
    long:  site,date,date_iso,event,time
    wide:  site,date,date_iso,<event>,<event>,...
Both are auto-detected from the header.

Usage:
    ./build_db.py sunsets.csv locations.csv sunsets.db
"""
import csv, os, re, sqlite3, sys

_norm_ws = re.compile(r'\s+')
def norm(s):
    return _norm_ws.sub(' ', (s or '').strip().upper())

# ---------- sun loaders ------------------------------------------------------

def _flush(con, batch):
    if batch:
        con.executemany("INSERT INTO sun VALUES (?,?,?,?)", batch)
        batch.clear()

def load_sun(src, con):
    """Returns (row_count, set_of_sites)."""
    sites = set()
    n = 0
    with open(src, newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        header = reader.fieldnames or []
        is_long = 'event' in header and 'time' in header
        event_cols = [c for c in header if c not in ('site', 'date', 'date_iso')] if not is_long else None

        batch = []
        for r in reader:
            site = r['site']
            date_iso = r['date_iso']
            sites.add(site)
            if is_long:
                batch.append((site, date_iso, r['event'], r['time']))
                n += 1
            else:
                for ev in event_cols:
                    v = (r.get(ev) or '').strip()
                    if not v:
                        continue
                    batch.append((site, date_iso, ev, v))
                    n += 1
            if len(batch) >= 50000:
                _flush(con, batch)
        _flush(con, batch)
        shape = 'long' if is_long else 'wide'
        print(f"  [{shape}] {n} sun rows, {len(sites)} sites", file=sys.stderr)
    return n, sites

# ---------- main -------------------------------------------------------------

def main():
    if len(sys.argv) != 4:
        print("Usage: build_db.py <sunsets.csv> <locations.csv> <out.db>", file=sys.stderr)
        sys.exit(1)
    sun_src, loc_src, dst = sys.argv[1], sys.argv[2], sys.argv[3]

    if os.path.exists(dst):
        os.remove(dst)

    con = sqlite3.connect(dst)
    con.executescript("""
        PRAGMA journal_mode = OFF;
        PRAGMA synchronous  = OFF;
        PRAGMA temp_store   = MEMORY;

        CREATE TABLE sun (
            site     TEXT NOT NULL,
            date_iso TEXT NOT NULL,
            event    TEXT NOT NULL,
            time     TEXT NOT NULL
        );
        CREATE TABLE sites (
            site TEXT PRIMARY KEY
        );
        CREATE TABLE locations (
            name_key    TEXT PRIMARY KEY,
            name        TEXT NOT NULL,
            longitude   TEXT,
            latitude    TEXT,
            timezone    TEXT,
            utc_offset  TEXT,
            elevation   TEXT
        );
    """)

    # ---- sun ----
    print("Loading sun rows...", file=sys.stderr)
    with con:
        n_sun, sites = load_sun(sun_src, con)
        con.executemany("INSERT OR IGNORE INTO sites VALUES (?)",
                        [(s,) for s in sites])

    # ---- locations ----
    print("Loading locations...", file=sys.stderr)
    n_loc = 0
    with open(loc_src, newline='', encoding='utf-8') as f, con:
        reader = csv.DictReader(f)
        batch = []
        for r in reader:
            name = (r.get('NAME') or '').strip()
            if not name:
                continue
            batch.append((
                norm(name),
                name,
                r.get('LONGITUDE', ''),
                r.get('LATITUDE', ''),
                r.get('TIMEZONE', ''),
                r.get('UTC_OFFSET', ''),
                r.get('ELEVATION', ''),
            ))
            n_loc += 1
        con.executemany(
            "INSERT OR REPLACE INTO locations VALUES (?,?,?,?,?,?,?)", batch
        )
    print(f"  {n_loc} location rows", file=sys.stderr)

    # ---- index ----
    print("Building index...", file=sys.stderr)
    with con:
        con.execute("CREATE INDEX idx_sun_site_date ON sun(site, date_iso)")

    con.close()
    print(f"Done. {dst} ({os.path.getsize(dst)//1024} KB)", file=sys.stderr)

if __name__ == '__main__':
    main()
