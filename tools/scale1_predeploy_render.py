#!/usr/bin/env python3
"""SCALE 1 — render a read-only query so it runs BEFORE the new views are deployed.

sql/scale1/fact_sku_daily_validation.sql addresses the new objects by their production names.
Until they are deployed those names do not resolve, so this tool replaces every reference to a
`pending_deploy` canonical object with a parenthesised copy of its canonical view body
(recursively), giving the same query text the owner can run read-only against production today.

Pure text transformation: no network, no credentials, no BigQuery. After deployment the
validation file runs as is and this tool is not needed.

usage: python tools/scale1_predeploy_render.py <query.sql>   (rendered SQL goes to stdout)
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CURRENT = ROOT / "sql" / "current"


def pending_bodies(root: Path = CURRENT) -> dict:
    """{`project.dataset.object`: canonical view body} for every pending_deploy object."""
    out = {}
    for manifest in sorted(root.glob("*/MANIFEST.json")):
        man = json.loads(manifest.read_text(encoding="utf-8"))
        for obj in man["objects"]:
            if obj["sync_state"] != "pending_deploy":
                continue
            sql = (ROOT / obj["canonical_path"]).read_text(encoding="utf-8")
            head, sep, body = sql.partition("\nAS\n")
            if not sep or "CREATE OR REPLACE VIEW" not in head:
                raise SystemExit(f"cannot locate the view body in {obj['canonical_path']}")
            ref = f"`{man['project']}.{obj['dataset']}.{obj['object_name']}`"
            out[ref] = body.strip().rstrip(";").strip()
    return out


def render(sql: str, bodies: dict) -> str:
    for _ in range(len(bodies) + 1):
        hits = [ref for ref in bodies if ref in sql]
        if not hits:
            return sql
        for ref in hits:
            sql = sql.replace(ref, f"(\n{bodies[ref]}\n)")
    raise SystemExit("pending objects reference each other cyclically")


def main(argv) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    sys.stdout.write(render(Path(argv[1]).read_text(encoding="utf-8"), pending_bodies()))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
