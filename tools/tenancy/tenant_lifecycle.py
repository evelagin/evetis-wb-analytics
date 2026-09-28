#!/usr/bin/env python3
"""Операторские переходы автомата арендатора (Tenancy T5, §L) — только владелец.

  python tools/tenancy/tenant_lifecycle.py status <tenant_id>
  python tools/tenancy/tenant_lifecycle.py bootstrap <tenant_id>              ∅ → CREDENTIALS_PENDING
  python tools/tenancy/tenant_lifecycle.py credentials-inserted <tenant_id>   → VALIDATING (версии секретов)
  python tools/tenancy/tenant_lifecycle.py approve-plan <tenant_id> --plan-hash H   → BACKFILLING
  python tools/tenancy/tenant_lifecycle.py suspend <tenant_id> --reason R
  python tools/tenancy/tenant_lifecycle.py resume <tenant_id>                 SUSPENDED|READY → VALIDATING
  python tools/tenancy/tenant_lifecycle.py reopen-chunk <tenant_id> --chunk ID --reason R   (ремонт DONE)

Полномочия владельца — строка решения в ref.OPERATOR_DECISIONS: туда пишет только владелец (у control и
runtime права записи в ref нет), и ребро оператора без решения аудит отвергает. Переход занимает номер
seq созданием маркера tenant_locks.S_<seq> (409 — параллельный переход, запись отменена); порядок — по
seq, а не по часам ноутбука. Ремонт отрезка — решение REOPEN_CHUNK со ссылкой на run_id отменяемой
версии DONE (часы не участвуют).

Только рёбра исполнителя OPERATOR и только через тот же валидатор, что у control
(lifecycle_core.decide). Команды «установить состояние» нет: READY ставит только control, и только
если выполнен весь контракт READY. Число версий секретов читается владельцем (gcloud), значения —
никогда.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.tenancy import tenant_tables as TT  # noqa: E402

import checkpoints as CK  # noqa: E402
import identity as I  # noqa: E402
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


def read_state(contract, tables):
    """(цепочка из маркеров, решения владельца, журнал чекпойнтов)."""
    ops, ref, locks = (contract["datasets"][k] for k in ("tenant_ops", "ref", "tenant_locks"))
    chain = L.chain_from_markers(tables.list_tables(locks))
    decisions = {d["decision_id"]: d for d in tables.rows(ref, "OPERATOR_DECISIONS") if d.get("decision_id")}
    ledger = list(tables.rows(ops, "BACKFILL_CHECKPOINTS"))
    return chain, decisions, ledger


def reopened(decisions):
    return frozenset(d.get("reopens_run_id") for d in decisions.values()
                     if d.get("decision_type") == "REOPEN_CHUNK" and d.get("reopens_run_id"))


def operator_binding(contract, tables, now, ents):
    """Привязка глазами оператора: действующее подтверждение в ref, согласованное с наблюдением.

    Живой отпечаток оператор не снимает (для этого нужен ключ Ozon). Его перепроверяют control
    перед каждой арендой отрезка и runtime перед каждой загрузкой (TOCTOU). Вердикт ключа —
    последняя проверка control (CAPABILITY_PROFILE), а не допущение.
    """
    ops, ref = contract["datasets"]["tenant_ops"], contract["datasets"]["ref"]
    rows = list(tables.rows(ref, "SELLER_BINDING"))
    obs = list(tables.rows(ops, "SELLER_IDENTITY_OBSERVATIONS"))
    apis = [I.SELLER] + ([I.PERFORMANCE] if any(e in L.ADS_DOMAINS for e in ents) else [])
    binding = {}
    for api in apis:
        st = I.effective_binding(rows, api, now, obs).status
        binding[api] = "BOUND" if st == "CONFIRMED" else st
    caps = {}
    for r in tables.rows(ops, "CAPABILITY_PROFILE"):
        k = (r.get("api"), r.get("capability"))
        if k not in caps or (I.as_utc(r.get("discovered_at")) or L._EPOCH) >= (I.as_utc(caps[k].get("discovered_at")) or L._EPOCH):
            caps[k] = r
    cred = {"seller": {"status": "PASS" if (caps.get(("seller", "credential_read_only")) or {}).get("status") == "AVAILABLE" else "FAIL"}}
    if I.PERFORMANCE in apis:
        cred["performance"] = {"status": "PASS" if (caps.get(("performance", "credential")) or {}).get("status") == "AVAILABLE" else "FAIL"}
    return binding, cred


def build_snapshot(contract, tables, now, secret_counts=None, plan_hash=None, extra_decision=None):
    chain, decisions, ledger = read_state(contract, tables)
    if extra_decision:
        decisions = dict(decisions, **{extra_decision["decision_id"]: extra_decision})
    jobs = (contract["marketplaces"].get("ozon") or {}).get("jobs") or {}
    ents = tuple(sorted({e for j in jobs.values() for e in j["entities"]}))
    ph, chunks = CK.latest_plan(ledger)
    folded = CK.fold(ledger, reopened(decisions))
    binding, cred = operator_binding(contract, tables, now, ents) if plan_hash else ({}, {})
    return L.Snapshot(tenant_id=contract["tenant_id"], now=now, events=chain, decisions=decisions,
                      enabled_entities=ents, secret_versions=secret_counts or {}, binding=binding, credentials=cred,
                      plan_hash=ph if chunks else None,
                      chunks={c.chunk_id: (folded.get(c.chunk_id) or {}).get("status", "PENDING") for c in chunks},
                      operator_plan_hash=plan_hash)


def new_decision(actor, now, **fields):
    base = {"decision_type": "TRANSITION", "expect_state": None, "to_state": None, "plan_hash": None,
            "secret_versions_json": None, "chunk_id": None, "reopens_run_id": None, "reason": None,
            "actor": actor, "decided_at": now.isoformat()}
    base.update(fields)
    base["decision_id"] = "dec-" + hashlib.sha256(json.dumps(base, sort_keys=True).encode() + uuid.uuid4().bytes).hexdigest()[:24]
    return base


def transition(contract, tables, now, target, actor, reason, plan_hash=None, secret_counts=None):
    """(статус, текст). Решение → ref, затем маркер S_<seq> (CAS), затем зеркало в tenant_ops."""
    chain, _d, _l = read_state(contract, tables)
    dec = new_decision(actor, now, expect_state=L.current_state(chain), to_state=target, plan_hash=plan_hash,
                       reason=reason,
                       secret_versions_json=json.dumps(secret_counts, sort_keys=True) if secret_counts else None)
    s = build_snapshot(contract, tables, now, secret_counts, plan_hash, extra_decision=dec)
    status, failures, event = L.decide(s, target, actor, reason or target, {
        "secret_versions": secret_counts, "plan_hash": plan_hash}, f"operator:{uuid.uuid4()}",
        decision_id=dec["decision_id"])
    if status != "WRITE":
        return status, "; ".join(failures) or "ok"
    ref, locks, ops = (contract["datasets"][k] for k in ("ref", "tenant_locks", "tenant_ops"))
    tables.append(ref, "OPERATOR_DECISIONS", [dec])
    if not tables.create_marker(locks, L.marker_name(event["seq"]), L.marker_labels(event),
                                json.dumps({"reason_code": event["reason_code"], "run_id": event["run_id"]})):
        return "CONFLICT", f"номер {event['seq']} занят параллельным переходом — повторите после status"
    tables.append(ops, "TENANT_STATE_EVENTS", [event])
    return "WRITE", f"{event['from_state']} → {event['to_state']} (seq {event['seq']}, {dec['decision_id']})"


def reopen_chunk(contract, tables, now, actor, chunk_id, reason):
    _chain, decisions, ledger = read_state(contract, tables)
    st = CK.fold(ledger, reopened(decisions)).get(chunk_id)
    if not st or st["status"] != "DONE" or not st.get("run_id"):
        return "REJECT", f"отрезок {chunk_id} не DONE — ремонт не нужен"
    dec = new_decision(actor, now, decision_type="REOPEN_CHUNK", chunk_id=chunk_id, reopens_run_id=st["run_id"],
                       reason=reason[:500])
    tables.append(contract["datasets"]["ref"], "OPERATOR_DECISIONS", [dec])
    return "WRITE", f"REOPEN {chunk_id}: отменена версия DONE {st['run_id']} ({dec['decision_id']})"


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
    if a.cmd == "status":
        chain, decisions, _l = read_state(c, t)
        print(json.dumps({"state": L.current_state(chain), "events": len(chain),
                          "audit": L.audit_history(chain, decisions)[:5]}, ensure_ascii=False))
        return 0
    actor = f"OPERATOR:{TT.owner_account()}"
    if a.cmd == "reopen-chunk":
        if not (a.chunk and a.reason):
            print("REJECT: --chunk и --reason обязательны")
            return 1
        status, text = reopen_chunk(c, t, now, actor, a.chunk, a.reason)
    else:
        if a.cmd == "suspend" and not a.reason:
            print("REJECT: приостановка без --reason")
            return 1
        counts = secret_versions(c["project_id"], (c["marketplaces"]["ozon"]["secret_ids"] or {}).values()) \
            if a.cmd == "credentials-inserted" else None
        status, text = transition(c, t, now, OPERATOR_TARGETS[a.cmd], actor, a.reason or a.cmd.upper(),
                                  a.plan_hash, counts)
    print(f"{status}: {text}")
    return 0 if status in ("WRITE", "NOOP") else 1


if __name__ == "__main__":
    sys.exit(main())
