#!/usr/bin/env python3
"""Операторские переходы автомата арендатора (Tenancy T5, §L).

  python tools/tenancy/tenant_lifecycle.py status <tenant_id>
  python tools/tenancy/tenant_lifecycle.py bootstrap <tenant_id>              ∅ → CREDENTIALS_PENDING
  python tools/tenancy/tenant_lifecycle.py credentials-inserted <tenant_id>   → VALIDATING (версии секретов)
  python tools/tenancy/tenant_lifecycle.py approve-plan <tenant_id> --plan-hash H   → BACKFILLING
  python tools/tenancy/tenant_lifecycle.py suspend <tenant_id> --reason R
  python tools/tenancy/tenant_lifecycle.py resume <tenant_id>                 SUSPENDED|READY → VALIDATING
  python tools/tenancy/tenant_lifecycle.py reopen-chunk <tenant_id> --chunk ID --reason R   (ремонт DONE)

Только рёбра исполнителя OPERATOR и только через тот же валидатор, что у control
(lifecycle_core.decide). Команды «установить состояние» нет: READY ставит только control, и только
если выполнен весь контракт READY. Число версий секретов читается владельцем (gcloud), значения —
никогда.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.tenancy import tenant_tables as TT  # noqa: E402

import checkpoints as CK  # noqa: E402
import lifecycle_core as L  # noqa: E402

OPERATOR_TARGETS = {"bootstrap": L.CREDENTIALS_PENDING, "credentials-inserted": L.VALIDATING,
                    "approve-plan": L.BACKFILLING, "suspend": L.SUSPENDED, "resume": L.VALIDATING}


def secret_versions(project: str, secret_ids) -> dict:
    out = {}
    for sid in secret_ids:
        p = subprocess.run(["gcloud", "secrets", "versions", "list", sid, f"--project={project}",
                            "--filter=state=ENABLED", "--format=value(name)"], capture_output=True, text=True)
        if p.returncode != 0:
            raise TT.TableError(f"секрет {sid}: версии не прочитаны")
        out[sid] = len([x for x in p.stdout.splitlines() if x.strip()])
    return out


def build_snapshot(contract, tables, now, secret_counts=None, plan_hash=None):
    ops = contract["datasets"]["tenant_ops"]
    ledger = list(tables.rows(ops, "BACKFILL_CHECKPOINTS"))
    last = {}
    for r in ledger:
        last[r["backfill_id"]] = r
    chunks = [CK.Chunk(r["entity"], _d(r["window_from"]), _d(r["window_to"])) for r in last.values()]
    jobs = (contract["marketplaces"].get("ozon") or {}).get("jobs") or {}
    ents = tuple(sorted({e for j in jobs.values() for e in j["entities"]}))
    return L.Snapshot(tenant_id=contract["tenant_id"], now=now, events=list(tables.rows(ops, "TENANT_STATE_EVENTS")),
                      enabled_entities=ents, secret_versions=secret_counts or {},
                      plan_hash=CK.plan_hash(chunks) if chunks else None,
                      chunks={c: s["status"] for c, s in CK.fold(ledger).items()},
                      operator_plan_hash=plan_hash)


def _d(v):
    from datetime import date
    return date.fromisoformat(str(v))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("cmd", choices=["status", "reopen-chunk", *OPERATOR_TARGETS])
    ap.add_argument("tenant_id")
    ap.add_argument("--plan-hash")
    ap.add_argument("--reason")
    ap.add_argument("--chunk")
    a = ap.parse_args(argv)
    from tools.tenancy.tenant_infra import contract_for
    c = contract_for(a.tenant_id)
    t = TT.Tables(c["project_id"])
    now = datetime.now(timezone.utc)
    ops = c["datasets"]["tenant_ops"]
    if a.cmd == "status":
        events = list(t.rows(ops, "TENANT_STATE_EVENTS"))
        print(json.dumps({"state": L.current_state(events), "events": len(events),
                          "audit": L.audit_history(events)[:5]}, ensure_ascii=False))
        return 0
    actor = f"OPERATOR:{TT.owner_account()}"
    run_id = f"operator:{uuid.uuid4()}"
    if a.cmd == "reopen-chunk":
        if not (a.chunk and a.reason):
            print("REJECT: --chunk и --reason обязательны")
            return 1
        ledger = list(t.rows(ops, "BACKFILL_CHECKPOINTS"))
        st = CK.fold(ledger).get(a.chunk)
        if not st or st["status"] != "DONE":
            print(f"REJECT: отрезок {a.chunk} не DONE — ремонт не нужен")
            return 1
        src = [r for r in ledger if r["backfill_id"] == a.chunk][-1]
        t.append(ops, "BACKFILL_CHECKPOINTS", [dict(src, status="REOPENED", run_id=run_id, updated_at=now.isoformat(),
                                                    error_code="OPERATOR_REPAIR", error_detail=a.reason[:500])])
        print(f"REOPENED {a.chunk}")
        return 0
    counts = secret_versions(c["project_id"], (c["marketplaces"]["ozon"]["secret_ids"] or {}).values()) \
        if a.cmd == "credentials-inserted" else None
    s = build_snapshot(c, t, now, counts, a.plan_hash)
    status, failures, event = L.decide(s, OPERATOR_TARGETS[a.cmd], actor, a.reason or a.cmd.upper(),
                                       {"secret_versions": counts, "plan_hash": a.plan_hash}, run_id)
    print(f"{status}: {'; '.join(failures) or 'ok'}")
    if status == "WRITE":
        t.append(ops, "TENANT_STATE_EVENTS", [event])
    return 0 if status in ("WRITE", "NOOP") else 1


if __name__ == "__main__":
    sys.exit(main())
