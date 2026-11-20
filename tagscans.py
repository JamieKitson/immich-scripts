#!/usr/bin/env python3

import os
import sys
import json
import urllib.request
import urllib.parse
import urllib.error

BASE_URL = "http://192.168.1.4:2283/api"
HOST_ROOT = "/mnt/sea5t/backup/Scans/Abba"
IMMICH_ROOT = "/external/newer/Scans/Abba"
TAG_NAME = "Abba Slide Scans"

API_KEY = os.environ.get("IMMICH_API_KEY")
if not API_KEY:
    sys.exit("IMMICH_API_KEY is not set")

def api(method, endpoint, data=None):
    url = BASE_URL + endpoint
    body = json.dumps(data).encode() if data is not None else None

    req = urllib.request.Request(
        url,
        data=body,
        method=method,
        headers={
            "x-api-key": API_KEY,
            "Content-Type": "application/json",
            "Accept": "application/json"
        }
    )

    try:
        with urllib.request.urlopen(req) as response:
            raw = response.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        print(f"API error {e.code}: {e.read().decode()}")
        raise

def get_assets(folder):
    path = urllib.parse.urlencode({"path": folder})
    result = api("GET", "/view/folder?" + path)

    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        if isinstance(result.get("assets"), list):
            return result["assets"]
        if isinstance(result.get("assets"), dict):
            return result["assets"].get("items", [])
        if isinstance(result.get("items"), list):
            return result["items"]

    raise ValueError(f"Unexpected folder response for {folder}")

def main():
    if not os.path.isdir(HOST_ROOT):
        sys.exit(f"Directory not found: {HOST_ROOT}")

    asset_ids = set()
    directories = 0

    print("Scanning directories and querying Immich...")

    for root, dirs, files in os.walk(HOST_ROOT):
        relative = os.path.relpath(root, HOST_ROOT)
        immich_folder = IMMICH_ROOT

        if relative != ".":
            immich_folder += "/" + relative.replace(os.sep, "/")

        directories += 1
        assets = get_assets(immich_folder)

        for asset in assets:
            original_path = asset.get("originalPath", "")

            # Safety check: only accept assets beneath our chosen root.
            if not original_path.startswith(IMMICH_ROOT + "/"):
                continue

            if asset.get("type") != "IMAGE":
                continue

            asset_ids.add(asset["id"])

    print()
    print(f"Directories checked: {directories}")
    print(f"Images found:        {len(asset_ids)}")
    print()

    if not asset_ids:
        print("No matching images found. Nothing to do.")
        return

    print("Sample matching files:")
    print("----------------------")

    # Show a sample from the root and nested folders.
    print(f"Found {len(asset_ids)} matching image assets.")
    print("The API returned the matching asset IDs successfully.")

    if "--apply" not in sys.argv:
        print()
        print("DRY RUN: No changes made.")
        print("Review the results, then run:")
        print("python3 ~/tag-film-scans.py --apply")
        return

    print()
    print(f"Applying tag: {TAG_NAME}")

    tags = api("GET", "/tags")
    tag = next(
        (t for t in tags if t.get("name") == TAG_NAME),
        None
    )

    if tag is None:
        tag = api("POST", "/tags", {"name": TAG_NAME})
        print("Created tag.")

    tag_id = tag["id"]

    ids = list(asset_ids)
    for start in range(0, len(ids), 500):
        batch = ids[start:start + 500]
        api(
            "PUT",
            f"/tags/{tag_id}/assets",
            {"ids": batch}
        )
        print(f"Tagged {min(start + len(batch), len(ids))}/{len(ids)}")

    print()
    print(f"Finished. Tagged {len(ids)} images.")

if __name__ == "__main__":
    main()


