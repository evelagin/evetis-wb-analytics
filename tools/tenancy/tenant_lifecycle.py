#!/usr/bin/env python3
"""Операторские переходы автомата арендатора (Tenancy T5, §L) — только владелец.

  python tools/tenancy/tenant_lifecycle.py status <tenant_id>
  python tools/tenancy/tenant_lifecycle.py bootstrap <tenant_id>              ∅ → CREDENTIALS_PENDING
  python tools/tenancy/tenant_lifecycle.py credentials-inserted <tenant_id>   → VALIDATING (версии секретов)
  python tools/tenancy/tenant_lifecycle.py approve-plan <tenant_id> --plan-hash H   → BACKFILLING
  python tools/tenancy/tenant_lifecycle.py suspend <tenant_id> --reason R
  python tools/tenancy/tenant_lifecycle.py resume <tenant_id>                 SUSPENDED|READY → VALIDATING
  python tools/tenancy/tenant_lifecycle.py reopen-chunk <tenant_id> --chunk ID --reason R   (ремонт DONE)
  python tools/tenancy/tenant_lifecycle.py accept-limitation <tenant_id> --domain D --boundary ID --reason R

Полномочия владельца — таблицы-знаки в ref, куда у control и runtime нет права создавать таблицы:
  * ref.OPD_<seq> — решение на ребро оператора с номером seq (метки to/from, описание — хеш плана и
    т. п.); привязано к номеру, повторно не предъявляется; затем маркер tenant_locks.S_<seq>;
  * ref.OPH_<n> — стоп-кран: suspend действует на control и runtime сразу, независимо от журнала;
  * строки ref.OPERATOR_DECISIONS — зеркало решений и решения, которым задержка чтения не опасна:
    REOPEN_CHUNK (ремонт DONE по run_id) и ACCEPT_LIMITATION (принять PARTIAL-границу истории).
Порядок переходов — по seq, а не по часам ноутбука.

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
    """(цепочка из маркеров, решения владельца по seq, журнал чекпойнтов, строки решений, стоп-кран)."""
    ops, ref, locks = (contract["datasets"][k] for k in ("tenant_ops", "ref", "tenant_locks"))
    chain = L.chain_from_markers(tables.list_tables(locks, with_expiry=True))
    decisions = {}
    for name, labels, desc in tables.series(ref, "OPD_"):
        m = L.DECISION_MARKER_RE.match(name)
        if m:
            decisions[int(m.group(1))] = L.decision_from_marker(labels, desc)
    rows = [d for d in tables.rows(ref, "OPERATOR_DECISIONS") if d.get("decision_id")]
    # Ремонт отрезков — знаки ref.OPR_<n> (консистентно); строки REOPEN_CHUNK — зеркало.
    rows += [{"decision_type": "REOPEN_CHUNK", "reopens_run_id": rid}
             for rid in reopened_from_signs(tables.series(ref, "OPR_"))]
    hold = L.hold_active([(n, lb) for n, lb, _d in tables.series(ref, "OPH_")])
    ledger = list(tables.rows(ops, "BACKFILL_CHECKPOINTS"))
    return chain, decisions, ledger, rows, hold


def reopened_from_signs(items):
    """run_id из знаков ref.OPR_<n> (строгий разбор описания; испорченный знак — пропуск)."""
    from tools.tenancy.validation import TenantDocumentError, parse_tenant_json
    out = []
    for _n, _lb, desc in items:
        try:
            doc = parse_tenant_json(desc or "{}")
        except TenantDocumentError:
            continue
        if isinstance(doc, dict) and doc.get("reopens_run_id"):
            out.append(doc["reopens_run_id"])
    return out


def reopened(rows):
    return frozenset(d.get("reopens_run_id") for d in rows
                     if d.get("decision_type") == "REOPEN_CHUNK" and d.get("reopens_run_id"))


def operator_binding(contract, tables, now, ents):
    """Привязка глазами оператора: действующее подтверждение в ref, согласованное с наблюдением.

    Живой отпечаток оператор не снимает (для этого нужен ключ Ozon). Его перепроверяют control
    перед каждой арендой отрезка и runtime перед каждой загрузкой (TOCTOU). Вердикт ключа —
    последняя проверка control (CAPABILITY_PROFILE), а не допущение.
    """
    ops, ref = contract["datasets"]["tenant_ops"], contract["datasets"]["ref"]
    items = tables.series(ref, "OPB_")                  # знаки привязки владельца — консистентно
    obs = list(tables.rows(ops, "SELLER_IDENTITY_OBSERVATIONS"))
    apis = [I.SELLER] + ([I.PERFORMANCE] if any(e in L.ADS_DOMAINS for e in ents) else [])
    binding = {}
    for api in apis:
        st = I.binding_from_markers(items, api, now, obs).status
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


def cycle_start(chain):
    for e in reversed(L.ordered([e for e in chain if not e.get("invalid_marker")])):
        if e.get("to_state") == L.VALIDATING:
            return I.as_utc(e.get("occurred_at"))
    return None


def build_snapshot(contract, tables, now, secret_counts=None, plan_hash=None, extra_decision=None):
    chain, decisions, ledger, rows, hold = read_state(contract, tables)
    if extra_decision:
        decisions = {**decisions, L.next_seq(chain): extra_decision}
    jobs = (contract["marketplaces"].get("ozon") or {}).get("jobs") or {}
    ents = tuple(sorted({e for j in jobs.values() for e in j["entities"]}))
    ph, chunks = CK.latest_plan(ledger, since=cycle_start(chain))
    folded = CK.fold(ledger, reopened(rows))
    binding, cred = operator_binding(contract, tables, now, ents) if plan_hash else ({}, {})
    return L.Snapshot(tenant_id=contract["tenant_id"], now=now, events=chain, decisions=decisions, hold=hold,
                      enabled_entities=ents, secret_versions=secret_counts or {}, binding=binding, credentials=cred,
                      plan_hash=ph if chunks else None,
                      chunks={c.chunk_id: (folded.get(c.chunk_id) or {}).get("status", "PENDING") for c in chunks},
                      operator_plan_hash=plan_hash)


def new_decision(actor, now, **fields):
    base = {"decision_type": "TRANSITION", "expect_state": None, "to_state": None, "expect_seq": None,
            "plan_hash": None, "secret_versions_json": None, "chunk_id": None, "reopens_run_id": None,
            "domain": None, "boundary_id": None, "reason": None, "actor": actor, "decided_at": now.isoformat()}
    base.update(fields)
    base["decision_id"] = "dec-" + hashlib.sha256(json.dumps(base, sort_keys=True).encode() + uuid.uuid4().bytes).hexdigest()[:24]
    return base


def transition(contract, tables, now, target, actor, reason, plan_hash=None, secret_counts=None):
    """(статус, текст). Знак-решение ref.OPD_<seq> (CAS), затем маркер S_<seq> (CAS), затем зеркала."""
    chain, _d, _l, _r, _h = read_state(contract, tables)
    seq = L.next_seq(chain)
    dec = new_decision(actor, now, expect_state=L.current_state(chain), to_state=target, plan_hash=plan_hash,
                       reason=reason, expect_seq=seq,
                       secret_versions_json=json.dumps(secret_counts, sort_keys=True) if secret_counts else None)
    s = build_snapshot(contract, tables, now, secret_counts, plan_hash, extra_decision=dec)
    status, failures, event = L.decide(s, target, actor, reason or target, {
        "secret_versions": secret_counts, "plan_hash": plan_hash}, f"operator:{uuid.uuid4()}",
        decision_id=dec["decision_id"])
    if status != "WRITE":
        return status, "; ".join(failures) or "ok"
    ref, locks, ops = (contract["datasets"][k] for k in ("ref", "tenant_locks", "tenant_ops"))
    if target != L.SUSPENDED:
        labels, desc = L.decision_marker(event["from_state"], target, decision_id=dec["decision_id"],
                                         plan_hash=plan_hash, secret_versions=secret_counts, actor=actor)
        name = L.decision_marker_name(event["seq"])
        if not tables.create_marker(ref, name, labels, desc):
            # Знак на этот номер уже есть: прошлый запуск упал между знаком и маркером («сирота»).
            # То же ребро и тот же план — повтор идемпотентен (достраиваем маркер); иначе — отказ.
            prev = L.decision_from_marker(*(tables.get_table(ref, name) or ({}, None)))
            if (prev.get("to_state"), prev.get("expect_state"), prev.get("plan_hash")) != \
                    (target, event["from_state"], plan_hash):
                return "CONFLICT", f"на номер {event['seq']} есть другое решение владельца — см. status"
            dec["decision_id"] = prev.get("decision_id") or dec["decision_id"]
    if not tables.create_marker(locks, L.marker_name(event["seq"]), L.marker_labels(event),
                                json.dumps({"reason_code": event["reason_code"], "run_id": event["run_id"]})):
        # Решение ref.OPD_<seq> привязано к номеру: чужое событие с этим номером им не станет.
        return "CONFLICT", f"номер {event['seq']} занят параллельным переходом — повторите после status"
    tables.append(ref, "OPERATOR_DECISIONS", [dec])
    tables.append(ops, "TENANT_STATE_EVENTS", [event])
    return "WRITE", f"{event['from_state']} → {event['to_state']} (seq {event['seq']}, {dec['decision_id']})"


def set_hold(contract, tables, actor, action, reason):
    """Стоп-кран владельца ref.OPH_<n>: suspend | resume. Действует на control и runtime сразу."""
    ref = contract["datasets"]["ref"]
    n = max([int(L.HOLD_MARKER_RE.match(nm).group(1)) for nm, _l, _c in tables.list_tables(ref)
             if L.HOLD_MARKER_RE.match(nm)] or [0]) + 1
    if not tables.create_marker(ref, f"OPH_{n:04d}", {"action": action},
                                json.dumps({"actor": actor, "reason": reason}, ensure_ascii=False)):
        return "CONFLICT", "параллельный стоп-кран — повторите после status"
    return "WRITE", f"стоп-кран {action} (OPH_{n:04d})"


def accept_limitation(contract, tables, now, actor, domain, boundary_id, reason):
    """ACCEPT_LIMITATION: владелец принимает PARTIAL-границу истории домена (конкретную строку)."""
    ops = contract["datasets"]["tenant_ops"]
    rows = [r for r in tables.rows(ops, "HISTORY_BOUNDARIES") if r.get("boundary_id") == boundary_id]
    if not rows or rows[0].get("entity") != domain or rows[0].get("completeness_status") != "PARTIAL":
        return "REJECT", "граница не найдена, другого домена или не PARTIAL"
    dec = new_decision(actor, now, decision_type="ACCEPT_LIMITATION", domain=domain, boundary_id=boundary_id,
                       reason=reason[:500])
    tables.append(contract["datasets"]["ref"], "OPERATOR_DECISIONS", [dec])
    return "WRITE", f"ограничение {domain} принято ({boundary_id}, {dec['decision_id']})"


def reopen_chunk(contract, tables, now, actor, chunk_id, reason):
    _chain, _d, ledger, rows, _h = read_state(contract, tables)
    st = CK.fold(ledger, reopened(rows)).get(chunk_id)
    if not st or st["status"] != "DONE" or not st.get("run_id"):
        return "REJECT", f"отрезок {chunk_id} не DONE — ремонт не нужен"
    dec = new_decision(actor, now, decision_type="REOPEN_CHUNK", chunk_id=chunk_id, reopens_run_id=st["run_id"],
                       reason=reason[:500])
    ref = contract["datasets"]["ref"]
    n = max([int(nm[4:]) for nm, _l, _c in tables.list_tables(ref) if nm.startswith("OPR_") and nm[4:].isdigit()]
            or [0]) + 1
    # Знак ref.OPR_<n> — консистентно для control (ремонт отказ УСИЛИВАЕТ, задержка строк недопустима).
    if not tables.create_marker(ref, f"OPR_{n:04d}", {"chunk": chunk_id[:63]},
                                json.dumps({"chunk_id": chunk_id, "reopens_run_id": st["run_id"],
                                            "decision_id": dec["decision_id"]}, ensure_ascii=False)):
        return "CONFLICT", "параллельный ремонт — повторите после status"
    tables.append(ref, "OPERATOR_DECISIONS", [dec])
    return "WRITE", f"REOPEN {chunk_id}: отменена версия DONE {st['run_id']} (OPR_{n:04d})"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("cmd", choices=["status", "reopen-chunk", "accept-limitation", *OPERATOR_TARGETS])
    ap.add_argument("tenant_id")
    ap.add_argument("--plan-hash")
    ap.add_argument("--reason")
    ap.add_argument("--chunk")
    ap.add_argument("--domain")
    ap.add_argument("--boundary")
    a = ap.parse_args(argv)
    from tools.tenancy.tenant_infra import contract_for
    c = contract_for(a.tenant_id)
    t = TT.Tables(c["project_id"])
    now = datetime.now(timezone.utc)
    if a.cmd == "status":
        chain, decisions, ledger, _r, hold = read_state(c, t)
        ph, chunks = CK.latest_plan(ledger, since=cycle_start(chain))
        per = {}
        for ch in chunks:
            p0 = per.setdefault(ch.domain, [str(ch.start), str(ch.end), 0])
            p0[0], p0[1], p0[2] = min(p0[0], str(ch.start)), max(p0[1], str(ch.end)), p0[2] + 1
        print(json.dumps({"state": L.current_state(chain), "events": len(chain), "owner_hold": hold,
                          "plan": {"hash": ph, "domains": per},      # что именно одобряет approve-plan
                          "audit": L.audit_history(chain, decisions)[:5]}, ensure_ascii=False))
        return 0
    actor = f"OPERATOR:{TT.owner_account()}"
    if a.cmd == "reopen-chunk":
        if not (a.chunk and a.reason):
            print("REJECT: --chunk и --reason обязательны")
            return 1
        status, text = reopen_chunk(c, t, now, actor, a.chunk, a.reason)
    elif a.cmd == "accept-limitation":
        if not (a.domain and a.boundary and a.reason):
            print("REJECT: --domain, --boundary и --reason обязательны")
            return 1
        status, text = accept_limitation(c, t, now, actor, a.domain, a.boundary, a.reason)
    elif a.cmd == "suspend":
        if not a.reason:
            print("REJECT: приостановка без --reason")
            return 1
        status, text = set_hold(c, t, actor, "suspend", a.reason)       # действует сразу и безусловно
        st2, text2 = transition(c, t, now, L.SUSPENDED, actor, a.reason)  # журнал — по возможности
        text += f"; журнал: {st2} {text2}"
    else:
        if a.cmd == "resume":
            _c, _d, _l, _r, hold = read_state(c, t)
            if hold:
                status, text = set_hold(c, t, actor, "resume", a.reason or "RESUME")
                if status != "WRITE":
                    print(f"{status}: {text}")
                    return 1
            state = L.current_state(read_state(c, t)[0])
            if state not in (L.SUSPENDED, L.READY):
                print(f"WRITE: стоп-кран снят; журнал в состоянии {state} — перехода возобновления не требуется")
                return 0
        counts = secret_versions(c["project_id"], (c["marketplaces"]["ozon"]["secret_ids"] or {}).values()) \
            if a.cmd == "credentials-inserted" else None
        status, text = transition(c, t, now, OPERATOR_TARGETS[a.cmd], actor, a.reason or a.cmd.upper(),
                                  a.plan_hash, counts)
    print(f"{status}: {text}")
    return 0 if status in ("WRITE", "NOOP") else 1


if __name__ == "__main__":
    sys.exit(main())
