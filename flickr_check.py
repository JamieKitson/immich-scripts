#!/usr/bin/env python3
"""Check every photo in the Flickr backup for presence in the Immich library (read-only).

Flickr side: the backup files, <root>/YYYY-MM/DD-HH-MM-<title>-<flickr id>.<ext>. The crawl is
cached (flickr-index.json, shared with the other scripts), so the slow drive is only touched to
read an image's dimensions when a photo can't be matched by name.

Immich side: asset metadata for the whole date range is downloaded once (in weekly windows, cached
in immich-assets.json) and matched locally, comparing the backup's capture minute with each asset's
localDateTime (wall-clock time).

Within each minute, Flickr photos are paired with Immich assets, strongest evidence first:

  found_name   original file name matches the Flickr title (IMG_1071 == IMG-1071)
  found_dims   same pixel dimensions (either orientation)
  weak         only the time (and a compatible shape / media type) matches: a burst could be
               hiding a missing photo, so check these by eye
  name_only    no match at that time, the backup file has no EXIF date taken, and Immich has exactly
               one file with the same name (its date is logged); counted as found
  undated_missing  no match at that time, the backup file has no EXIF date taken, and Immich has
               no file, or more than one file, with that name
  missing      no match at that time and the backup file does have an EXIF date taken, so it
               should have matched by time

Only photos still unmatched after the time pass have their EXIF read (the drive is slow); the
result is cached in flickr-exif.json.

The CSV is written minute by minute as the script goes. Rerunning continues after the photos
already in it (--restart starts over), so stopping early loses nothing.

Each Immich asset is used for at most one Flickr photo, so a burst of 5 in Flickr and 3 in
Immich shows 2 missing. Results go to a CSV; nothing is written to Immich or the backup.

    pip install requests pillow
    export IMMICH_API_KEY=xxxx
    ./flickr_check.py --url http://localhost:2283/api --until 2008-12   # early photos first
    ./flickr_check.py --url http://localhost:2283/api
"""
import argparse
import csv
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from PIL import Image

from immich_favourites import FlickrIndex, Immich, iso, log, ratio_close

NAME_RE = re.compile(r"^(\d\d)-(\d\d)-(\d\d)-(.+)-(\d+)\.([A-Za-z0-9]+)$")
MONTH_RE = re.compile(r"^(\d{4})-(\d\d)$")
VIDEO_EXT = {"mov", "mp4", "avi", "m4v", "3gp", "mpg", "mpeg", "wmv", "mts", "mkv", "webm"}
STATUSES = ["found_name", "found_dims", "weak", "name_only", "undated_missing", "missing"]
COLUMNS = ["status", "flickr_time", "title", "flickr_id", "flickr_path", "exif_date",
           "asset_time", "asset_name", "asset_path", "asset_link"]


def norm(name):
    """Compare names loosely: no extension, case or punctuation (Flickr turns DSC_3521 into DSC-3521)."""
    return re.sub(r"[^a-z0-9]", "", re.sub(r"\.[A-Za-z0-9]{2,4}$", "", name or "").lower())


