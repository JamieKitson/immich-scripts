#!/usr/bin/env python3
"""Copy missing photos (everything except the Nikon D80) from the Flickr backup into the main library.

Flickr backup files look like  <root>/YYYY-MM/DD-HH-MM-<name>-<flickr id>.<ext>
e.g.  2007-03/29-12-52-IMG-1071-443720252.jpg

Which files are copied (date = capture time, taken from the folder + file name, or failing that
the EXIF date; the file's modified time is never used):

  * Outside 2006-12-18 .. 2007-05-21 (inclusive): never copied.
  * IMG_nnnn (Canon SD800):           copied for the whole range.
  * DSC_nnnn (Nikon D70s, or the D80 bought 2007-03-31, same naming):
                                      copied only before 2007-03-31.
  * Any other name: copied, unless the EXIF model is a Nikon D80 (skipped). Files with no
    camera EXIF cannot be shown to be D80, so they are copied. Before 2007-03-31 the D80 can't
    be involved, so EXIF isn't read at all.

Copies are renamed to  YYYY-MM-DD-HH-MM-<rest of name>, e.g. 2007-03-29-12-52-IMG-1071-443720252.jpg
Existing files in the destination are never overwritten.

Dry run unless --apply is given.

    pip install pillow
    ./flickr_recover.py --dest /path/to/library/recovered            # dry run
    ./flickr_recover.py --dest /path/to/library/recovered --apply
"""
import argparse
import csv
import os
import re
import shutil
import sys
from collections import Counter
from datetime import datetime

from PIL import Image

START = datetime(2006, 12, 18, 0, 0)
END = datetime(2007, 5, 21, 23, 59, 59)
D80_FROM = datetime(2007, 3, 31, 0, 0)  # from here on DSC_ files are the D80 (not wanted) and EXIF must be checked

NAME_RE = re.compile(r"^(\d\d)-(\d\d)-(\d\d)-(.+)-(\d+)\.([A-Za-z0-9]+)$")
MONTH_RE = re.compile(r"^(\d{4})-(\d\d)$")
CAMERA_NAME_RE = re.compile(r"^(IMG|DSC)[-_]\d{4}$", re.I)

T_MAKE, T_MODEL, T_DATETIME = 271, 272, 306
EXIF_IFD, T_DATETIME_ORIGINAL = 0x8769, 36867


def read_exif(path):
    """Return (make, model, datetime|None); empty strings / None when unreadable."""
    try:
        with Image.open(path) as im:
            ex = im.getexif()
            sub = ex.get_ifd(EXIF_IFD) if hasattr(ex, "get_ifd") else {}
            raw = sub.get(T_DATETIME_ORIGINAL) or ex.get(T_DATETIME)
            dt = None
            if raw:
                try:
                    dt = datetime.strptime(str(raw).strip()[:19], "%Y:%m:%d %H:%M:%S")
                except ValueError:
                    pass
            return (str(ex.get(T_MAKE) or "").strip("\x00 "),
                    str(ex.get(T_MODEL) or "").strip("\x00 "), dt)
    except Exception:
        return "", "", None


def parse_path(path, root):
    """Return (date|None, rest_of_name|None, camera_name|None) from the Flickr path."""
    folder = os.path.basename(os.path.dirname(path))
    m, fm = NAME_RE.match(os.path.basename(path)), MONTH_RE.match(folder)
    if not (m and fm):
        return None, None, None
    day, hh, mm, title, fid, ext = m.groups()
    try:
        dt = datetime(int(fm[1]), int(fm[2]), int(day), int(hh), int(mm))
    except ValueError:
        return None, None, None
    rest = f"{title}-{fid}.{ext}"
    return dt, rest, title if CAMERA_NAME_RE.match(title) else None


def decide(path, root):
    """Return (copy: bool, reason, date|None, new_name|None)."""
    dt, rest, cam_name = parse_path(path, root)
    make = model = ""
    exif_dt = None
    # EXIF is only needed for a missing date, or to rule out the D80 on non-camera names
    need_exif = dt is None or (cam_name is None and dt >= D80_FROM)
    if need_exif:
        make, model, exif_dt = read_exif(path)
    when = dt or exif_dt
    if when is None:
        return False, "no date (not in flickr layout, no EXIF date)", None, None
    if not START <= when <= END:
        return False, "outside date range", when, None

    if cam_name:
        prefix = cam_name[:3].upper()
        if prefix == "IMG":
            ok, reason = True, "IMG_ name (Canon)"
        elif when < D80_FROM:
            ok, reason = True, "DSC_ name before D80 (Nikon D70s)"
        else:
            return False, "DSC_ name after D80 bought", when, None
    else:
        low = f"{make} {model}".lower()
        if "d80" in low:
            return False, f"Nikon D80 (EXIF {model})", when, None
        if when < D80_FROM:
            ok, reason = True, "other name, before D80 (EXIF not checked)"
        else:
            ok, reason = True, f"EXIF {model}" if model else "other name, no camera EXIF"

    if rest is None:  # not in flickr layout, so build the name from the file name itself
        rest = os.path.basename(path)
    new_name = f"{when:%Y-%m-%d-%H-%M}-{rest}"
    return ok, reason, when, new_name


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--flickr-root", default="/mnt/disk/flickrBackup/jamie/", help="Flickr backup directory")
    ap.add_argument("--dest", required=True, help="Directory to copy the recovered files into")
    ap.add_argument("--apply", action="store_true", help="Actually copy (default is a dry run)")
    ap.add_argument("--report", default="flickr-recover.csv", help="CSV log of every decision")
    args = ap.parse_args()

    root = args.flickr_root
    if not os.path.isdir(root):
        sys.exit(f"Flickr root not found: {root}")

    # only crawl month folders that can overlap the range
    months = {(2006, 12), (2007, 1), (2007, 2), (2007, 3), (2007, 4), (2007, 5)}
    files = []
    for entry in sorted(os.listdir(root)):
        fm = MONTH_RE.match(entry)
        d = os.path.join(root, entry)
        if fm and (int(fm[1]), int(fm[2])) in months and os.path.isdir(d):
            files += [os.path.join(d, f) for f in sorted(os.listdir(d)) if os.path.isfile(os.path.join(d, f))]
        elif not fm and os.path.isdir(d):
            print(f"note: ignoring folder not named YYYY-MM: {d}")
    print(f"{len(files)} files in candidate month folders")

    if args.apply:
        os.makedirs(args.dest, exist_ok=True)

    stats = Counter()
    with open(args.report, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["status", "reason", "date", "source", "destination"])
        for path in files:
            ok, reason, when, new_name = decide(path, root)
            dest = os.path.join(args.dest, new_name) if ok else ""
            if ok and os.path.exists(dest):
                status, reason = "exists", "destination already exists"
            elif ok:
                status = "copied" if args.apply else "would_copy"
                if args.apply:
                    try:
                        shutil.copy2(path, dest)
                    except OSError as e:
                        status, reason = "error", str(e)
            else:
                status = "skipped"
            stats[status if status != "skipped" else f"skipped: {reason.split(':')[0]}"] += 1
            w.writerow([status, reason, when.isoformat(" ") if when else "", path, dest])
            if status in ("copied", "would_copy", "error"):
                print(f"{status}: {path} -> {dest}")

    print()
    for k, v in sorted(stats.items()):
        print(f"{v:6d}  {k}")
    print(f"\nFull log: {args.report}" + ("" if args.apply else "   (dry run; use --apply to copy)"))


if __name__ == "__main__":
    main()
