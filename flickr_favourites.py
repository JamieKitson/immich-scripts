#!/usr/bin/env python3
"""
Favourite in Immich the photos you have tagged "myfavs" on Flickr.

For each Flickr photo with the tag:
  1. Get the time it was taken: from the Flickr API (date_taken extra); if Flickr doesn't know
     it, from the Flickr backup on disk (looked up by Flickr ID in flickr-index.json).
  2. Search Immich by that capture time (compared with Immich's localDateTime, see README),
     then narrow by picture shape and, where the Flickr title is still the original file name,
     by file name.
  3. Favourite the asset (only with --apply; default is a dry run).

Progress is appended to a JSONL state file, so an interrupted run resumes where it stopped.
Dry-run and --apply runs keep separate state files.

Flickr access: you need a Flickr API key (https://www.flickr.com/services/apps/create/).
Without OAuth only your public photos are visible to the API. To include private ones, also give
the OAuth key/secret and token/secret (see README).

Setup:   pip install requests pillow          (and requests-oauthlib for private photos)
Usage:   export IMMICH_API_KEY=xxxx FLICKR_API_KEY=yyyy
         ./flickr_favourites.py --flickr-user jamiekitson                  # dry run
         ./flickr_favourites.py --flickr-user jamiekitson --apply          # for real
"""
import argparse
import json
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone

import requests

from immich_favourites import (RAW_EXT, FlickrIndex, Immich, close, log, pick_best,  # noqa: F401
                               ratio_close, resolve_tie)

API = "https://api.flickr.com/services/rest/"
ID_RE = re.compile(r"-(\d+)\.[A-Za-z0-9]+$")   # backup names end in -<flickr id>.<ext>


# ---------------------------------------------------------------- flickr
class Flickr:
    def __init__(self, key, secret=None, token=None, token_secret=None):
        if token and token_secret and secret:
            try:
                from requests_oauthlib import OAuth1Session
            except ImportError:
                sys.exit("pip install requests-oauthlib to use Flickr OAuth")
            self.s = OAuth1Session(key, client_secret=secret, resource_owner_key=token,
                                   resource_owner_secret=token_secret)
            self.oauth = True
        else:
            self.s = requests.Session()
            self.oauth = False
        self.key = key

    def call(self, method, **params):
        p = {"method": method, "api_key": self.key, "format": "json", "nojsoncallback": 1, **params}
        for attempt in range(4):
            try:
                r = self.s.get(API, params=p, timeout=60)
                r.raise_for_status()
                j = r.json()
                if j.get("stat") != "ok":
                    raise RuntimeError(f"flickr {method}: code {j.get('code')} {j.get('message')}")
                return j
            except (requests.ConnectionError, requests.Timeout, requests.HTTPError):
                if attempt == 3:
                    raise
                time.sleep(2 ** attempt)

    def resolve_user(self, user):
        """NSID (12345@N00), 'me' (needs OAuth), profile URL or alias -> NSID or 'me'."""
        if user == "me" or "@" in user:
            return user
        url = user if user.startswith("http") else f"https://www.flickr.com/photos/{user}/"
        return self.call("flickr.urls.lookupUser", url=url)["user"]["id"]

    def tagged(self, user_id, tag, logf):
        out, page = [], 1
        while True:
            r = self.call("flickr.photos.search", user_id=user_id, tags=tag, tag_mode="all",
                          extras="date_taken,original_format,o_dims,tags", per_page=500,
                          page=page, sort="date-posted-asc")["photos"]
            out += r["photo"]
            log(f"flickr: page {page}/{r['pages']}, {len(out)}/{r['total']} photos", logf)
            if page >= int(r["pages"]):
                return out
            page += 1


