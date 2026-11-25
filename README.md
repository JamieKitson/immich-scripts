# immich-scripts

Small Python scripts for tidying an [Immich](https://immich.app) library that is indexed from
external (read-only) libraries.

All scripts read the API key from the environment and are **dry runs unless you pass `--apply`**.

```bash
export IMMICH_API_KEY=xxxx
```

## Scripts

| Script | What it does |
|---|---|
| [`immich_favourites.py`](immich_favourites.py) | Marks photos as favourites in Immich based on a directory of favourite copies. The main script, documented below. |
| [`tz_probe.py`](tz_probe.py) | Read-only test: compares what Immich stored (UTC time, `timeZone`) with each file's own EXIF, to show which timezone offset Immich applies. |
| [`albumscans.py`](albumscans.py) | Adds every asset under a scans folder to an album (`Film Scans`), creating the album if needed. |
| [`tagscans.py`](tagscans.py) | Tags every asset under a scans folder with a tag (`Abba Slide Scans`). |
| [`favs.py`](favs.py) | Earlier favourites script: searches Immich by filename for each source file, writes `favourite_matches.csv`, and favourites unique matches with `--apply`. |
| [`favsall.py`](favsall.py) | Earlier favourites script: downloads all asset metadata from Immich and matches filenames locally, writes `favourite_matches.csv`, and favourites unique matches with `--apply`. |

`albumscans.py`, `tagscans.py`, `favs.py` and `favsall.py` have their server URL, folders, album or
tag name hard-coded near the top of the file, so edit those before running them.

## immich_favourites.py

You have a directory holding a *copy* of your favourite photos (e.g. resized for a photo frame)
and a large library indexed by Immich. This script finds the originals and favourites them.

Matching on the file system first and then looking assets up by path means the script rarely has
to search the whole Immich library.

Immich's path lookup is slow (about 0.5 s per call in testing), while a time-window search takes
10-75 ms. So when a file is found on disk, the script first runs one time search using that file's
EXIF time and picks the asset by exact `originalPath`. Only if that misses does it use the path
lookup. The state file records which was used as `lookup` (`time`, `path` or `mixed`).

### How it matches

For each file in `--source`:

1. Search the library roots (host paths) for a file with the same name.
   - **One hit:** look the asset up in Immich by its `originalPath`.
   - **Several hits:** compare the EXIF capture time of the source with each hit and use the matching one(s).
   - **No hits** (or the hit isn't in Immich, or none has a matching date): search Immich by capture time.
2. If several versions still match, pick one (`--ties`, see below).
3. Favourite the asset. Already-favourite assets are skipped.

Time search compares the source's wall-clock EXIF time with each asset's `localDateTime`, because
Immich stores UTC and the camera's offset isn't known. It fetches a window of `--max-tz-offset`
hours either side (one query, 15-75 ms in testing), then filters on `localDateTime`, the aspect
ratio of the picture, and the filename.

`tz_probe.py` is a read-only test of how Immich picks the offset: the offset tag is always honoured,
GPS gives the local zone with summer time, and files with neither get UTC (apart from some Nikon
files, probably from maker notes). Narrowing the search to specific offsets wasn't worth the
complexity, since the wide query is already fast.

### Flickr backups

Files named `DD-HH-MM-<original name>-<flickr id>.<ext>` come from a Flickr backup
(see [flickrBackup.php](https://github.com/JamieKitson/misc/blob/master/flickrBackup.php)).
For these the script:

1. Tries the original name first, `<original name>.<ext>`, and carries on as normal if it finds it.
   It also tries the last `-` as `_` and all `-` as `_`, and the extension in lower and upper case,
   because Flickr turns `DSC_3521` into `DSC-3521`.
2. If the source has no EXIF and a date is needed, looks the file up in `--flickr-root`.
   It uses that file's EXIF date if present, otherwise the date from its `YYYY-MM` folder and the
   `DD-HH-MM` part of the name (minute precision, so a ±31 s window is used).

The Flickr folder is crawled lazily, only when a date is needed, and the result is cached in
`flickr-index.json` because the drive is slow. Use `--reindex-flickr` to rebuild it, or
`--flickr-root ''` to switch the lookup off.

### Setup and usage

```bash
pip install requests pillow

# dry run: nothing is written to Immich
./immich_favourites.py --url http://localhost:2283/api

# try a small batch first
./immich_favourites.py --url http://localhost:2283/api --limit 50

# for real
./immich_favourites.py --url http://localhost:2283/api --apply
```

### Library paths

The script searches host directories but Immich stores container paths, so each root is a
host/container pair. The defaults are set in `DEFAULT_MAPS` at the top of the script, matching the
read-only mounts in `docker-compose.yml`:

| Host path | Container path |
|---|---|
| `/btsync` | `/external/older` |
| `/mnt/sea5t/backup` | `/external/newer` |
| `/mnt/sea5t/JamiePixel3a2` | `/external/phones/JamiePixel3a2` |
| `/mnt/sea5t/JamiePixel7Pro` | `/external/phones/JamiePixel7Pro` |
| `/mnt/sea5t/JamieSurfaceDuo` | `/external/phones/JamieSurfaceDuo` |

Add another with `--search-root HOST=CONTAINER` (repeatable). Passing it **replaces** the defaults,
so list every root you want searched.

### Options

| Option | Default | Meaning |
|---|---|---|
| `--url` | `$IMMICH_URL` or `http://localhost:2283/api` | Immich API base URL (ends in `/api`) |
| `--api-key` | `$IMMICH_API_KEY` | API key |
| `--web-url` | `--url` without `/api` | Immich web UI base URL, used for links in the log |
| `--source` | `/srv/http/192.168.1.4/resized/` | Directory of favourite copies |
| `--search-root HOST=CONTAINER` | see above | Library root to search; repeatable |
| `--apply` | off | Actually write to Immich |
| `--date-window` | `1.0` | EXIF time tolerance in seconds |
| `--no-time-first` | off | Look files up by path only. By default a fast time search is tried first (see below) |
| `--max-tz-offset` | `14.0` | Largest camera UTC offset (hours) considered in time search |
| `--ties` | `pick` | When several versions match: `pick` one, `skip` (log as ambiguous), or favourite `all` |
| `--ignore-case` | off | Case-insensitive filename matching |
| `--flickr-root` | `/mnt/disk/flickrBackup/jamie/` | Flickr backup directory (`''` to disable) |
| `--flickr-cache` | `flickr-index.json` | Cache file for the Flickr crawl |
| `--reindex-flickr` | off | Re-crawl the Flickr backup |
| `--state` | `favourites-<dryrun\|apply>.jsonl` | State file |
| `--restart` | off | Ignore existing state and start over |
| `--retry-errors` | off | Re-process entries that errored |
| `--stats-every` | `25` | Print stats every N files |
| `--limit` | none | Only process the first N files |

### Choosing between versions (`--ties pick`)

When the same picture exists in several versions, the script prefers:

1. `.jpg` / `.jpeg`, then other formats, then raw (nef, cr2, cr3, arw, dng, raf, orf, rw2, ...).
2. Names without `COVER` or `_NNNN` (four digits after an underscore).
3. The shorter path, then alphabetical order, so the choice is repeatable.

The state file records `picked_from` (number of candidates) and `others` (IDs not chosen).

### Output, resuming and statuses

- Each file gets a log line, plus a stats line (counts, rate, ETA) every `--stats-every` files.
  Output also goes to `favourites-dryrun.log` or `favourites-apply.log`.
- Every result is appended to `favourites-dryrun.jsonl` or `favourites-apply.jsonl`. Re-running the
  same command resumes where it stopped (Ctrl-C is safe). Dry runs and `--apply` runs use
  separate files.
- Delete the state file or use `--restart` to start over. `--retry-errors` re-processes only the
  files that errored.
- With `--ties skip`, ambiguous files are logged with a link to each candidate asset in the Immich UI.

| Status | Meaning |
|---|---|
| `favourited` / `would_favourite` | Asset favourited (or would be, in a dry run) |
| `already_favourite` | Nothing to do |
| `not_found` | No Immich asset matched by time |
| `not_in_immich` | A file was found but no asset could be identified |
| `ambiguous` | Several matches and `--ties skip` is set |
| `no_exif` | No usable date and no name match |
| `error` | An exception; see the log. Retry with `--retry-errors` |

To re-process only one status, remove those lines from the state file, e.g.:

```bash
jq -c 'select(.status!="not_found")' favourites-dryrun.jsonl > tmp && mv tmp favourites-dryrun.jsonl
```

### Notes and limitations

- Naive EXIF times are compared as wall-clock time; offset tags are ignored.
- HEIC, RAW and video files that Pillow can't read have no EXIF date and are logged as `no_exif`.
- A single filename match is trusted without checking the date.
- Immich can't favourite offline assets, so rescan the external libraries if paths have moved.
