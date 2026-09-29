#!/usr/bin/env python3
"""Подтверждение кабинета продавца владельцем (Tenancy T5, протокол F).

  python tools/tenancy/tenant_binding.py show <tenant_id> [--api seller|performance] [--reveal]
  python tools/tenancy/tenant_binding.py confirm <tenant_id> --api A --observation ID --fingerprint FP \
                                          --expect-current <OPB_<api>_<n>|NONE>
  python tools/tenancy/tenant_binding.py revoke <tenant_id> --api A --expect-current <OPB_<api>_<n>> --reason TEXT

observe (control: lifecycle.py validate) → show → владелец сверяет кабинет вне системы → confirm →
ref.OPB_<api>_<n> (таблица-знак владельца; описание — строка события) + зеркало ref.SELLER_BINDING.
Пишет ТОЛЬКО владелец: у control и runtime права создавать таблицы или писать строки в ref нет.

confirm:
  * оптимистичная конкуренция: --expect-current должен совпасть с действующим событием привязки
    (или NONE, если подтверждений нет). Событие — знак ref.OPB_<api>_<n+1> (tables.insert, 409 —
    параллельная запись, ничего не записано). Истина — знаки (метаданные консистентны; control и runtime
    читают их же, отзыв виден сразу), строки SELLER_BINDING — зеркало для SQL;
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
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.tenancy import tenant_tables as TT  # noqa: E402

import identity as I  # noqa: E402  (pipelines/ozon/runtime через tenant_tables)

NONE = "NONE"


def current_event_id(items, api, now):
    """(имя последнего знака API или NONE, действующая привязка) — по знакам ref.OPB_*.

    Сверка --expect-current — по имени знака, а не по binding_id: испорченное описание знака
    (INVALID_BINDING) тоже можно назвать и отозвать."""
    n, _row = I.binding_head(items, api)
    return (I.binding_marker_name(api, n) if n else NONE), I.binding_from_markers(items, api, now)


def decide_confirm(observations, items, api, observation_id, fingerprint, expect_current, now, actor):
    """(WRITE | NOOP | REJECT, причины, строка события | None) — без облака."""
    cur_id, cur = current_event_id(items, api, now)
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
    # Отпечаток пересчитывается из тех значений, которые видит владелец (show): строку наблюдения пишет
    # control, и подменить «лицо» при чужом отпечатке он не должен суметь.
    try:
        recomputed = (I.seller_fingerprint(obs.get("seller_client_id"), obs.get("company_inn"), obs.get("company_ogrn"))
                      if api == I.SELLER else I.performance_fingerprint(obs.get("performance_client_id")))
    except I.IdentityError:
        recomputed = None
    if recomputed != fingerprint:
        return "REJECT", ["отпечаток не пересчитывается из показанных значений identity — наблюдение подделано"], None
    row = {"binding_id": "bnd-" + hashlib.sha256(f"{api}|{observation_id}|{fingerprint}|{now.isoformat()}".encode()).hexdigest()[:24],
           "marketplace": "OZON", "api": api, "identity_fingerprint": fingerprint,
           "seller_client_id": obs.get("seller_client_id") if api == I.SELLER else None,
           "performance_client_id": obs.get("performance_client_id") if api == I.PERFORMANCE else None,
           "status": "CONFIRMED", "confirmed_by": actor, "confirmed_at": now.isoformat(),
           "source_observation_id": observation_id, "revoked_at": None, "revoked_by": None,
           "notes": "confirmed via tools/tenancy/tenant_binding.py"}
    return "WRITE", [], row


def decide_revoke(items, api, expect_current, reason, now, actor):
    """Отзыв действующей или испорченной (INVALID_BINDING) головы; повторный отзыв — NOOP."""
    cur_id, cur = current_event_id(items, api, now)
    if cur_id == NONE or cur.status == I.UNBOUND:
        return "NOOP", ["действующей привязки нет"], None
    if expect_current != cur_id:
        return "REJECT", [f"ожидалось {expect_current}, фактически {cur_id}"], None
    if not reason:
        return "REJECT", ["отзыв без причины"], None
    _n, src = I.binding_head(items, api)
    defaults = {"marketplace": "OZON", "identity_fingerprint": "", "seller_client_id": None,
                "performance_client_id": None, "confirmed_by": actor, "confirmed_at": now.isoformat(),
                "source_observation_id": ""}
    # Частично заполненная голова (ручная правка) не должна давать строку без REQUIRED-полей зеркала.
    src = {**defaults, **{k: v for k, v in (src or {}).items() if not k.startswith("_") and v is not None}} \
        if src and not src.get("_invalid") else defaults
    row = dict(src, api=api, binding_id="rev-" + hashlib.sha256(f"{cur_id}|{now.isoformat()}".encode()).hexdigest()[:24],
               status="REVOKED", revoked_at=now.isoformat(), revoked_by=actor, notes=reason[:500])
    return "WRITE", [], row


def _load(tenant_id):
    from tools.tenancy.tenant_infra import contract_for
    c = contract_for(tenant_id)
    t = TT.Tables(c["project_id"])
    return c, t, list(t.rows(c["datasets"]["tenant_ops"], "SELLER_IDENTITY_OBSERVATIONS")), \
        t.series(c["datasets"]["ref"], "OPB_")


def write_event(c, t, items, api, row):
    """Знак ref.OPB_<api>_<n+1> (409 — параллельная запись: False), затем зеркало-строка."""
    n, _row = I.binding_head(items, api)
    labels, desc = I.binding_marker(row)
    if not t.create_marker(c["datasets"]["ref"], I.binding_marker_name(api, n + 1), labels, desc):
        return False
    t.append(c["datasets"]["ref"], "SELLER_BINDING", [row])
    return True


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


def _write(c, t, items, api, decide):
    status, why, row = decide()
    print(f"{status}: {'; '.join(why) or 'ok'}")
    if status != "WRITE":
        return 0 if status == "NOOP" else 1
    if not write_event(c, t, items, api, row):
        print("CONFLICT: параллельная запись привязки заняла номер — ничего не записано")
        return 1
    print(f"записано: {row['binding_id']} (знак ref.{I.binding_marker_name(api, I.binding_head(items, api)[0] + 1)})")
    return 0


def cmd_confirm(a):
    c, t, obs, items = _load(a.tenant_id)
    now = datetime.now(timezone.utc)
    actor = f"OPERATOR:{TT.owner_account()}"
    return _write(c, t, items, a.api, lambda: decide_confirm(
        obs, items, a.api, a.observation, a.fingerprint, a.expect_current, now, actor))


def cmd_revoke(a):
    c, t, _obs, items = _load(a.tenant_id)
    now = datetime.now(timezone.utc)
    return _write(c, t, items, a.api, lambda: decide_revoke(
        items, a.api, a.expect_current, a.reason, now, f"OPERATOR:{TT.owner_account()}"))


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