class Context:
    """Photostream neighbours via flickr.photos.getContext (one API call per step). Everything
    fetched is cached on disk, so neighbouring photos in one roll reuse each other's walks."""

    def __init__(self, fl, path):
        self.fl, self.path = fl, path
        self.d = {"ctx": {}, "titles": {}, "info": {}}
        try:
            with open(path) as f:
                self.d = json.load(f)
        except (OSError, ValueError):
            pass

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.d, f)
        os.replace(tmp, self.path)

    def neighbours(self, pid):
        """{'prev': id|None, 'next': id|None} in photostream order."""
        c = self.d["ctx"].get(pid)
        if c is None:
            j = self.fl.call("flickr.photos.getContext", photo_id=pid)
            c = {}
            for key in ("prev", "next"):
                x = j.get(key + "photo") or {}
                nid = str(x.get("id") or "0")
                c[key] = None if nid == "0" else nid
                if c[key]:
                    self.d["titles"][nid] = x.get("title", "")
            self.d["ctx"][pid] = c
        return c

    def title(self, pid):
        return self.d["titles"].get(pid, "")


class FilmIndex:
    """Numbered scan files (0010.jpg, ...) under the film folder, by number. Walked lazily, once."""

    EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".heic"} | RAW_EXT

    def __init__(self, host, container, logf):
        self.host, self.container_root, self.logf = os.path.normpath(host), container.rstrip("/"), logf
        self.by_num = None

    def find(self, n):
        """[(host dir, file name)] of every image file whose name is the number n."""
        if self.by_num is None:
            t0, self.by_num, count = time.time(), {}, 0
            for dp, _dn, fns in os.walk(self.host, onerror=lambda e: log(f"walk error: {e}", self.logf)):
                for fn in fns:
                    stem, ext = os.path.splitext(fn)
                    if stem.isdigit() and ext.lower() in self.EXTS:
                        self.by_num.setdefault(int(stem), []).append((dp, fn))
                        count += 1
            log(f"film index: {count} numbered files under {self.host} in {time.time() - t0:.0f}s", self.logf)
        return self.by_num.get(n, [])

    def container(self, host_dir, fn):
        rel = os.path.relpath(os.path.join(host_dir, fn), self.host)
        return self.container_root + "/" + rel.replace(os.sep, "/")


def numeric_title(p):
    """int if the Flickr title is just digits (an untouched scan name like '0045'), else None."""
    m = re.fullmatch(r"\s*(\d+)\s*", re.sub(r"\.[A-Za-z0-9]{2,4}$", "", p.get("title") or ""))
    return int(m.group(1)) if m else None


class BackupById:
    """Date lookup in the flickr backup by Flickr photo ID, using FlickrIndex (cached crawl)."""

    def __init__(self, fi):
        self.fi, self.ids = fi, None

    def date_for_id(self, pid):
        if self.fi.files is None:
            self.fi._load()
        if self.ids is None:
            self.ids = {}
            for name in self.fi.files:
                m = ID_RE.search(name)
                if m:
                    self.ids.setdefault(m.group(1), name)
        name = self.ids.get(str(pid))
        if not name:
            return None, None, "not in flickr backup", None
        return self.fi.date_for(name)


# ---------------------------------------------------------------- core
def norm(name):
    return re.sub(r"[^a-z0-9]", "", re.sub(r"\.[A-Za-z0-9]{2,4}$", "", name or "").lower())


def time_assets(im, t, w, max_tz_offset):
    """Assets whose wall-clock time (Immich localDateTime) is within w seconds of t."""
    out = []
    for a in im.by_time(t, max_tz_offset * 3600 + w):
        try:
            lt = datetime.fromisoformat(a["localDateTime"].replace("Z", "+00:00"))
        except Exception:
            continue
        if close(lt, t, w):
            out.append(a)
    return out


