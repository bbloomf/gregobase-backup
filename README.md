# GregoBase backup

A snapshot of every chant's GABC source from [GregoBase](https://gregobase.selapa.net/).

## Layout

- `gabc/<bucket>/<id>.gabc`: GABC for chant `<id>`, bucketed by thousands
  (`gabc/00000/`, `gabc/01000/`, …). Chants whose GregoBase page lists numbered
  elements are saved as `<id>.<elem>.gabc`.
- `gabc/index.tsv`: one row per file, with the chant ID, element, path, and
  chant name (from the GABC `name:` header).
- `chants.csv`: one row per chant with its metadata: title, version, usage,
  mode, Cantus ID, sources (with GregoBase source IDs), tags, warnings,
  remarks, commentary, transcriber, proofreaders, and the dates it was added
  and last modified. Fields with several values are separated by `; `.
  `hidden` is `yes` for chants whose GregoBase page redirects visitors to the
  score list; many are placeholders titled like `░░16424░░`, and some have no
  GABC to download.

Chant `<id>` is viewable at `https://gregobase.selapa.net/chant.php?id=<id>`.

## Updating

```bash
python3 download_gabc.py
python3 download_metadata.py
```

The scripts need only the Python standard library. Both walk chant IDs from 1
upward until a batch of 500 IDs comes back empty.

- `download_gabc.py` overwrites existing files and rebuilds `gabc/index.tsv`
  from the files on disk after every run. After a complete run with no errors
  it also removes files for chants that no longer exist.
- `download_metadata.py` reads each chant page plus its GABC header (the only
  place mode and commentary appear). A complete run rewrites `chants.csv`; any
  other run updates the rows for the chants it reached and keeps the rest.

Chants that fail with a network or server error are retried after 30 seconds,
2 minutes, and 10 minutes; any other error is reported without retrying. If
every chant in a batch fails, the site or the connection is down, so the
script keeps retrying for about an hour before stopping. Either way it ends by
printing the command that picks up what it missed, for example:

```bash
python3 download_gabc.py --ids 7737-7760,9001
python3 download_metadata.py --start 18001
```

Both scripts also take `--start`/`--end` to fetch a range, and
`--workers`/`--delay` to control load on the server.
