#!/usr/bin/env python3
"""Copy missing Huawei P9 photos from the Flickr backup into the main library (name-based only).

Flickr backup files look like  <root>/YYYY-MM/DD-HH-MM-<title>-<flickr id>.<ext>

Date range (from the folder + file name, never the modified time): 2017-04-22 .. 2017-05-10 inclusive.
In that range:

  * IMG-YYYYMMDD-HHMMSS   (P9 camera, e.g. 14-11-09-IMG-20170514-110903-34561553701.jpg)  -> copy
  * IMG-YYYYMMDD-WAnnnn   (WhatsApp)                                                       -> skip
  * Gruffalo-...          (e.g. 23-15-20-Gruffalo-03-20-39-23-04-17-33437788844.png)       -> copy
  * VID-...               none expected                                                    -> WARNING (the .jpg is a
                          stand-in for a video; the last number is its Flickr ID). With --download-videos
                          the original video is fetched via the Flickr API (see below).
  * anything else                                                                          -> skipped, logged

Copies are renamed to  YYYY-MM-DD-HH-MM-<rest of name>. Existing files in the destination are never
overwritten. Dry run unless --apply is given. Keep this next to flickr_recover.py (it shares the
filename pattern).

--download-videos: for each VID stand-in, asks Flickr (flickr.photos.getSizes / getInfo) for the
original video and saves it as YYYY-MM-DD-HH-MM-VID-...-<id>.<ext> in --dest. The API lookup also
happens in a dry run (it is read-only), so a dry run shows whether originals are available to you,
or which sizes Flickr offers instead. Transcoded copies (HD/Site/Mobile MP4) are only used with
--allow-transcoded. Needs  pip install requests requests-oauthlib  and, as for flickr_favourites.py,
FLICKR_API_KEY, FLICKR_API_SECRET, FLICKR_OAUTH_TOKEN, FLICKR_OAUTH_SECRET (see flickr_auth.py).

    ./flickr_recover_p9.py --dest /path/to/library/recovered            # dry run
    ./flickr_recover_p9.py --dest /path/to/library/recovered --apply
"""
import argparse
import csv
import os
import re
import shutil
import sys
from collections import Counter
from datetime import datetime

from flickr_recover import MONTH_RE, NAME_RE

START = datetime(2017, 4, 22, 0, 0)
END = datetime(2017, 5, 10, 23, 59, 59)
MONTHS = {(2017, 4), (2017, 5)}

P9_RE = re.compile(r"^IMG[-_]\d{8}[-_]\d{6}$", re.I)
WA_RE = re.compile(r"^(IMG|VID)[-_]\d{8}[-_]WA\d+$", re.I)
GRUFFALO_RE = re.compile(r"^Gruffalo\b", re.I)
VID_RE = re.compile(r"^VID\b", re.I)


def decide(path):
    """Return (status, reason, date|None, new_name|None); status is copy/skip/warn."""
    folder = os.path.basename(os.path.dirname(path))
    m, fm = NAME_RE.match(os.path.basename(path)), MONTH_RE.match(folder)
    if not (m and fm):
        return "skip", "not in flickr layout", None, None
    day, hh, mm, title, fid, ext = m.groups()
    try:
        when = datetime(int(fm[1]), int(fm[2]), int(day), int(hh), int(mm))
    except ValueError:
        return "skip", "invalid date in path", None, None
    if not START <= when <= END:
        return "skip", "outside date range", when, None
    if WA_RE.match(title):
        return "skip", "WhatsApp image/video", when, None
    if VID_RE.match(title):
        return "warn", "VID file in range (unexpected)", when, None
    if P9_RE.match(title):
        reason = "P9 IMG name"
    elif GRUFFALO_RE.match(title):
        reason = "Gruffalo image"
    else:
        return "skip", "other name", when, None
    return "copy", reason, when, f"{when:%Y-%m-%d-%H-%M}-{title}-{fid}.{ext}"


ID_RE = re.compile(r"-(\d+)\.[A-Za-z0-9]+$")
VIDEO_EXT = {"video/quicktime": "mov", "video/mp4": "mp4", "video/x-msvideo": "avi",
             "video/x-m4v": "m4v", "video/3gpp": "3gp"}
TRANSCODED = ["HD MP4", "Site MP4", "Mobile MP4"]


def flickr_client():
    import flickr_favourites  # lazy: pulls in requests/pillow only when videos are wanted
    env = os.environ
    need = ["FLICKR_API_KEY", "FLICKR_API_SECRET", "FLICKR_OAUTH_TOKEN", "FLICKR_OAUTH_SECRET"]
    missing = [k for k in need if not env.get(k)]
    if missing:
        sys.exit("--download-videos needs these environment variables: " + ", ".join(missing))
    return flickr_favourites.Flickr(env["FLICKR_API_KEY"], env["FLICKR_API_SECRET"],
                                    env["FLICKR_OAUTH_TOKEN"], env["FLICKR_OAUTH_SECRET"])