def parse_bound(s, end):
    """YYYY-MM or YYYY-MM-DD -> datetime (start of that month/day, or for end the start of the next)."""
    for fmt, step in (("%Y-%m-%d", timedelta(days=1)), ("%Y-%m", None)):
        try:
            d = datetime.strptime(s, fmt)
        except ValueError:
            continue
        if not end:
            return d
        if step:
            return d + step
        return datetime(d.year + d.month // 12, d.month % 12 + 1, 1)
    sys.exit(f"bad date {s!r}: use YYYY-MM or YYYY-MM-DD")


# ---------------------------------------------------------------- flickr side
def load_flickr(index, since, until):
    """List of dicts (minute, title, fid, ext, path) for backup files in range, plus count of
    files that don't look like backup photos."""
    if index.files is None:
        index._load()
    items, ignored = [], 0
    for name, dirs in index.files.items():
        for rel in dirs:
            m, fm = NAME_RE.match(name), MONTH_RE.match(os.path.basename(rel))
            if not (m and fm):
                ignored += 1
                continue
            try:
                when = datetime(int(fm[1]), int(fm[2]), int(m[1]), int(m[2]), int(m[3]))
            except ValueError:
                ignored += 1
                continue
            if (since and when < since) or (until and when >= until):
                continue
            items.append({"minute": when, "title": m[4], "fid": m[5], "ext": m[6].lower(),
                          "path": os.path.join(index.root, rel, name)})
    items.sort(key=lambda f: (f["minute"], f["fid"]))
    return items, ignored


class Dims:
    """Pixel size of backup files, read lazily (header only) and cached because the drive is slow."""

    def __init__(self, path):
        self.path, self.cache, self.dirty = path, {}, False
        try:
            with open(path) as f:
                self.cache = json.load(f)
        except Exception:
            pass

    def get(self, f):
        if f["path"] not in self.cache:
            try:
                with Image.open(f["path"]) as im:
                    self.cache[f["path"]] = list(im.size)
            except Exception:
                self.cache[f["path"]] = None
            self.dirty = True
        v = self.cache[f["path"]]
        return tuple(v) if v else None

    def save(self):
        if self.dirty:
            with open(self.path, "w") as f:
                json.dump(self.cache, f)


class ExifDates:
    """EXIF date taken of backup files ("" if none), read lazily and cached (slow drive)."""

    def __init__(self, path):
        self.path, self.cache, self.dirty = path, {}, False
        try:
            with open(path) as f:
                self.cache = json.load(f)
        except Exception:
            pass

    def get(self, f):
        if f["path"] not in self.cache:
            date = ""
            try:
                with Image.open(f["path"]) as im:
                    ex = im.getexif()
                    date = (ex.get_ifd(0x8769).get(36867) or ex.get_ifd(0x8769).get(36868)
                            or "")
                    date = str(date).strip().strip("\x00").strip()
                    if date.startswith("0000"):
                        date = ""
            except Exception:
                date = ""  # unreadable or not an image (e.g. video): no EXIF date
            self.cache[f["path"]] = date
            self.dirty = True
        return self.cache[f["path"]]

    def save(self):
        if self.dirty:
            with open(self.path, "w") as f:
                json.dump(self.cache, f)


# ---------------------------------------------------------------- immich side
def slim(a):
    e = a.get("exifInfo") or {}
    return {"id": a["id"], "local": a.get("localDateTime"), "name": a.get("originalFileName"),
            "path": a.get("originalPath"), "type": a.get("type"),
            "w": e.get("exifImageWidth"), "h": e.get("exifImageHeight")}


def fetch_assets(im, start, end, pad_hours):
    """All assets taken between start and end (padded for timezone offsets, deduplicated by id)."""
    assets, t, t0, step = {}, start, time.time(), timedelta(days=7)
    pad = timedelta(hours=pad_hours)
    total_days = max((end - start).days, 1)
    while t < end:
        t2, page = min(t + step, end), 1
        while True:
            r = im._req("POST", "/search/metadata",
                        json={"takenAfter": iso(t - pad), "takenBefore": iso(t2 + pad),
                              "size": 250, "withExif": True, "page": page}).json()["assets"]
            for a in r["items"]:
                assets[a["id"]] = slim(a)
            if not r.get("nextPage"):
                break
            page = int(r["nextPage"])
        t = t2
        if (t - start).days // 7 % 26 == 0:  # about every half year
            log(f"  fetched to {t:%Y-%m-%d}: {len(assets)} assets, "
                f"{(t - start).days * 100 // total_days}%, {time.time() - t0:.0f}s")
    return list(assets.values())


def load_assets(im, args, start, end):
    """Cached asset list. The cache records the date range it covers; a request inside that range
    is served from it, and a larger one only downloads the missing ends. --refetch starts over, and
    a different server or --tz-pad discards the cache."""
    ident = {"url": im.url, "pad": args.tz_pad}
    have, cs, ce, built = {}, None, None, time.time()
    if not args.refetch and os.path.exists(args.immich_cache):
        try:
            with open(args.immich_cache) as f:
                c = json.load(f)
            k = c.get("key", {})
            if all(k.get(n) == v for n, v in ident.items()):
                have = {a["id"]: a for a in c["assets"]}
                cs, ce = datetime.fromisoformat(k["start"]), datetime.fromisoformat(k["end"])
                built = c.get("built", built)
            else:
                log("immich cache is for a different server or --tz-pad, refetching")
        except Exception as e:
            log(f"immich cache unreadable ({e}), refetching")
            have, cs, ce = {}, None, None
    if cs is None:
        gaps = [(start, end)]
    else:
        gaps = ([(start, cs)] if start < cs else []) + ([(ce, end)] if end > ce else [])
    if not gaps:
        log(f"immich assets loaded from {args.immich_cache}: {len(have)} "
            f"({(time.time() - built) / 86400:.1f} days old, --refetch to update)")
        return list(have.values())
    for g0, g1 in gaps:
        log(f"downloading Immich metadata {g0:%Y-%m-%d} .. {g1:%Y-%m-%d} ...")
        for a in fetch_assets(im, g0, g1, args.tz_pad):
            have[a["id"]] = a
    lo, hi = (start, end) if cs is None else (min(start, cs), max(end, ce))
    key = {**ident, "start": lo.isoformat(), "end": hi.isoformat()}
    tmp = args.immich_cache + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"key": key, "built": time.time(), "assets": list(have.values())}, f)
    os.replace(tmp, args.immich_cache)
    log(f"{len(have)} assets cached to {args.immich_cache} ({lo:%Y-%m-%d} .. {hi:%Y-%m-%d})")
    return list(have.values())