def photo_time(p, backup, detail, args):
    """(datetime labelled UTC, window) for a Flickr photo, or (None, None)."""
    if str(p.get("datetakenunknown", "0")) != "1" and str(p.get("datetakengranularity", "0")) == "0":
        try:
            t = datetime.strptime(p["datetaken"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            detail["time_from"] = "flickr api"
            return t, args.date_window
        except (KeyError, ValueError):
            pass
    detail["flickr_date"] = (f"unknown={p.get('datetakenunknown')} "
                             f"granularity={p.get('datetakengranularity')} value={p.get('datetaken')}")
    if backup is not None:
        t, win, note, _path = backup.date_for_id(p["id"])
        if t:
            detail["time_from"] = "backup path (minute)" if win else "backup file EXIF"
            return t, win or args.date_window
        detail["backup"] = note
    return None, None


def flickr_size(p):
    try:
        return int(p["width_o"]), int(p["height_o"])
    except (KeyError, ValueError, TypeError):
        return None


def asset_dims(a):
    e = a.get("exifInfo") or {}
    w, h = e.get("exifImageWidth"), e.get("exifImageHeight")
    return (w, h) if w and h else None


def candidates_by_time(p, im, backup, args, detail):
    """Immich assets at the photo's capture time with a similar shape. (None, note) if no date."""
    t, win = photo_time(p, backup, detail, args)
    if t is None:
        return None
    detail["time"] = t.strftime("%Y-%m-%d %H:%M:%S")
    items = time_assets(im, t, win, args.max_tz_offset)
    detail["time_candidates"] = len(items)
    if not args.no_ratio:
        size = flickr_size(p)
        items = [a for a in items if ratio_close(size, asset_dims(a))]
    return items


def finish(assets, detail, im, args):
    detail.update(assets=[a["id"] for a in assets], paths=[a.get("originalPath") for a in assets])
    todo = [a for a in assets if not a.get("isFavorite")]
    if not todo:
        return "already_favourite", detail
    if args.apply:
        for a in todo:
            im.favourite(a["id"])
        return "favourited", detail
    return "would_favourite", detail


def resolve_files(p, targets, im, args, detail, label):
    """Immich asset for candidate film files [(host dir, name)]: look each up by path, keep similar
    shapes, and only answer if they are all in one folder (one roll). Reason goes in detail."""
    film = args.film
    if not targets:
        detail["neighbours"] = f"{label}: no such file under {film.host}"
        return None
    assets = []
    for d, fn in targets:
        assets += im.by_path(film.container(d, fn))
    if not assets:
        detail["neighbours"] = f"{label}: found {len(targets)} file(s) on disk (e.g. {targets[0][1]}) but none in immich"
        return None
    if not args.no_ratio:
        assets = [a for a in assets if ratio_close(flickr_size(p), asset_dims(a))]
    folders = {os.path.dirname(a["originalPath"]) for a in assets}
    if len(folders) != 1:
        detail["neighbours"] = (f"{label}: {len(folders)} candidate folders: "
                                + ", ".join(sorted(folders)[:3]))
        return None
    best = pick_best(assets)
    detail["neighbours"] = f"{label}: {best['originalPath']}"
    return best


def via_neighbours(p, ctx, im, args, detail):
    """Film scans with no date. Their numbers look like 71810002: roll 718100, frame 02.
    If the Flickr title is itself such a number the file is just that name. Otherwise (the title was
    changed) walk the photostream (getContext) to the nearest photos whose title is still a number and
    work out this one's frame from the distance. Files are found by number in the film folder.
    Returns an asset or None (reason in detail['neighbours'])."""
    film = args.film
    own = numeric_title(p)
    if own is not None:
        detail.update(wanted_number=own, neighbour_match="own title")
        return resolve_files(p, film.find(own), im, args, detail, f"title {own}")

    pid = str(p["id"])

    def nearest(direction):
        cur = pid
        for d in range(1, args.neighbour_steps + 1):
            nid = ctx.neighbours(cur)[direction]
            if not nid:
                return None
            n = numeric_title({"title": ctx.title(nid)})
            if n is not None:
                return nid, d, n
            cur = nid
        return None

    before, after = nearest("prev"), nearest("next")
    in_roll = lambda f: 1 <= f <= args.max_frame
    frame = lambda a: a[2] % 100      # last two digits: 71810002 -> 2
    roll = lambda a: a[2] - frame(a)  # the rest:          71810002 -> 71810000

    def guess(anchor, is_before, order):
        """Frame this photo would have if frames run in `order` along the stream."""
        sgn = 1 if order == "asc" else -1
        return frame(anchor) + sgn * anchor[1] if is_before else frame(anchor) - sgn * anchor[1]

    chosen = None   # (wanted number, order, anchor ids, how)
    if before and after:
        if roll(before) == roll(after):
            for order in ("asc", "desc"):
                w1, w2 = guess(before, True, order), guess(after, False, order)
                if w1 == w2 and in_roll(w1):
                    chosen = (roll(before) + w1, order, [before[0], after[0]], "both agree")
                    break
        if chosen is None:
            # They don't line up (gaps, or a roll boundary between them). Take the direction from the
            # numbers themselves and trust the closer anchor, skipping any frame outside a roll.
            order = ("asc" if before[2] < after[2] else "desc" if before[2] > after[2]
                     else args.scan_order)
            near = sorted([(before[1], True, before), (after[1], False, after)], key=lambda x: x[0])
            opts = [(d, roll(a) + guess(a, b, order), a) for d, b, a in near if in_roll(guess(a, b, order))]
            if len(opts) == 2 and opts[0][0] == opts[1][0] and opts[0][1] != opts[1][1]:
                detail["neighbours"] = (f"{ctx.title(before[0])} (-{before[1]}) and {ctx.title(after[0])} "
                                        f"(+{after[1]}) disagree and are equally close")
                return None
            if opts:
                d, w, a = opts[0]
                chosen = (w, order, [a[0]], f"closest of two that disagree ({ctx.title(a[0])}, {d} away)")
    elif before or after:
        if not args.one_sided:
            detail["neighbours"] = "only one numeric neighbour (use --one-sided to accept)"
            return None
        a, is_before = (before, True) if before else (after, False)
        ok_orders = [o for o in (args.scan_order, "desc" if args.scan_order == "asc" else "asc")
                     if in_roll(guess(a, is_before, o))]
        if ok_orders:   # a frame outside a roll rules an order out; if both fit, use --scan-order
            chosen = (roll(a) + guess(a, is_before, ok_orders[0]), ok_orders[0], [a[0]], "one side only")
    else:
        detail["neighbours"] = f"no numeric title within {args.neighbour_steps} photos either way"
        return None
    if chosen is None:
        detail["neighbours"] = (f"every frame worked out from the neighbours is outside 1-{args.max_frame} "
                                f"(neighbours {before and ctx.title(before[0])} -{before and before[1]}, "
                                f"{after and ctx.title(after[0])} +{after and after[1]})")
        return None
    wanted, order, anchors, how = chosen
    detail.update(wanted_number=wanted, scan_order=order, anchors=anchors, neighbour_match=how)

    names = [ctx.title(a) for a in anchors]
    # The anchors used must be in the same folder (one roll); the wanted file must be in it too.
    dirs = None
    for aid in anchors:
        ds = {d for d, _ in film.find(numeric_title({"title": ctx.title(aid)}))}
        dirs = ds if dirs is None else dirs & ds
    if not dirs:
        detail["neighbours"] = f"no folder under {film.host} contains {' and '.join(names)}"
        return None
    targets = [(d, fn) for d, fn in film.find(wanted) if d in dirs]
    return resolve_files(p, targets, im, args, detail, f"from {' and '.join(names)} -> {wanted}")


def process(p, im, backup, ctx, args):
    """Return (status, detail). Statuses: favourited/would_favourite/already_favourite/
    not_found/ambiguous/no_date/error."""
    title = p.get("title", "")
    detail = {"title": title}
    if str(p.get("datetakenunknown", "0")) == "1":
        # No date taken (no EXIF date): typical of film scans. Flickr's date would only be the upload
        # time, so don't search by time; the photostream neighbours are the only clue.
        items = None
        detail["note"] = "flickr has no date taken"
    else:
        items = candidates_by_time(p, im, backup, args, detail)
        if items is None:
            detail["note"] = "no usable date from flickr or the backup"
    if items:
        if len(items) > 1 and norm(title):
            named = [a for a in items if norm(a.get("originalFileName")) == norm(title)]
            if named:
                items = named
                detail["name_match"] = True
        if len(items) == 1:
            return finish(items, detail, im, args)
        asset, res = resolve_tie(items, args, detail, f"{len(items)} immich assets at that time")
        return res if res else finish(asset if isinstance(asset, list) else [asset], detail, im, args)

    # Neighbours are only for photos with no usable date taken. If there was a date, the time
    # search is authoritative: the photo isn't a roll-sequence scan, so don't guess from neighbours.
    if ctx is not None and items is None:
        asset = via_neighbours(p, ctx, im, args, detail)
        if asset:
            detail["time_from"] = "photostream neighbours"
            return finish([asset], detail, im, args)
    return ("no_date" if items is None else "not_found"), detail


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=os.environ.get("IMMICH_URL", "http://localhost:2283/api"),
                    help="Immich API base URL (ends in /api)")
    ap.add_argument("--api-key", default=os.environ.get("IMMICH_API_KEY"), help="Immich API key")
    ap.add_argument("--web-url", help="Immich web UI base URL for links (default: --url without /api)")
    ap.add_argument("--flickr-key", default=os.environ.get("FLICKR_API_KEY"), help="Flickr API key")
    ap.add_argument("--flickr-secret", default=os.environ.get("FLICKR_API_SECRET"),
                    help="Flickr API secret (OAuth only)")
    ap.add_argument("--flickr-token", default=os.environ.get("FLICKR_OAUTH_TOKEN"),
                    help="Flickr OAuth token (OAuth only)")
    ap.add_argument("--flickr-token-secret", default=os.environ.get("FLICKR_OAUTH_SECRET"),
                    help="Flickr OAuth token secret (OAuth only)")
    ap.add_argument("--flickr-user", required=True,
                    help="your Flickr NSID, alias or profile URL ('me' with OAuth)")
    ap.add_argument("--tag", default="myfavs", help="Flickr tag to favourite (default: myfavs)")
    ap.add_argument("--apply", action="store_true", help="actually write to Immich (default: dry run)")
    ap.add_argument("--date-window", type=float, default=1.0, help="time tolerance in seconds")
    ap.add_argument("--max-tz-offset", type=float, default=14.0,
                    help="max camera UTC offset (hours) considered when time-searching")
    ap.add_argument("--no-ratio", action="store_true", help="don't filter on picture shape")
    ap.add_argument("--film-root", default="/btsync/backup/Film",
                    help="host folder holding the numbered film scans ('' disables neighbour lookup)")
    ap.add_argument("--film-container", default="/external/older/backup/Film",
                    help="the same folder as Immich sees it")
    ap.add_argument("--no-neighbours", dest="neighbours", action="store_false",
                    help="don't try to work out renamed film scans from neighbouring photos")
    ap.add_argument("--scan-order", choices=["asc", "desc"], default="asc",
                    help="do scan numbers increase (asc) or decrease along the stream order?")
    ap.add_argument("--max-frame", type=int, default=40,
                    help="highest frame number on a roll (a roll is 36-38 frames; frames are 1 or more)")
    ap.add_argument("--neighbour-steps", type=int, default=5,
                    help="how far to walk to find a photo whose title is just a number")
    ap.add_argument("--one-sided", action="store_true",
                    help="accept a guess from a numeric neighbour on one side only (default needs both "
                         "sides to agree)")
    ap.add_argument("--context-cache", default="flickr-context.json",
                    help="cache of photostream neighbours (delete it if you reorder your photostream)")
    ap.add_argument("--ties", choices=["pick", "skip", "all"], default="pick",
                    help="when several assets match equally: pick one (default), skip, or favourite all")
    ap.add_argument("--flickr-root", default="/mnt/disk/flickrBackup/jamie/",
                    help="flickr backup dir, used when the API has no capture time ('' to disable)")
    ap.add_argument("--flickr-cache", default="flickr-index.json", help="cache for the backup crawl")
    ap.add_argument("--reindex-flickr", action="store_true", help="re-crawl the flickr backup")
    ap.add_argument("--state", help="state file (default: flickr-favourites-<dryrun|apply>.jsonl)")
    ap.add_argument("--restart", action="store_true", help="ignore existing state and start over")
    ap.add_argument("--retry-errors", action="store_true", help="re-process entries that errored")
    ap.add_argument("--stats-every", type=int, default=25, help="print stats every N photos")
    ap.add_argument("--limit", type=int, help="only process the first N photos (for testing)")
    args = ap.parse_args()

    if not args.api_key:
        sys.exit("Set IMMICH_API_KEY or pass --api-key")
    if not args.flickr_key:
        sys.exit("Set FLICKR_API_KEY or pass --flickr-key")

    web_url = (args.web_url or args.url.rstrip("/").removesuffix("/api")).rstrip("/")
    mode = "apply" if args.apply else "dryrun"
    state_path = args.state or f"flickr-favourites-{mode}.jsonl"
    logf = open(f"flickr-favourites-{mode}.log", "a")
    log(f"mode={mode.upper()} tag={args.tag} state={state_path}", logf)

    done = {}
    if os.path.exists(state_path) and not args.restart:
        with open(state_path) as f:
            for line in f:
                try:
                    r = json.loads(line)
                    done[r["file"]] = r["status"]
                except Exception:
                    pass
        if args.retry_errors:
            done = {k: v for k, v in done.items() if v != "error"}
        log(f"resuming: {len(done)} photos already processed", logf)
    state = open(state_path, "a" if not args.restart else "w")

    fl = Flickr(args.flickr_key, args.flickr_secret, args.flickr_token, args.flickr_token_secret)
    if not fl.oauth:
        log("no Flickr OAuth: only public photos are visible to the API", logf)
    uid = fl.resolve_user(args.flickr_user)
    photos = fl.tagged(uid, args.tag, logf)
    if args.limit:
        photos = photos[:args.limit]
    total = len(photos)
    log(f"{total} photos tagged {args.tag!r}, {len(done)} already done", logf)

    backup = (BackupById(FlickrIndex(args.flickr_root.rstrip("/") or "/", args.flickr_cache,
                                     args.reindex_flickr, logf)) if args.flickr_root else None)
    ctx = Context(fl, args.context_cache) if args.neighbours and args.film_root else None
    args.film = FilmIndex(args.film_root, args.film_container, logf) if ctx else None
    im = Immich(args.url, args.api_key)
    stats = Counter()
    start, n_done = time.time(), 0
    try:
        for i, p in enumerate(photos, 1):
            pid = str(p["id"])
            if pid in done:
                continue
            try:
                status, detail = process(p, im, backup, ctx, args)
            except Exception as e:
                status, detail = "error", {"title": p.get("title"), "error": repr(e)}
            stats[status] += 1
            n_done += 1
            state.write(json.dumps({"file": pid, "status": status, **detail}) + "\n")
            state.flush()
            label = f"{pid} {detail.get('title')!r}"
            if status in ("ambiguous", "not_found", "no_date", "error"):
                log(f"[{i}/{total}] {status.upper()} {label} "
                    f"{detail.get('neighbours') or detail.get('note', detail.get('error', detail.get('time', '')))} "
                    f"https://www.flickr.com/photo.gne?id={pid}", logf)
                if status == "ambiguous":
                    for aid in detail.get("assets", []):
                        log(f"    {web_url}/photos/{aid}", logf)
            else:
                log(f"[{i}/{total}] {status} {label} via {detail.get('time_from')}"
                    f"{' +name' if detail.get('name_match') else ''}", logf)
            if n_done % args.stats_every == 0:
                if ctx:
                    ctx.save()
                rate = n_done / (time.time() - start)
                remaining = total - len(done) - n_done
                eta = timedelta(seconds=int(remaining / rate)) if rate else "?"
                log(f"STATS {dict(stats)} | {rate:.1f}/s | ETA {eta}", logf)
    except KeyboardInterrupt:
        log("interrupted - re-run the same command to resume", logf)

    if ctx:
        ctx.save()
    log(f"FINISHED this run: {dict(stats)}", logf)
    if not args.apply:
        log("dry run: nothing was written to Immich. Re-run with --apply.", logf)


if __name__ == "__main__":
    main()
