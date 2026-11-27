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
| [`flickr_favourites.py`](flickr_favourites.py) | Favourites in Immich the photos tagged `myfavs` on Flickr, found by capture time. Documented below. |
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
   - **Several hits:** run a time search on the source's capture time and keep the assets whose path is one
     of the hits (no file reads). With `--no-time-first` it instead reads each hit's EXIF date from disk, which is slow.
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

## flickr_favourites.py

Favourites in Immich every photo of yours tagged `myfavs` (change with `--tag`) on Flickr. It imports
helpers from `immich_favourites.py`, so keep the two files together.

For each tagged photo it:

1. Gets the capture time from the Flickr API (`date_taken`). If Flickr doesn't know it (the date is
   the upload date, or only a month or year), it looks the photo up by its Flickr ID in the backup
   (`flickr-index.json`) and uses that file's EXIF date, or failing that the date in its folder and name.
2. Searches Immich by that time, the same way as `immich_favourites.py` (wall-clock time against
   `localDateTime`, within `--date-window`), and drops assets whose picture shape differs.
3. If several assets are left and the Flickr title is still the original file name, keeps the ones
   with that name. Anything still tied is resolved with `--ties` (default `pick`).
4. Favourites the asset (only with `--apply`).

**Renamed film scans.** Only for photos with no date taken (Flickr's `datetakenunknown`, typical of
scans; they skip the time search, since Flickr's date would just be the upload time), the script walks outwards through your photostream
with `flickr.photos.getContext` (previous/next photo, one API call per step) to the nearest photos
whose title is still just a number. It finds those in Immich by time and checks that their file
names are that number. The distance gives the number for this photo: with `0010` one step before and
`0013` two steps after, the photo is `0011`. It then looks up that name in the same folder as the
neighbour. Whether numbers rise or fall along the stream is worked out from the two neighbours, which
must agree (this also checks the stream order). `--one-sided` accepts a single neighbour, using
`--scan-order`. The result is logged as `neighbours` in the state file. Everything fetched is cached
in `flickr-context.json` (delete it if you reorder your photostream), so neighbouring photos in a roll
share their walks. Other options: `--neighbour-steps` (default 5, since a roll is at most 38 frames and few are public and titled), `--no-neighbours`.

(Downloading the whole photostream instead is a poor fit: `flickr.photos.search` caps each query, so
it would need many date-range queries for a library of 70k photos.)

Flickr needs an [API key](https://www.flickr.com/services/apps/create/). Without OAuth the API only
returns public photos. For private ones, run [`flickr_auth.py`](flickr_auth.py) once (it needs
`pip install requests-oauthlib` and `FLICKR_API_KEY` / `FLICKR_API_SECRET` set). It prints a URL to
authorise (read access only), asks for the code Flickr shows, then prints
`FLICKR_OAUTH_TOKEN` and `FLICKR_OAUTH_SECRET`. Export those with the key and secret and use
`--flickr-user me`.

```bash
export IMMICH_API_KEY=xxxx FLICKR_API_KEY=yyyy
./flickr_favourites.py --flickr-user jamiekitson --limit 20     # dry run
./flickr_favourites.py --flickr-user jamiekitson --apply
```

State goes to `flickr-favourites-dryrun.jsonl` or `-apply.jsonl`, keyed by Flickr photo ID (`file`), and
the run resumes the same way as above. Statuses are as above, with `no_date` in place of `no_exif`.
List the photos that weren't found:

```bash
jq -r 'select(.status=="not_found") | "\(.file) \(.title)"' flickr-favourites-dryrun.jsonl
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
