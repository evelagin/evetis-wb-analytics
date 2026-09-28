#!/usr/bin/env python3
"""Подтверждение кабинета продавца владельцем (Tenancy T5, протокол F).

  python tools/tenancy/tenant_binding.py show <tenant_id> [--api seller|performance] [--reveal]
  python tools/tenancy/tenant_binding.py confirm <tenant_id> --api A --observation ID --fingerprint FP \
                                          --expect-current <binding_id|NONE>
  python tools/tenancy/tenant_binding.py revoke <tenant_id> --api A --expect-current <binding_id> --reason TEXT

observe (control: lifecycle.py validate) → show → владелец сверяет кабинет вне системы → confirm →
ref.SELLER_BINDING (CONFIRMED). Пишет ТОЛЬКО владелец: у control и runtime права записи в ref нет.

confirm:
  * оптимистичная конкуренция: --expect-current должен совпасть с действующим событием привязки
    (или NONE, если подтверждений нет). Событие занимает номер маркером tenant_locks.B_<api>_<n>
    (tables.insert, 409 — параллельное подтверждение, запись отменена). Текущее событие берётся из
    последнего маркера (метаданные консистентны), а не из строк ref: tabledata.list не видит строки
    потоковой вставки до ~90 мин, и «перечитать после записи» ложно сообщало о конфликте;
  * последнее наблюдение API — ровно одно, свежее (≤ 24 ч), статус OBSERVED, отпечаток совпадает с
    --fingerprint и --observation — иначе отказ;
  * идемпотентно: действующая привязка с тем же отпечатком и наблюдением — ничего не пишется;
  * identity в Git, артефакты и журналы не попадает; show печатает маски, полные значения — только
    с --reveal в терминал владельца.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.tenancy import tenant_tables as TT  # noqa: E402

import identity as I  # noqa: E402  (pipelines/ozon/runtime через tenant_tables)

NONE = "NONE"
MARKER_RE = re.compile(r"^B_(seller|performance)_(\d{4})$")


def marker_head(markers, api):
    """(номер последнего маркера, binding_id его события) по API; (0, None), если маркеров нет."""
    best = (0, None)
    for name, labels, _created in markers:
        m = MARKER_RE.match(name or "")
        if m and m.group(1) == api and int(m.group(2)) > best[0]:
            best = (int(m.group(2)), (labels or {}).get("binding"))
    return best


def binding_label(binding_id: str) -> str:
    return re.sub(r"[^a-z0-9_-]", "_", binding_id.lower())[:63]


def current_event_id(bindings, api, now):
    b = I.effective_binding(bindings, api, now)
    return (b.binding_id or NONE) if b.status != I.UNBOUND or b.binding_id else NONE, b


def decide_confirm(observations, bindings, api, observation_id, fingerprint, expect_current, now, actor):
    """(WRITE | NOOP | REJECT, причины, строка SELLER_BINDING | None) — без облака."""
    cur_id, cur = current_event_id(bindings, api, now)
    if cur.status == "CONFIRMED" and cur.fingerprint == fingerprint and cur.source_observation_id == observation_id:
        return "NOOP", ["привязка уже действует с этим отпечатком и наблюдением"], None
    if expect_current != cur_id:
        return "REJECT", [f"ожидалось текущее событие {expect_current}, фактически {cur_id} (конкурентная запись?)"], None
    if cur.status == I.INVALID_BINDING:
        return "REJECT", [f"действующая привязка некорректна: {cur.reason} — сначала revoke"], None
    obs, why = I.observation_status(observations, api, now)
    if obs is None:
        return "REJECT", [why], None
    if obs.get("observation_id") != observation_id:
        return "REJECT", ["--observation не последнее наблюдение этого API"], None
    if obs.get("identity_fingerprint") != fingerprint:
        return "REJECT", ["--fingerprint не совпадает с наблюдением"], None
    row = {"binding_id": "bnd-" + hashlib.sha256(f"{api}|{observation_id}|{fingerprint}|{now.isoformat()}".encode()).hexdigest()[:24],
           "marketplace": "OZON", "api": api, "identity_fingerprint": fingerprint,
           "seller_client_id": obs.get("seller_client_id") if api == I.SELLER else None,
           "performance_client_id": obs.get("performance_client_id") if api == I.PERFORMANCE else None,
           "status": "CONFIRMED", "confirmed_by": actor, "confirmed_at": now.isoformat(),
           "source_observation_id": observation_id, "revoked_at": None, "revoked_by": None,
           "notes": "confirmed via tools/tenancy/tenant_binding.py"}
    return "WRITE", [], row


def decide_revoke(bindings, api, expect_current, reason, now, actor):
    cur_id, cur = current_event_id(bindings, api, now)
    if cur_id == NONE or cur.status == I.UNBOUND:
        return "NOOP", ["действующей привязки нет"], None
    if expect_current != cur_id:
        return "REJECT", [f"ожидалось {expect_current}, фактически {cur_id}"], None
    if not reason:
        return "REJECT", ["отзыв без причины"], None
    src = next(b for b in bindings if b.get("binding_id") == cur_id)
    row = dict(src, binding_id="rev-" + hashlib.sha256(f"{cur_id}|{now.isoformat()}".encode()).hexdigest()[:24],
               status="REVOKED", revoked_at=now.isoformat(), revoked_by=actor, notes=reason[:500])
    return "WRITE", [], row


def _load(tenant_id):
    from tools.tenancy.tenant_infra import contract_for
    c = contract_for(tenant_id)
    t = TT.Tables(c["project_id"])
    return c, t, list(t.rows(c["datasets"]["tenant_ops"], "SELLER_IDENTITY_OBSERVATIONS")), \
        list(t.rows(c["datasets"]["ref"], "SELLER_BINDING"))


def head_consistent(markers, bindings, api, now):
    """Строки ref догнали маркеры? Иначе решение по строкам принимать нельзя (отставание чтения)."""
    n, head = marker_head(markers, api)
    cur_id, _cur = current_event_id(bindings, api, now)
    return n == 0 or binding_label(cur_id) == head, n


def write_event(c, t, api, row, n):
    """Занять номер маркером, затем дописать строку. False — номер занят (параллельная запись)."""
    ok = t.create_marker(c["datasets"]["tenant_locks"], f"B_{api}_{n:04d}",
                         {"api": api, "binding": binding_label(row["binding_id"]),
                          "status": row["status"].lower()}, f"binding event {row['status']}")
    if ok:
        t.append(c["datasets"]["ref"], "SELLER_BINDING", [row])
    return ok


def cmd_show(a):
    _c, _t, obs, binds = _load(a.tenant_id)
    now = datetime.now(timezone.utc)
    for api in ([a.api] if a.api else list(I.APIS)):
        o, why = I.observation_status(obs, api, now)
        cur_id, cur = current_event_id(binds, api, now)
        print(f"[{api}] привязка: {cur.status} (событие {cur_id}), наблюдение: {why}")
        if o:
            v = (lambda x: x) if a.reveal else I.mask
            print(f"  observation_id={o['observation_id']} observed_at={o['observed_at']}")
            print(f"  fingerprint={o['identity_fingerprint']}")
            if api == I.SELLER:
                print(f"  Client-Id={v(o.get('seller_client_id'))} ИНН={v(o.get('company_inn'))} ОГРН={v(o.get('company_ogrn'))}")
                print(f"  название={o.get('company_name') if a.reveal else '(—reveal)'} подписка={o.get('subscription_type')}")
            else:
                print(f"  client_id={v(o.get('performance_client_id'))} доказательство={o.get('evidence_json')}")
    return 0


def _write(c, t, binds, api, now, decide):
    markers = t.list_tables(c["datasets"]["tenant_locks"])
    fresh, n = head_consistent(markers, binds, api, now)
    if not fresh:
        print("REJECT: строки ref.SELLER_BINDING ещё не видны (запись моложе ~90 мин) — повторите позже")
        return 1
    status, why, row = decide()
    print(f"{status}: {'; '.join(why) or 'ok'}")
    if status != "WRITE":
        return 0 if status == "NOOP" else 1
    if not write_event(c, t, api, row, n + 1):
        print("CONFLICT: параллельная запись привязки заняла номер — ничего не записано")
        return 1
    print(f"записано: {row['binding_id']} (маркер B_{api}_{n + 1:04d}); строка видна чтению через ≤ 90 мин")
    return 0


def cmd_confirm(a):
    c, t, obs, binds = _load(a.tenant_id)
    now = datetime.now(timezone.utc)
    actor = f"OPERATOR:{TT.owner_account()}"
    return _write(c, t, binds, a.api, now, lambda: decide_confirm(
        obs, binds, a.api, a.observation, a.fingerprint, a.expect_current, now, actor))


def cmd_revoke(a):
    c, t, _obs, binds = _load(a.tenant_id)
    now = datetime.now(timezone.utc)
    return _write(c, t, binds, a.api, now, lambda: decide_revoke(
        binds, a.api, a.expect_current, a.reason, now, f"OPERATOR:{TT.owner_account()}"))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("show"); s.add_argument("tenant_id"); s.add_argument("--api", choices=I.APIS); s.add_argument("--reveal", action="store_true")
    c = sub.add_parser("confirm"); c.add_argument("tenant_id"); c.add_argument("--api", choices=I.APIS, required=True)
    c.add_argument("--observation", required=True); c.add_argument("--fingerprint", required=True); c.add_argument("--expect-current", required=True)
    r = sub.add_parser("revoke"); r.add_argument("tenant_id"); r.add_argument("--api", choices=I.APIS, required=True)
    r.add_argument("--expect-current", required=True); r.add_argument("--reason", required=True)
    a = ap.parse_args(argv)
    return {"show": cmd_show, "confirm": cmd_confirm, "revoke": cmd_revoke}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
