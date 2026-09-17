#!/usr/bin/env python3
"""
EVETIS · WB Executive V2 Phase B — ОТКАТ раскладки dashboard 2.

Восстанавливает ширину, параметры и все dashcards dashboard 2 из снимка, снятого до
Phase B: metabase/rollback/exec_v2_phase_b_before/dashboard-2-layout.json.

Карточки «Executive V2 · …» в коллекции 6 не удаляются (на старую раскладку они не
выведены); архивировать их — флаг --archive-v2-cards.
Старые карточки Phase B не менял, поэтому их восстанавливать не нужно.
BigQuery откат не затрагивает.

  tools/metabase_exec_v2_rollback.py --dry-run
  tools/metabase_exec_v2_rollback.py
  tools/metabase_exec_v2_rollback.py --archive-v2-cards
"""
import json, os, subprocess, sys

PROFILE = os.environ.get("MB_PROFILE", "evetis-dev")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SNAP = os.path.join(ROOT, "metabase/rollback/exec_v2_phase_b_before/dashboard-2-layout.json")


def mb(*args, body=None):
    cmd = ["mb", *args, "-p", PROFILE, "--json", "--max-bytes=0"]
    if body is not None:
        with open("/tmp/exec_v2_rollback_body.json", "w") as f:
            json.dump(body, f, ensure_ascii=False)
        cmd += ["--file", "/tmp/exec_v2_rollback_body.json"]
    out = subprocess.run(cmd, capture_output=True, text=True)
    if out.returncode != 0:
        raise SystemExit(f"mb {' '.join(args)} failed: {out.stdout[:600]} {out.stderr[:600]}")
    return json.loads(out.stdout) if out.stdout.strip() else None


snap = json.load(open(SNAP))
dashcards = []
for i, dc in enumerate(snap["dashcards"], 1):
    dc = dict(dc)
    dc["id"] = -i  # исходные dashcard-id удалены при замене раскладки — создаются заново
    dashcards.append(dc)
body = {"width": snap["width"], "parameters": snap["parameters"], "dashcards": dashcards}
print(f"dashboard {snap['id']}: восстановить {len(dashcards)} dashcards, width={snap['width']}")
if "--dry-run" in sys.argv:
    sys.exit(0)
mb("dashboard", "update", str(snap["id"]), body=body)
print("раскладка восстановлена")
if "--archive-v2-cards" in sys.argv:
    items = mb("collection", "items", "6")
    rows = items.get("data", items) if isinstance(items, dict) else items
    for r in rows:
        if r.get("model") == "card" and r.get("name", "").startswith("Executive V2 · "):
            mb("card", "update", str(r["id"]), body={"archived": True})
            print(f"archived {r['id']} {r['name']}")
