#!/usr/bin/env python3
"""Read rclone lsjson on stdin, print the name of the newest video file.
Used by the Dub-from-Drive workflow to pick which video to dub.
Prints an empty line if there is no video.
"""
import sys
import json

VIDEO_EXTS = (".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v", ".flv", ".ts")


def main() -> None:
    try:
        items = json.load(sys.stdin)
    except Exception:
        items = []
    vids = [
        i for i in items
        if not i.get("IsDir", False)
        and i.get("Name", "").lower().endswith(VIDEO_EXTS)
    ]
    vids.sort(key=lambda i: i.get("ModTime", ""), reverse=True)
    print(vids[0]["Name"] if vids else "")


if __name__ == "__main__":
    main()
