# Dawn and Dusk times

This repository has all the dawn and dusk times for Canadian cities as on the
[NRC's
website](https://nrc.canada.ca/en/research-development/products-services/software-applications/sun-calculator/).
This is better for recurring or mass-querying that dataset.

## Setup

Convert the csv files to a sqlitedb, which makes cold lookups significantly
faster.

```
python3 build_db.py sunsets_2026.csv locations.csv dawnofthedusk.db
```

Use `dawnofthedusk.py` to query times


```
python3 dawnofthedusk.py calgary
python3 dawnofthedusk.py 'Calgary AB'
python3 dawnofthedusk.py 'calgary alberta'
python3 dawnofthedusk.py 'Edmonton AB' 2026-11-17
python3 dawnofthedusk.py calgary --short
```

`--short` outputs the civil twilight start and end in 12h time. These are often
the most relevant values for the meaning of "sundown".