# ---------------------------------------------------------------- matching
def same_dims(a, b):
    return bool(a and b) and sorted(a) == sorted(b)


def asset_dims(a):
    return (a["w"], a["h"]) if a.get("w") and a.get("h") else None


def match_minute(items, avail, dims):
    """Pair Flickr items with assets from the same minute. Returns [(item, asset|None, status)]."""
    out, left = {}, list(avail)

    def take(item, pred, status):
        for a in left:
            if pred(item, a):
                left.remove(a)
                out[id(item)] = (item, a, status)
                return

    todo = list(items)
    for status, pred in (
        ("found_name", lambda f, a: norm(f["title"]) == norm(a["name"])),
        ("found_dims", lambda f, a: same_dims(dims(f), asset_dims(a))),
        ("weak", lambda f, a: (f["ext"] in VIDEO_EXT) == (a.get("type") == "VIDEO")
                              and ratio_close(dims(f), asset_dims(a))),
    ):
        if not left:
            break
        for f in todo:
            if id(f) not in out and left:
                take(f, pred, status)
    return [out.get(id(f), (f, None, "missing")) for f in items]


def name_lookup(im, f):
    """Immich assets with the Flickr photo's original name, whatever their date."""
    title = f["title"]
    variants = {title, re.sub(r"-(\d+)$", r"_\1", title), title.replace("-", "_")}
    seen = {}
    for v in variants:
        try:
            for a in im.search({"originalFileName": v}):
                if norm(a.get("originalFileName")) == norm(title):
                    seen[a["id"]] = slim(a)
        except Exception as e:
            log(f"name lookup failed for {v!r}: {e}")
    return list(seen.values())


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=os.environ.get("IMMICH_URL", "http://localhost:2283/api"),
                    help="Immich API base URL (ends in /api)")
    ap.add_argument("--api-key", default=os.environ.get("IMMICH_API_KEY"))
    ap.add_argument("--web-url", help="Immich web UI base URL for links (default: --url without /api)")
    ap.add_argument("--flickr-root", default="/mnt/disk/flickrBackup/jamie/")
    ap.add_argument("--flickr-cache", default="flickr-index.json")
    ap.add_argument("--reindex-flickr", action="store_true", help="Re-crawl the Flickr backup")
    ap.add_argument("--since", help="First month/day to check, YYYY-MM or YYYY-MM-DD (default: everything)")
    ap.add_argument("--until", help="Last month/day to check, inclusive (default: everything)")
    ap.add_argument("--tz-pad", type=float, default=14.0,
                    help="Hours either side to fetch from Immich, to cover camera timezone offsets")
    ap.add_argument("--immich-cache", default="immich-assets.json")
    ap.add_argument("--refetch", action="store_true", help="Download the Immich metadata again")
    ap.add_argument("--no-name-fallback", action="store_true",
                    help="Skip the by-file-name Immich search for undated photos with no match by time")
    ap.add_argument("--dims-cache", default="flickr-dims.json")
    ap.add_argument("--exif-cache", default="flickr-exif.json")
    ap.add_argument("--report", default="flickr-check.csv",
                    help="CSV of every Flickr photo and its result; rows are written as it goes and "
                         "photos already in it are skipped, so a rerun continues where it stopped")
    ap.add_argument("--restart", action="store_true", help="Overwrite the report and start from scratch")
    args = ap.parse_args()

    if not args.api_key:
        sys.exit("Set IMMICH_API_KEY or pass --api-key")
    if not os.path.isdir(args.flickr_root) and not os.path.exists(args.flickr_cache):
        sys.exit(f"Flickr root not found: {args.flickr_root}")
    web = (args.web_url or re.sub(r"/api/?$", "", args.url)).rstrip("/")
    since = parse_bound(args.since, False) if args.since else None
    until = parse_bound(args.until, True) if args.until else None

    index = FlickrIndex(args.flickr_root, args.flickr_cache, args.reindex_flickr, None)
    flickr, ignored = load_flickr(index, since, until)
    if not flickr:
        sys.exit("no Flickr backup photos in that range")
    log(f"{len(flickr)} Flickr backup files in range ({ignored} other files ignored), "
        f"{flickr[0]['minute']:%Y-%m-%d} .. {flickr[-1]['minute']:%Y-%m-%d}")

    im = Immich(args.url, args.api_key)
    start = datetime(flickr[0]["minute"].year, flickr[0]["minute"].month, flickr[0]["minute"].day)
    end = flickr[-1]["minute"] + timedelta(days=1)
    assets = load_assets(im, args, start, end)

    by_min = defaultdict(list)
    for a in assets:
        try:
            lt = datetime.fromisoformat(a["local"].replace("Z", "+00:00")).replace(tzinfo=None)
        except Exception:
            continue
        by_min[lt.replace(second=0, microsecond=0)].append(a)
    for lst in by_min.values():
        lst.sort(key=lambda a: a["path"] or "")

    dims = Dims(args.dims_cache)
    exif = ExifDates(args.exif_cache)
    groups = defaultdict(list)
    for f in flickr:
        groups[f["minute"]].append(f)

    # Resume: photos already in the report are skipped, and their assets are not offered again.
    done_paths, done_assets = set(), set()
    if os.path.exists(args.report) and not args.restart:
        with open(args.report, newline="", encoding="utf-8") as fh:
            rd = csv.DictReader(fh)
            if rd.fieldnames != COLUMNS:
                sys.exit(f"{args.report} has different columns (older version?): move it aside "
                         f"or pass --restart to overwrite it")
            for row in rd:
                if row["flickr_path"]:
                    done_paths.add(row["flickr_path"])
                if row["asset_link"]:
                    done_assets.add(row["asset_link"].rsplit("/", 1)[-1])
    new_file = args.restart or not os.path.exists(args.report) or os.path.getsize(args.report) == 0
    todo_n = sum(1 for f in flickr if f["path"] not in done_paths)
    log(f"{len(done_paths)} photos already in {args.report}, {todo_n} to do")

    written = 0
    with open(args.report, "w" if new_file else "a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        if new_file:
            w.writerow(COLUMNS)
            fh.flush()
        try:
            for minute, items in groups.items():
                items = [f for f in items if f["path"] not in done_paths]
                if not items:
                    continue
                avail = [a for a in by_min.get(minute, []) if a["id"] not in done_assets]
                res = match_minute(items, avail, dims.get)
                for i, (f, a, status) in enumerate(res):
                    if status != "missing" or exif.get(f):
                        continue  # matched, or has a date taken but nothing matched: missing
                    status = "undated_missing"
                    if not args.no_name_fallback:
                        hits = name_lookup(im, f)
                        if len(hits) == 1:
                            a, status = hits[0], "name_only"
                    res[i] = (f, a, status)
                for f, a, status in res:
                    w.writerow([status, f"{f['minute']:%Y-%m-%d %H:%M}", f["title"], f["fid"], f["path"],
                                exif.cache.get(f["path"], ""),
                                a["local"] if a else "", a["name"] if a else "", a["path"] if a else "",
                                f"{web}/photos/{a['id']}" if a else ""])
                fh.flush()
                before, written = written, written + len(res)
                if before // 500 != written // 500:
                    log(f"  {written}/{todo_n} done, at {minute:%Y-%m-%d}")
        except KeyboardInterrupt:
            log(f"interrupted after {written} photos; run again to continue from there")
        finally:
            dims.save()
            exif.save()

    per_year, tot = defaultdict(Counter), Counter()
    with open(args.report, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            per_year[row["flickr_time"][:4]][row["status"]] += 1
            tot[row["status"]] += 1
    print(f"\n{'year':>6} {'total':>7} " + " ".join(f"{s:>15}" for s in STATUSES))
    for y in sorted(per_year):
        c = per_year[y]
        print(f"{y:>6} {sum(c.values()):>7} " + " ".join(f"{c[s]:>15}" for s in STATUSES))
    print(f"{'all':>6} {sum(tot.values()):>7} " + " ".join(f"{tot[s]:>15}" for s in STATUSES))
    print(f"\nFull log: {args.report}  (filter status=missing and undated_missing; review weak and name_only by eye)")


if __name__ == "__main__":
    main()
