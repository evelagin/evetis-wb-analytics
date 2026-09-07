#!/usr/bin/env python3
"""Добавляет запись о raw-файле в MANIFEST.csv.

RAW DATA IMMUTABILITY: сами файлы в raw/ после получения не редактируются.
Манифест — единственное, что дописывается.

usage: _manifest_add.py <path> <source> <requested_period> [row_count]
row_count: число, либо "auto" (json/csv), либо "n/a"
"""
import csv
import hashlib
import json
import os
import sys
from datetime import datetime, timezone, timedelta

MSK = timezone(timedelta(hours=3))
ROOT = os.path.dirname(os.path.abspath(__file__))
MANIFEST = os.path.join(ROOT, "MANIFEST.csv")
FIELDS = [
    "filename", "source", "extraction_timestamp_msk", "requested_period",
    "sha256", "row_count", "size_bytes",
]


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def auto_rows(path):
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == ".json":
            d = json.load(open(path, encoding="utf-8"))
            for keys in (("items",), ("result", "items"), ("rows",)):
                cur = d
                for k in keys:
                    cur = cur.get(k) if isinstance(cur, dict) else None
                    if cur is None:
                        break
                if isinstance(cur, list):
                    return len(cur)
            return len(d) if isinstance(d, list) else "n/a"
        if ext == ".csv":
            with open(path, encoding="utf-8") as f:
                return max(sum(1 for _ in f) - 1, 0)
    except Exception:
        return "n/a"
    return "n/a"


def main():
    path, source, period = sys.argv[1], sys.argv[2], sys.argv[3]
    rows = sys.argv[4] if len(sys.argv) > 4 else "auto"
    if rows == "auto":
        rows = auto_rows(path)
    ts = datetime.fromtimestamp(os.path.getmtime(path), MSK).isoformat()
    rel = os.path.relpath(os.path.abspath(path), ROOT)

    existing = []
    if os.path.exists(MANIFEST):
        existing = list(csv.DictReader(open(MANIFEST, encoding="utf-8")))
    existing = [r for r in existing if r["filename"] != rel]
    existing.append({
        "filename": rel, "source": source, "extraction_timestamp_msk": ts,
        "requested_period": period, "sha256": sha256(path),
        "row_count": rows, "size_bytes": os.path.getsize(path),
    })
    existing.sort(key=lambda r: r["filename"])
    with open(MANIFEST, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(existing)
    print(f"manifest: {rel} rows={rows} size={os.path.getsize(path)}")


if __name__ == "__main__":
    main()