def video_candidates(fl, pid, allow_transcoded):
    """Return (list of (label, url, ext|None), note). Original first; note says what Flickr offers."""
    sizes = {str(x["label"]): x for x in fl.call("flickr.photos.getSizes", photo_id=pid)["sizes"]["size"]}
    cands = []
    if "Video Original" in sizes:
        cands.append(("Video Original", sizes["Video Original"]["source"], None))
    try:
        info = fl.call("flickr.photos.getInfo", photo_id=pid)["photo"]
        sec, fmt, owner = info.get("originalsecret"), info.get("originalformat"), info["owner"]["nsid"]
        if sec and info.get("media") == "video":
            cands.append(("play/orig", f"https://www.flickr.com/photos/{owner}/{pid}/play/orig/{sec}/", fmt))
    except Exception:
        pass
    if allow_transcoded:
        cands += [(l, sizes[l]["source"], "mp4") for l in TRANSCODED if l in sizes]
    return cands, "flickr offers: " + ", ".join(sizes)


def download(fl, url, dest_base, ext):
    """Download url to dest_base.<ext>; returns the final path, or None if Flickr served a web page."""
    import requests
    for session in (requests, fl.s):  # try unauthenticated, then OAuth-signed
        try:
            r = session.get(url, stream=True, allow_redirects=True, timeout=120)
        except requests.RequestException:
            continue
        ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
        if r.status_code != 200 or ctype.startswith("text/") or "json" in ctype:
            r.close()
            continue
        path = f"{dest_base}.{ext or VIDEO_EXT.get(ctype, 'mp4')}"
        if os.path.exists(path):
            r.close()
            return path, True
        with open(path + ".part", "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
        os.replace(path + ".part", path)
        return path, False
    return None, False


def handle_video(fl, pid, path, when, args):
    """Try to get the original video for a VID stand-in. Returns (status, reason, destination)."""
    title_id = os.path.basename(path)[9:].rsplit(".", 1)[0]  # strip DD-HH-MM-, extension
    base = os.path.join(args.dest, f"{when:%Y-%m-%d-%H-%M}-{title_id}")
    try:
        cands, note = video_candidates(fl, pid, args.allow_transcoded)
        if not cands:
            print(f"  video {pid}: no original available ({note})")
            return "video_unavailable", note, ""
        if not args.apply:
            print(f"  video {pid}: would download {cands[0][0]}")
            return "would_download_video", cands[0][0], base
        for label, url, ext in cands:
            got, existed = download(fl, url, base, ext)
            if got:
                if not existed:  # Immich falls back to mtime when a video has no creation metadata
                    ts = when.timestamp()
                    os.utime(got, (ts, ts))
                print(f"  video {pid}: {'already there' if existed else 'downloaded'} {label} -> {got}")
                return ("exists" if existed else "video_downloaded"), label, got
        print(f"  video {pid}: download failed for all of {[c[0] for c in cands]} ({note})")
        return "video_unavailable", f"download refused; {note}", ""
    except Exception as e:
        print(f"  video {pid}: error {e}")
        return "error", str(e), ""


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--flickr-root", default="/mnt/disk/flickrBackup/jamie/", help="Flickr backup directory")
    ap.add_argument("--dest", required=True, help="Directory to copy the recovered files into")
    ap.add_argument("--apply", action="store_true", help="Actually copy (default is a dry run)")
    ap.add_argument("--download-videos", action="store_true",
                    help="Fetch the original video for each VID stand-in from the Flickr API")
    ap.add_argument("--allow-transcoded", action="store_true",
                    help="With --download-videos, accept Flickr's HD/Site/Mobile MP4 if no original is available")
    ap.add_argument("--report", default="flickr-recover-p9.csv", help="CSV log of every decision")
    args = ap.parse_args()

    if not os.path.isdir(args.flickr_root):
        sys.exit(f"Flickr root not found: {args.flickr_root}")

    files = []
    for entry in sorted(os.listdir(args.flickr_root)):
        fm = MONTH_RE.match(entry)
        d = os.path.join(args.flickr_root, entry)
        if fm and (int(fm[1]), int(fm[2])) in MONTHS and os.path.isdir(d):
            files += [os.path.join(d, f) for f in sorted(os.listdir(d)) if os.path.isfile(os.path.join(d, f))]
    print(f"{len(files)} files in candidate month folders")

    if args.apply:
        os.makedirs(args.dest, exist_ok=True)
    fl = flickr_client() if args.download_videos else None

    stats = Counter()
    with open(args.report, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["status", "reason", "date", "source", "destination"])
        for path in files:
            decision, reason, when, new_name = decide(path)
            dest = os.path.join(args.dest, new_name) if decision == "copy" else ""
            if decision == "copy" and os.path.exists(dest):
                status, reason = "exists", "destination already exists"
            elif decision == "copy":
                status = "copied" if args.apply else "would_copy"
                if args.apply:
                    try:
                        shutil.copy2(path, dest)
                    except OSError as e:
                        status, reason = "error", str(e)
            elif decision == "warn":
                status = "warning"
                print(f"WARNING: {reason}: {path}")
                im = ID_RE.search(path)
                if fl and im:
                    status, reason, dest = handle_video(fl, im[1], path, when, args)
            else:
                status = "skipped"
            stats[status if status != "skipped" else f"skipped: {reason}"] += 1
            w.writerow([status, reason, when.isoformat(" ") if when else "", path, dest])
            if status in ("copied", "would_copy", "error"):
                print(f"{status}: {path} -> {dest}")

    print()
    for k, v in sorted(stats.items()):
        print(f"{v:6d}  {k}")
    print(f"\nFull log: {args.report}" + ("" if args.apply else "   (dry run; use --apply to copy)"))


if __name__ == "__main__":
    main()
