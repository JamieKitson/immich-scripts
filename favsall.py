
#!/usr/bin/env python3

import os
import re
import csv
import json
import urllib.request
import urllib.error
from pathlib import Path
from collections import defaultdict

# --------------------------------------------------
# Configuration
# --------------------------------------------------

IMMICH_URL = "http://192.168.1.4:2283"
SOURCE_DIR = Path("/srv/http/192.168.1.4/resized/")
REPORT = Path("favourite_matches.csv")

API_KEY = os.environ.get("IMMICH_API_KEY")

PAGE_SIZE = 1000
BATCH_SIZE = 500

# --------------------------------------------------
# API helpers
# --------------------------------------------------

def api_request(method, endpoint, data=None):
    url = IMMICH_URL + "/api/" + endpoint

    headers = {
        "x-api-key": API_KEY,
        "Accept": "application/json",
    }

    body = None
    if data is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(data).encode()

    req = urllib.request.Request(
        url, data=body, headers=headers, method=method
    )

    with urllib.request.urlopen(req, timeout=120) as response:
        raw = response.read()
        return json.loads(raw) if raw else None


def get_all_assets():
    print("Retrieving Immich image metadata...")

    assets = []
    page = 1

    while True:
        result = api_request(
            "POST",
            "search/metadata",
            {
                "page": page,
                "size": PAGE_SIZE,
                "type": "IMAGE",
            }
        )

        # Immich search responses contain assets.items.
        # Also tolerate older response formats.
        asset_data = result.get("assets", result)

        if isinstance(asset_data, dict):
            items = asset_data.get("items", [])
            next_page = asset_data.get("nextPage")
        else:
            items = asset_data
            next_page = None

        assets.extend(items)

        print(f"  Retrieved {len(assets)} images")

        if not next_page or not items:
            break

        page += 1

    print(f"Total Immich images: {len(assets)}")
    return assets


# --------------------------------------------------
# Filename matching
# --------------------------------------------------

def normalise(filename):
    """
    Normalise filenames to help identify duplicates
    which have acquired suffixes such as (1), _2, -3.
    """
    stem = Path(filename).stem.lower().strip()

    # Remove common duplicate-number suffixes.
    stem = re.sub(r"\s*\(\d+\)$", "", stem)
    stem = re.sub(r"[_-]\d+$", "", stem)

    return stem


def main():
    if not API_KEY:
        raise SystemExit(
            "Please set the IMMICH_API_KEY environment variable."
        )

    if not SOURCE_DIR.is_dir():
        raise SystemExit(f"Directory not found: {SOURCE_DIR}")

    # Get source filenames (not recursive).
    source_files = sorted(
        p for p in SOURCE_DIR.iterdir() if p.is_file()
    )

    print(f"Source files: {len(source_files)}")

    assets = get_all_assets()

    # Build indexes of Immich assets.
    exact_index = defaultdict(list)
    normalised_index = defaultdict(list)

    for asset in assets:
        filename = asset.get("originalFileName")

        if not filename:
            continue

        exact_index[filename.lower()].append(asset)
        normalised_index[normalise(filename)].append(asset)

    # --------------------------------------------------
    # Match source files
    # --------------------------------------------------

    unique = []
    multiple = []
    missing = []

    report_rows = []

    for source in source_files:
        filename = source.name

        # Prefer exact filename matches.
        matches = exact_index.get(filename.lower(), [])
        match_type = "exact"

        # Otherwise try normalised filename.
        if not matches:
            matches = normalised_index.get(normalise(filename), [])
            match_type = "normalised"

        # Avoid counting the same asset twice.
        matches = list({a["id"]: a for a in matches}.values())

        if len(matches) == 1:
            unique.append((source, matches[0], match_type))
            status = "UNIQUE"

        elif len(matches) > 1:
            multiple.append((source, matches, match_type))
            status = "MULTIPLE"

        else:
            missing.append(source)
            status = "NOT FOUND"

        report_rows.append({
            "source_filename": filename,
            "status": status,
            "match_type": match_type if matches else "",
            "match_count": len(matches),
            "immich_filenames": "; ".join(
                a.get("originalFileName", "") for a in matches
            ),
            "immich_ids": "; ".join(a["id"] for a in matches),
        })

    # --------------------------------------------------
    # Report
    # --------------------------------------------------

    with REPORT.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "source_filename",
                "status",
                "match_type",
                "match_count",
                "immich_filenames",
                "immich_ids",
            ],
        )
        writer.writeheader()
        writer.writerows(report_rows)

    print("\n========== MATCHING RESULTS ==========")
    print(f"Unique matches:  {len(unique)}")
    print(f"Multiple matches: {len(multiple)}")
    print(f"No matches:      {len(missing)}")
    print(f"CSV report:      {REPORT.resolve()}")

    exact_count = sum(1 for _, _, t in unique if t == "exact")
    normalised_count = sum(1 for _, _, t in unique if t == "normalised")

    print("\nUnique match breakdown:")
    print(f"  Exact:       {exact_count}")
    print(f"  Normalised:  {normalised_count}")

    print("\nMultiple matches (first 20):")
    for source, matches, match_type in multiple[:20]:
        names = ", ".join(
            a.get("originalFileName", "") for a in matches
        )
        print(f"  {source.name} -> {names}")

    # --------------------------------------------------
    # Favourite assets (only with --apply)
    # --------------------------------------------------

    if "--apply" not in os.sys.argv:
        print("\nDRY RUN: Nothing has been changed.")
        print("Review the CSV, then run with --apply to favourite")
        print("all uniquely matched images.")
        return

    to_favourite = [
        asset["id"]
        for _, asset, _ in unique
        if not asset.get("isFavorite", False)
    ]

    print(f"\nAlready favourites: {len(unique) - len(to_favourite)}")
    print(f"To be favourited:   {len(to_favourite)}")

    if not to_favourite:
        print("Nothing to update.")
        return

    print("\nUpdating Immich...")

    for i in range(0, len(to_favourite), BATCH_SIZE):
        batch = to_favourite[i:i + BATCH_SIZE]

        api_request(
            "PUT",
            "assets",
            {
                "ids": batch,
                "isFavorite": True,
            }
        )

        print(
            f"  Updated {min(i + len(batch), len(to_favourite))}"
            f" / {len(to_favourite)}"
        )

    print("\nFinished favouriting images.")


if __name__ == "__main__":
    main()

