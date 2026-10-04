#!/usr/bin/env python3
"""Control plane арендатора (Tenancy T5) — точка входа Cloud Run Job `tenant-control`.

Идентичность — sa-tenant-control@<проект арендатора>. Права: секреты (чтение), чтение ozon_raw /
ref / tenant_ops, дописывание журналов tenant_ops (insertAll) и аренды tenant_locks. НЕТ
bigquery.jobs.create (нет DML), НЕТ записи в ozon_raw и ref, НЕТ права подтвердить привязку
(ref.SELLER_BINDING пишет только владелец: tools/tenancy/tenant_binding.py), НЕТ права запускать
Cloud Run (исполнитель бэкфилла запускается отдельными воротами).

Переходы автомата занимают номер маркером tenant_locks.S_<seq> (409 — параллельный переход, выход 5);
рёбра оператора действительны только с решением владельца в ref.OPERATOR_DECISIONS. Журнал с
нарушением аудита — выход 4 для всех команд, кроме status.

  python lifecycle.py validate        учётные данные + наблюдение identity (Seller, Performance)
  python lifecycle.py discover        профиль возможностей (только при BOUND)
  python lifecycle.py history         границы истории (только при BOUND)
  python lifecycle.py plan            план бэкфилла → BACKFILL_CHECKPOINTS (PENDING)
  python lifecycle.py claim-next      аренда следующего отрезка → окружение исполнителя
  python lifecycle.py verify-chunks   RUNNING → DONE/FAILED по журналу прогонов runtime
  python lifecycle.py dq              прогон DQ → DQ_RESULTS
  python lifecycle.py advance         попытка следующего перехода CONTROL (с валидатором)
  python lifecycle.py status          состояние и сводка (без значений identity)

ENV (кроме общих runtime): TENANT_ID, TENANT_OPS_DATASET, TENANT_LOCKS_DATASET,
ENABLED_ENTITIES (через запятую), HISTORY_SEED (необязательно, YYYY-MM-DD — только точка старта).
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import uuid
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C  # noqa: E402
import checkpoints as CK  # noqa: E402
import credentials as CR  # noqa: E402
import dq as DQ  # noqa: E402
import history as H  # noqa: E402
import identity as I  # noqa: E402
import lifecycle_core as L  # noqa: E402
import quota as Q  # noqa: E402
import backfill_core as BF  # noqa: E402
from control_store import ControlStore  # noqa: E402

sys.excepthook = C.safe_excepthook

# сущность → [(api, capability)], без которых сущность не работает
ENTITY_CAPABILITIES = {
    "catalog": [("seller", "catalog")], "prices": [("seller", "prices")],
    "seller_info": [("seller", "seller_info")], "stocks": [("seller", "stocks")],
    "fbo_postings": [("seller", "fbo_postings")], "finance_accrual": [("seller", "finance_accrual")],
    "clusters": [("seller", "clusters")], "supplies": [("seller", "supplies")],
    "ads_campaigns": [("performance", "campaigns")],
    "ads_expense_daily": [("performance", "expense")],
    "ads_sku_daily": [("performance", "async_report")],
}


class Ctx:
    def __init__(self, env=os.environ):
        self.tenant = env["TENANT_ID"]
        self.entities = tuple(e for e in env.get("ENABLED_ENTITIES", "").split(",") if e)
        seed = env.get("HISTORY_SEED")
        self.seed = date.fromisoformat(seed) if seed else None
        self.run_id = f"ctl-{uuid.uuid4()}"
        self.actor = f"{L.CONTROL}:{self.run_id}"
        self.now = datetime.now(timezone.utc)
        self.today_msk = C.now_msk().date()
        self.store = ControlStore(C.bq(), C.PROJECT, {
            "tenant_ops": env["TENANT_OPS_DATASET"], "tenant_locks": env["TENANT_LOCKS_DATASET"],
            "ref": C.REF_DATASET, "ozon_raw": C.DATASET}, redact=C.redact_value)
        self.ads = any(e in L.ADS_DOMAINS for e in self.entities)


# ───────────────────────────────────────────── наблюдение identity и учётных данных
def observe_seller(ctx):
    version = os.environ.get("SELLER_IDENTITY_VERSION", I.SELLER_V1)
    if version not in (I.SELLER_V1, I.SELLER_V2):
        raise I.IdentityError("unsupported identity observation version")
    model = os.environ.get("SELLER_INVENTORY_MODEL", CR.STRICT_MODEL)
    if model not in CR.CREDENTIAL_MODELS:
        raise C.ConfigError("unsupported Seller credential model")
    policy = CR.load_policy()
    proof = CR.runtime_authorization_evidence(policy, C.SELLER_PROFILES)
    if model == CR.BROAD_MODEL and (version != I.SELLER_V2 or C.PROJECT == C.LEGACY_INGESTION_PROJECT or proof["status"] != "PASS"):
        raise C.ConfigError("broad Seller model requires external V2 and qualified runtime consistency")
    roles_http, roles = C.seller_post("/v1/roles", {})
    code, si = C.seller_post("/v1/seller/info", {})
    si = si if isinstance(si, dict) else {}
    company = si.get("company") or {} if code == 200 else {}
    if not isinstance(company, dict): company = {}
    client_id = C.seller_client_id()
    try:
        fp = I.seller_fingerprint(client_id, company.get("inn"), company.get("ogrn"), version=version)
    except I.IdentityError:
        fp = None
    verdict = CR.evaluate_seller_roles(roles if roles_http == 200 else {}, ctx.entities, ctx.now, policy,
                                      model=model, authentication_ok=code == 200, identity_valid=bool(fp),
                                      runtime_integrity=proof, profiles=C.SELLER_PROFILES)
    if roles_http != 200:
        verdict["blocking"].append(f"KEY_INSPECTION_FAILED: /v1/roles HTTP {roles_http}")
        verdict["dimensions"]["inspection_integrity"] = "FAIL"
        verdict["status"] = "FAIL"
    verdict["http_roles"], verdict["http_seller_info"] = roles_http, code
    status = "INCOMPLETE" if not fp else "OBSERVED" if verdict["status"] == "PASS" else "CREDENTIAL_REJECTED"
    sub = ((si or {}).get("subscription") or {}) if code == 200 else {}
    row = {"observation_id": f"obs-{uuid.uuid4()}", "run_id": ctx.run_id, "api": I.SELLER,
           "observed_at": ctx.now.isoformat(), "identity_fingerprint": fp or "",
           "seller_client_id": client_id, "performance_client_id": None,
           "company_inn": company.get("inn"), "company_ogrn": company.get("ogrn"),
           "company_name": company.get("name"), "legal_name": company.get("legal_name"),
           "subscription_type": sub.get("type"),
           "evidence_json": json.dumps({"credential": verdict, "seller_info_http": code, "identity_version": version,
                                 "authentication": "HTTP_ACCEPTED" if code == 200 else "UNPROVEN",
                                 "owner_confirmation": False, "machine_identity": "OBSERVED" if fp else "INCOMPLETE",
                                 "ogrn_present": "ogrn" in company, "ogrn_nonempty": bool(str(company.get("ogrn") or "").strip())}, ensure_ascii=False),
           "status": status}
    return verdict, row, fp


def catalog_skus():
    code, lst = C.seller_post("/v3/product/list", {"filter": {"visibility": "ALL"}, "last_id": "", "limit": 1000})
    if code != 200:
        return None
    ids = [i["product_id"] for i in ((lst.get("result") or {}).get("items") or [])]
    skus = set()
    for k in range(0, len(ids), 1000):
        c, info = C.seller_post("/v3/product/info/list", {"product_id": ids[k:k + 1000], "offer_id": [], "sku": []})
        if c != 200:
            return None
        skus |= {str(i.get("sku")) for i in (info.get("items") or [])}
    return skus


def observe_performance(ctx, skus, seller_fp=None):
    """Токен + безвредное чтение; доказательство — SKU кампаний CPC ⊆ каталог Seller."""
    token_ok, read_ok, http_read = True, False, None
    try:
        C.perf_token()
    except RuntimeError:
        token_ok = False
    advertised = set()
    if token_ok:
        http_read, txt = C.perf_get("/api/client/campaign")
        read_ok = http_read == 200
        if read_ok:
            camps = [c for c in (json.loads(txt).get("list") or []) if c.get("advObjectType") == "SKU"][:20]
            for c in camps:
                code, t = C.perf_get(f"/api/client/campaign/{int(c['id'])}/v2/products?page=1&pageSize=100")
                if code == 200:
                    advertised |= {str(p.get("sku")) for p in (json.loads(t).get("products") or [])}
    verdict = CR.evaluate_performance(token_ok, read_ok, http_read=http_read)
    perf_id = C.perf_client_id()
    fp = I.performance_fingerprint(perf_id) if perf_id else None
    inside = len(advertised & skus) if skus is not None else 0
    if verdict["status"] != "PASS":
        status = "CREDENTIAL_REJECTED"
    elif skus is None or not advertised:
        status = "NO_EVIDENCE"                     # нечем доказать, что это тот же продавец
    elif inside != len(advertised):
        status = "FOREIGN_SKUS"                    # кампании рекламируют чужие SKU — fail closed
    else:
        status = "OBSERVED"
    row = {"observation_id": f"obs-{uuid.uuid4()}", "run_id": ctx.run_id, "api": I.PERFORMANCE,
           "observed_at": ctx.now.isoformat(), "identity_fingerprint": fp or "",
           "seller_client_id": None, "performance_client_id": perf_id, "company_inn": None,
           "company_ogrn": None, "company_name": None, "legal_name": None, "subscription_type": None,
           "evidence_json": json.dumps({"credential": verdict, "advertised_skus": len(advertised),
                                        "in_seller_catalog": inside, "seller_binding_fingerprint": seller_fp,
                                        "sku_evidence_coverage": "SAMPLED", "owner_confirmation": False}, ensure_ascii=False),
           "status": status}
    return verdict, row, fp


def owner_binding(ctx, api, observations=None):
    """Привязка из маркеров владельца ref.OPB_<api>_<n> (tables.get — консистентно)."""
    items = ctx.store.ref_series(lambda n: I.binding_marker_name(api, n))
    return I.binding_from_markers(items, api, ctx.now, observations)


def live_binding(ctx, seller_fp, perf_fp, legal_ogrn=None):
    obs = list(ctx.store.rows("tenant_ops", "SELLER_IDENTITY_OBSERVATIONS"))
    out = {"seller": I.live_status(owner_binding(ctx, I.SELLER, obs), seller_fp, legal_ogrn)[0]}
    if ctx.ads:
        pb = owner_binding(ctx, I.PERFORMANCE, obs)
        out["performance"] = I.live_status(pb, perf_fp)[0]
        if seller_fp and I.fingerprint_version(seller_fp) == I.SELLER_V2 and pb.seller_binding_fingerprint != seller_fp:
            out["performance"] = I.INVALID_BINDING
    return out


# Команды, меняющие журналы, допустимы только в своих состояниях автомата: план строится до
# подтверждения оператором и после него не меняется; отрезки берутся только в BACKFILLING.
COMMAND_STATES = {"discover": (L.CAPABILITY_DISCOVERY,), "history": (L.CAPABILITY_DISCOVERY,),
                  "plan": (L.CAPABILITY_DISCOVERY,), "claim-next": (L.BACKFILLING,),
                  "verify-chunks": (L.BACKFILLING, L.RECONCILING), "dq": (L.RECONCILING, L.READY)}


def owner_rows(ctx) -> list:
    """Строки решений владельца ref.OPERATOR_DECISIONS: ACCEPT_LIMITATION (и зеркала знаков).

    Писать туда control не может; строка может быть не видна до ~90 мин — это только задерживает
    принятие ограничения (оно лишь ослабляет отказ). REOPEN_CHUNK отказ УСИЛИВАЕТ, поэтому он — знак
    ref.OPR_<n> (tables.get, без задержки), см. owner_reopens."""
    return [d for d in ctx.store.rows("ref", "OPERATOR_DECISIONS") if d.get("decision_id")]


def reopened_from_signs(items) -> frozenset:
    """run_id версий DONE, отменённых знаками владельца ref.OPR_<n> (описание — JSON с reopens_run_id)."""
    out = set()
    for _name, _labels, desc in items:
        try:
            rid = json.loads(desc or "{}").get("reopens_run_id")
        except (ValueError, AttributeError):
            rid = None
        if rid:
            out.add(rid)
    return frozenset(out)


def owner_reopens(ctx) -> frozenset:
    return reopened_from_signs(ctx.store.ref_series(lambda n: f"OPR_{n:04d}"))


def accepted_limitations(rows) -> set:
    """{(домен, boundary_id)} — все принятия владельца (не «последнее по порядку чтения»)."""
    return {(d.get("domain"), d.get("boundary_id")) for d in rows
            if d.get("decision_type") == "ACCEPT_LIMITATION" and d.get("domain") and d.get("boundary_id")}


def owner_decisions(ctx, chain) -> dict:
    """seq → решение владельца ref.OPD_<seq> для рёбер оператора цепочки (tables.get по имени)."""
    out = {}
    for e in chain:
        if (e.get("actor") or "").split(":")[0] == L.OPERATOR and e.get("seq"):
            m = ctx.store.ref_marker(L.decision_marker_name(e["seq"]))
            if m:
                out[int(e["seq"])] = L.decision_from_marker(*m)
    return out


def hold_on(ctx) -> bool:
    return L.hold_active([(n, lb) for n, lb, _d in ctx.store.ref_series(lambda k: f"OPH_{k:04d}")])


def verified_state(ctx):
    """(состояние, цепочка, решения владельца, нарушения) — цепочка из маркеров S_<seq>."""
    chain = ctx.store.state_chain()
    decisions = owner_decisions(ctx, chain)
    return L.current_state(chain), chain, decisions, L.audit_history(chain, decisions)


def require_state(ctx, command):
    state, _chain, _d, problems = verified_state(ctx)
    hold = hold_on(ctx)
    if hold or problems or state not in COMMAND_STATES[command]:
        C.log(event="command_rejected", run_id=ctx.run_id, command=command, state=state, owner_hold=hold,
              allowed=list(COMMAND_STATES[command]), journal_problems=problems[:3])
        raise SystemExit(4)
    return state


def cycle_events(chain):
    """События текущего цикла — после последнего входа в VALIDATING (включительно)."""
    ev = L.ordered([e for e in chain if not e.get("invalid_marker")])
    start = max((i for i, e in enumerate(ev) if e.get("to_state") == L.VALIDATING), default=0)
    return ev[start:]


def approved_plan_hash(chain, decisions):
    """Хеш плана из решения владельца APPROVE — только в ТЕКУЩЕМ цикле (после ротации ключа или
    возобновления план прошлого цикла не годится: разрыв до сегодняшнего дня не был бы догружен)."""
    for e in reversed(cycle_events(chain)):
        if e.get("to_state") == L.BACKFILLING and (e.get("actor") or "").split(":")[0] == L.OPERATOR:
            return (decisions.get(int(e.get("seq") or 0)) or {}).get("plan_hash")
    return None


def cycle_started_at(chain):
    """Время сервера (создание маркера) последнего входа в VALIDATING: старше — доказательства
    прошлого цикла (до приостановки или ротации ключа) и для переходов не годятся."""
    for e in reversed(L.ordered([e for e in chain if not e.get("invalid_marker")])):
        if e.get("to_state") == L.VALIDATING:
            return I.as_utc(e.get("occurred_at"))
    return None


def _cap_row(ctx, api, cap, status, http=None, kind="LIVE_CALL", evidence=None, notes=None):
    return {"profile_id": hashlib.sha256(f"{ctx.run_id}|{api}|{cap}".encode()).hexdigest()[:32],
            "run_id": ctx.run_id, "discovered_at": ctx.now.isoformat(), "api": api, "capability": cap,
            "status": status, "evidence_kind": kind, "http_status": http,
            "evidence_json": json.dumps(evidence or {}, ensure_ascii=False, default=str), "notes": notes}


def cmd_validate(ctx):
    if hold_on(ctx):                               # стоп-кран: ни ключей, ни вызовов Ozon
        C.log(event="command_rejected", run_id=ctx.run_id, command="validate", owner_hold=True)
        return 4
    sv, srow, sfp = observe_seller(ctx)
    rows = [srow]
    caps = [_cap_row(ctx, "seller", "credential_read_only", "AVAILABLE" if sv["status"] == "PASS" else "DENIED",
                     kind="ROLE_LIST", evidence={k: sv[k] for k in ("blocking", "warnings", "method_count",
                                                                    "role_names", "expires_at", "credential_model", "dimensions",
                                                                    "policy_spec_sha256", "policy_version")})]
    if ctx.ads and (sv["status"] == "PASS" or sv["credential_model"] == CR.STRICT_MODEL):
        pv, prow, _pfp = observe_performance(ctx, catalog_skus() if sv["status"] == "PASS" else None, sfp)
        rows.append(prow)
        caps.append(_cap_row(ctx, "performance", "credential", "AVAILABLE" if pv["status"] == "PASS" else "DENIED",
                             evidence={"blocking": pv["blocking"], "warnings": pv["warnings"]}))
    ctx.store.append("SELLER_IDENTITY_OBSERVATIONS", rows)
    ctx.store.append("CAPABILITY_PROFILE", caps)
    # Отпечаток (даже префикс) в журнал не пишется: при публичных ИНН/ОГРН короткий Client-Id
    # восстанавливается перебором (ревью PR #226). Отпечаток — только в tenant_ops арендатора.
    C.log(event="validate_done", run_id=ctx.run_id, seller_credential=sv["status"],
          seller_dimensions=sv["dimensions"], credential_model=sv["credential_model"],
          observations=[{"api": r["api"], "status": r["status"]} for r in rows])
    return 0 if sv["status"] == "PASS" else 1


def require_bound(ctx):
    """Каждая команда после VALIDATING начинается со свежего наблюдения этого же прогона."""
    sv, _row, sfp = observe_seller(ctx)
    pfp = None
    if ctx.ads:
        pfp = I.performance_fingerprint(C.perf_client_id())
    b = live_binding(ctx, sfp if sv["status"] == "PASS" else None, pfp, _row.get("company_ogrn"))
    if any(v != I.BOUND for v in b.values()):
        C.log(event="binding_not_bound", run_id=ctx.run_id, binding=b)
        raise SystemExit(3)
    return sv, b


# ───────────────────────────────────────────── возможности
def _probe(api_call):
    try:
        code, _ = api_call()
    except C.ApiPathDenied:
        raise
    return ("AVAILABLE" if code == 200 else "DENIED" if code == 403 else "UNAVAILABLE"), code


def cmd_discover(ctx):
    require_state(ctx, "discover")
    sv, _b = require_bound(ctx)
    y = ctx.today_msk - timedelta(days=1)
    d30 = ctx.today_msk - timedelta(days=30)
    probes = {
        "catalog": lambda: C.seller_post("/v3/product/list", {"filter": {"visibility": "ALL"}, "last_id": "", "limit": 1}),
        "prices": lambda: C.seller_post("/v5/product/info/prices", {"cursor": "", "limit": 1, "filter": {"visibility": "ALL"}}),
        "seller_info": lambda: C.seller_post("/v1/rating/summary", {}),
        "stocks": lambda: C.seller_post("/v1/analytics/stocks", {"skus": []}),
        "fbo_postings": lambda: C.seller_post("/v3/posting/fbo/list", {"filter": {"since": f"{d30}T00:00:00.000Z", "to": f"{y}T23:59:59.999Z"}, "limit": 1, "cursor": ""}),
        "finance_accrual": lambda: C.seller_post("/v1/finance/accrual/by-day", {"date": str(y), "last_id": ""}),
        "clusters": lambda: C.seller_post("/v1/cluster/list", {"cluster_type": "CLUSTER_TYPE_OZON"}),
        "supplies": lambda: C.seller_post("/v3/supply-order/list", {"filter": {"states": ["COMPLETED"]}, "limit": 1}),
    }
    rows = []
    for ent, fn in probes.items():
        if ent in ctx.entities:
            st, code = _probe(fn)
            rows.append(_cap_row(ctx, "seller", ent, st, http=code))
    code, fbs = C.seller_post("/v3/posting/fbs/list", {"dir": "ASC", "filter": {"since": f"{d30}T00:00:00.000Z", "to": f"{y}T23:59:59.999Z"}, "limit": 1, "offset": 0})
    fbs_n = len(((fbs or {}).get("result") or {}).get("postings") or []) if code == 200 else None
    rows.append(_cap_row(ctx, "seller", "fbs_activity", "NOT_APPLICABLE" if fbs_n == 0 else ("AVAILABLE" if fbs_n else "UNKNOWN"),
                         http=code, evidence={"fbs_postings_30d": fbs_n}, notes="контракт FBO-only"))
    rows.append(_cap_row(ctx, "seller", "subscription", "AVAILABLE", kind="LIVE_CALL",
                         evidence={"source": "/v1/seller/info subscription.type"}))
    if ctx.ads:
        c1, txt = C.perf_get("/api/client/campaign")
        rows.append(_cap_row(ctx, "performance", "campaigns", "AVAILABLE" if c1 == 200 else "UNAVAILABLE", http=c1))
        c2, _t = C.perf_get(f"/api/client/statistics/expense?dateFrom={ctx.today_msk - timedelta(days=7)}&dateTo={y}")
        rows.append(_cap_row(ctx, "performance", "expense", "AVAILABLE" if c2 == 200 else "UNAVAILABLE", http=c2))
        running = [c for c in (json.loads(txt).get("list") or [])] if c1 == 200 else []
        active = sum(1 for c in running if str(c.get("state", "")).endswith("RUNNING"))
        rows.append(_cap_row(ctx, "performance", "active_campaigns", "AVAILABLE" if c1 == 200 else "UNKNOWN",
                             evidence={"running": active, "quota": Q.budget(active or None, [], ctx.now)}))
        cpc = [c for c in running if c.get("advObjectType") == "SKU"]
        rows.append(_cap_row(ctx, "performance", "async_report", "AVAILABLE" if cpc else "NOT_APPLICABLE",
                             kind="LIVE_CALL" if cpc else "DOCUMENTED",
                             evidence={"cpc_campaigns": len(cpc)},
                             notes=None if cpc else "нет CPC-кампаний: отчёт по SKU проверять не на чем"))
    ctx.store.append("CAPABILITY_PROFILE", rows)
    C.log(event="discover_done", run_id=ctx.run_id, capabilities={r["capability"]: r["status"] for r in rows})
    return 0


# ───────────────────────────────────────────── границы истории
def _fbo_probe(s, e):
    code, d = C.seller_post("/v3/posting/fbo/list", {"filter": {"since": f"{s}T00:00:00.000Z", "to": f"{e}T23:59:59.999Z"},
                                                    "limit": 1, "cursor": "", "sort_dir": "ASC"})
    if code == 200:
        p = (d.get("postings") or [])
        return H.ProbeResult(H.DATA if p else H.EMPTY, date.fromisoformat(p[0]["created_at"][:10]) if p else None, 200)
    return H.ProbeResult(H.REJECTED if code == 400 else H.ERROR, None, code if isinstance(code, int) else None)


def _accrual_probe(day):
    code, r = C.seller_post("/v1/finance/accrual/by-day", {"date": str(day), "last_id": ""})
    if code == 200:
        return H.ProbeResult(H.DATA if (r.get("accruals") or []) else H.EMPTY, day if r.get("accruals") else None, 200)
    return H.ProbeResult(H.REJECTED if code == 400 else H.ERROR, None, code if isinstance(code, int) else None)


def _expense_probe(s, e):
    code, txt = C.perf_get(f"/api/client/statistics/expense?dateFrom={s}&dateTo={e}")
    if code == 200:
        from entities import _csv_rows, _rub
        spend = [r for r in _csv_rows(txt) if r.get("ID") and (_rub(r.get("Расход")) or 0) != 0]
        return H.ProbeResult(H.DATA if spend else H.EMPTY, None, 200)
    return H.ProbeResult(H.REJECTED if code == 400 else H.ERROR, None, code if isinstance(code, int) else None)


def _boundary_row(ctx, b: H.Boundary):
    return {"boundary_id": hashlib.sha256(f"{ctx.run_id}|{b.domain}".encode()).hexdigest()[:32],
            "entity": b.domain, "requested_from": None,
            "api_documented_from": str(b.api_documented_from) if b.api_documented_from else None,
            "api_verified_from": str(b.api_verified_from) if b.api_verified_from else None,
            "first_observed_activity": str(b.first_observed_activity) if b.first_observed_activity else None,
            "first_data_date": None, "available_to": None,
            "backfill_from": str(b.first_observed_activity) if b.first_observed_activity else None,
            "backfill_to": None, "limitation_reason": b.limitation_reason,
            "completeness_status": b.completeness_status if b.completeness_status != "PARTIAL" else "PARTIAL",
            "confidence": b.confidence,
            "evidence_json": json.dumps({"mode": "EARLIEST_AVAILABLE", "seed": str(ctx.seed) if ctx.seed else None,
                                         "calls": b.calls, "probes": b.evidence[-40:]}, ensure_ascii=False),
            "determined_at": ctx.now.isoformat(), "run_id": ctx.run_id}


def cmd_history(ctx):
    require_state(ctx, "history")
    require_bound(ctx)
    out = {}
    if "fbo_postings" in ctx.entities:
        out["fbo_postings"] = H.windowed_first_activity("fbo_postings", _fbo_probe, ctx.today_msk, ctx.seed, exact_first=True)
    if "ads_expense_daily" in ctx.entities or "ads_sku_daily" in ctx.entities:
        out["ads_expense_daily"] = H.windowed_first_activity("ads_expense_daily", _expense_probe, ctx.today_msk, ctx.seed, max_calls=160)
        if "ads_sku_daily" in ctx.entities:
            b = out["ads_expense_daily"]
            out["ads_sku_daily"] = H.Boundary("ads_sku_daily", None, b.api_verified_from, b.first_observed_activity,
                                              b.limitation_reason, b.completeness_status, "DERIVED_FROM_EXPENSE", 0,
                                              [{"derived_from": "ads_expense_daily"}])
    if "finance_accrual" in ctx.entities:
        anchors = [b.first_observed_activity for b in out.values() if b.first_observed_activity]
        out["finance_accrual"] = H.sampled_first_activity("finance_accrual", _accrual_probe, ctx.today_msk,
                                                          anchor=min(anchors) if anchors else None)
    ctx.store.append("HISTORY_BOUNDARIES", [_boundary_row(ctx, b) for b in out.values()])
    C.log(event="history_done", run_id=ctx.run_id,
          boundaries={d: {"first": str(b.first_observed_activity), "status": b.completeness_status,
                          "confidence": b.confidence, "calls": b.calls} for d, b in out.items()})
    return 0


# ───────────────────────────────────────────── план и чекпойнты
def latest(rows, key, ts):
    best = {}
    for r in rows:
        k = r.get(key)
        if k is None:
            continue
        if k not in best or (I.as_utc(r.get(ts)) or L._EPOCH) >= (I.as_utc(best[k].get(ts)) or L._EPOCH):
            best[k] = r
    return best


def latest_caps(rows) -> dict:
    """(api, capability) → последняя строка CAPABILITY_PROFILE (как V_CAPABILITY_CURRENT)."""
    best = {}
    for r in rows:
        k = (r.get("api"), r.get("capability"))
        key = (I.as_utc(r.get("discovered_at")) or L._EPOCH, str(r.get("profile_id")))
        if k not in best or key >= best[k][0]:
            best[k] = (key, r)
    return {k: v[1] for k, v in best.items()}


def domain_start(boundaries: dict, dom: str):
    """Начало обязательного диапазона домена (для плана и для DQ COVERAGE — одно и то же).

    Начисления выбираются выборкой (5/15/25) и могут начаться раньше найденной даты, если они
    редкие; поэтому их диапазон начинается не позже самой ранней активности любого домена.
    """
    firsts = {d: date.fromisoformat(str(b["first_observed_activity"])) for d, b in boundaries.items()
              if d in L.HISTORICAL_DOMAINS and b.get("first_observed_activity")}
    if dom not in firsts:
        return None
    start = firsts[dom]
    if dom == "finance_accrual":
        start = max(min(firsts.values()), H.DOCUMENTED_FLOOR["finance_accrual"])
    return start


def build_plan(boundaries: dict, cutover: date):
    chunks = []
    for dom in L.HISTORICAL_DOMAINS:
        start = domain_start(boundaries, dom)
        if start is None:
            continue
        if dom == "fbo_postings":
            start -= timedelta(days=1)             # сутки МСК D требуют UTC-суток D−1 (§H)
        chunks += CK.plan(dom, start, cutover)
    return chunks


def _cp_row(ctx, c: CK.Chunk, status, attempts=0, plan_hash=None, **extra):
    return {"backfill_id": c.chunk_id, "entity": c.domain, "window_from": str(c.start), "window_to": str(c.end),
            "status": status, "attempts": attempts, "rows_written": extra.pop("rows_written", None),
            "run_id": extra.pop("run_id", ctx.run_id), "error_code": extra.pop("error_code", None),
            "error_detail": extra.pop("error_detail", None), "updated_at": ctx.now.isoformat(),
            "plan_hash": plan_hash, "lease_owner": extra.pop("lease_owner", None),
            "lease_until": extra.pop("lease_until", None), "lease_generation": extra.pop("lease_generation", None),
            "started_at": extra.pop("started_at", None), "completed_at": extra.pop("completed_at", None),
            "evidence_json": json.dumps(extra, ensure_ascii=False, default=str) if extra else None}


plan_versions, latest_plan = CK.plan_versions, CK.latest_plan


def cmd_plan(ctx):
    """План — версия с собственным хешем; перестроение допустимо только до подтверждения
    (CAPABILITY_DISCOVERY), в работу идёт ровно та версия, хеш которой подтвердил владелец."""
    require_state(ctx, "plan")
    require_bound(ctx)
    _st, chain, _d, _p = verified_state(ctx)
    bounds = latest(_fresh(ctx.store.rows("tenant_ops", "HISTORY_BOUNDARIES"), "determined_at",
                           cycle_started_at(chain)), "entity", "determined_at")
    cutover = ctx.today_msk - timedelta(days=1)
    chunks = [c for c in build_plan(bounds, cutover) if c.domain in ctx.entities]
    ph = CK.plan_hash(chunks)
    have = {(r["backfill_id"], r.get("plan_hash")) for r in ctx.store.rows("tenant_ops", "BACKFILL_CHECKPOINTS")}
    new = [_cp_row(ctx, c, "PENDING", plan_hash=ph) for c in chunks if (c.chunk_id, ph) not in have]
    ctx.store.append("BACKFILL_CHECKPOINTS", new)
    C.log(event="plan_done", run_id=ctx.run_id, plan_hash=ph, chunks=len(chunks), new=len(new),
          cutover=str(cutover))
    return 0


def run_id_for(chunk: CK.Chunk, gen: int) -> str:
    # Случайная часть — первой: runtime называет staging-таблицу по первым 10 символам run_id без
    # дефисов (common.merge_rows); два исполнителя одного отрезка не делят одну staging-таблицу.
    return f"bf-{uuid.uuid4().hex[:8]}-{chunk.chunk_id}-{gen:04d}"


# Выгрузки Performance: одна одновременная на аккаунт (Swagger «Лимиты») — один слот на арендатора.
PERF_SLOT = hashlib.sha256(b"performance-export-slot").hexdigest()[:16]


def chunk_exports(c: CK.Chunk, caps) -> int | None:
    """Выгрузки Performance на отрезок: только ads_sku_daily (асинхронный отчёт). Одна кампания в
    запросе — одна выгрузка; берётся ВСЁ число CPC-кампаний кабинета (верхняя оценка), окно
    отрезка ≤ 62 суток. None — число неизвестно (отрезок не берётся)."""
    if c.domain != "ads_sku_daily":
        return 0
    ev = json.loads((caps.get(("performance", "async_report")) or {}).get("evidence_json") or "{}")
    n = ev.get("cpc_campaigns")
    if not isinstance(n, int) or n < 0:
        return None
    return n * -(-((c.end - c.start).days + 1) // 62)


def quota_state(ledger, caps, now):
    ev = json.loads((caps.get(("performance", "active_campaigns")) or {}).get("evidence_json") or "{}")
    active = ev.get("running") if isinstance(ev.get("running"), int) else None
    spent = []
    for r in ledger:
        if r.get("status") == "RUNNING" and r.get("entity") == "ads_sku_daily":
            n = json.loads(r.get("evidence_json") or "{}").get("exports")
            if isinstance(n, int):
                spent.append((I.as_utc(r.get("started_at")), n))
    return Q.budget(active, spent, now)


def ledger_generations(ledger, chunk_id):
    return [r.get("lease_generation") for r in ledger if r.get("backfill_id") == chunk_id]


def cmd_claim_next(ctx):
    require_state(ctx, "claim-next")
    require_bound(ctx)
    _st, chain, decisions, _p = verified_state(ctx)
    ledger = list(ctx.store.rows("tenant_ops", "BACKFILL_CHECKPOINTS"))
    folded = CK.fold(ledger, owner_reopens(ctx))
    approved = approved_plan_hash(chain, decisions)
    versions = plan_versions(ledger)
    if approved not in versions:
        C.log(event="plan_not_approved", run_id=ctx.run_id, approved=approved, visible=sorted(versions))
        return 4
    chunks = versions[approved][0]
    caps = latest_caps(ctx.store.rows("tenant_ops", "CAPABILITY_PROFILE"))
    budget = quota_state(ledger, caps, ctx.now)
    leases = ctx.store.leases()
    for c in chunks:
        if not CK.claimable(folded.get(c.chunk_id)):
            continue
        exports = chunk_exports(c, caps)
        if exports is None or not Q.can_run(exports, budget):
            C.log(event="quota_deferred", run_id=ctx.run_id, chunk=c.chunk_id, domain=c.domain,
                  exports=exports, remaining=budget["remaining"])
            continue                               # продолжение — в следующем окне бюджета
        gen = CK.next_lease_generation(leases, c.chunk_id, ctx.now, ledger_generations(ledger, c.chunk_id))
        if gen is None:
            continue                               # аренда держится у другого исполнителя
        until = ctx.now + CK.LEASE_TTL
        rid = run_id_for(c, gen)
        attempts = (folded.get(c.chunk_id) or {}).get("attempts", 0) + 1
        executor_env = {"ENTITIES": c.domain, "SINCE": str(c.start), "UNTIL": str(c.end),
                        "INGESTION_RUN_ID": rid, "STRICT_PAGE_CAPS": "1"}
        continuation = None
        if C.PROJECT != C.LEGACY_INGESTION_PROJECT:
            executor_env.update(BACKFILL_MODE=BF.VERSION, TENANT_BINDING_REQUIRED="1",
                                BACKFILL_TARGET_PROJECT=C.PROJECT,
                                BACKFILL_GENERATION=approved,
                                BACKFILL_ORIGIN=versions[approved][1].isoformat())
            continuation = BF.plan(executor_env, c.domain, C.PROJECT, C.DATASET, C.REF_DATASET,
                                   ctx.today_msk, ctx.now)
        if exports:
            # Слот ограждает одновременность выгрузок, а не видимость журнала: без допуска (таймаут
            # исполнителя 1 ч < срока аренды 2 ч), иначе пропускная способность падала бы вдвое.
            slot = CK.next_lease_generation(leases, PERF_SLOT, ctx.now, grace=timedelta(0))
            if slot is None or not ctx.store.create_lease(CK.lease_name(PERF_SLOT, slot), until, ctx.run_id):
                C.log(event="quota_deferred", run_id=ctx.run_id, chunk=c.chunk_id, domain=c.domain,
                      reason="performance_slot_busy")
                continue                           # одна выгрузка на аккаунт одновременно
        if not ctx.store.create_lease(CK.lease_name(c.chunk_id, gen), until, ctx.run_id):
            continue                               # проиграли гонку (409) — следующий отрезок
        ctx.store.append("BACKFILL_CHECKPOINTS", [_cp_row(
            ctx, c, "RUNNING", attempts, plan_hash=approved, run_id=rid, lease_owner=ctx.run_id,
            lease_until=until.isoformat(), lease_generation=gen, started_at=ctx.now.isoformat(),
            exports=exports, runtime_plan=continuation)])
        C.log(event="chunk_claimed", run_id=ctx.run_id, chunk=c.chunk_id, domain=c.domain,
              lease_until=until.isoformat(), executor_env=executor_env)
        return 0
    C.log(event="nothing_to_claim", run_id=ctx.run_id)
    return 0


def classify_error(msg: str | None) -> str:
    m = msg or ""
    if "StrictLimitError" in m or "PaginationError" in m:
        return "TRUNCATION"
    if "429" in m or "выгруз" in m.lower() or "limit" in m.lower():
        return CK.QUOTA_ERROR_CLASS
    return "RUNTIME_ERROR"


def cmd_verify_chunks(ctx):
    require_state(ctx, "verify-chunks")
    ledger = list(ctx.store.rows("tenant_ops", "BACKFILL_CHECKPOINTS"))
    folded = CK.fold(ledger, owner_reopens(ctx))
    runs = list(ctx.store.rows("ozon_raw", "OZON_INGESTION_RUNS"))
    out = []
    for cid, st in folded.items():
        if st["status"] != "RUNNING":
            continue
        # Версия RUNNING именно текущей попытки (run_id свёртки), а не последняя в порядке выдачи.
        r = next((x for x in ledger if x["backfill_id"] == cid and x.get("status") == "RUNNING"
                  and x.get("run_id") == st["run_id"]), None)
        if r is None:
            continue
        c = CK.Chunk(r["entity"], date.fromisoformat(str(r["window_from"])), date.fromisoformat(str(r["window_to"])))
        mine = [x for x in runs if x.get("ingestion_run_id") == r["run_id"] and x.get("entity") == c.domain]
        ok = [x for x in mine if x.get("status") == "OK" and str(x.get("source_from")) == str(c.start)
              and str(x.get("source_to")) == str(c.end)]
        until = I.as_utc(r.get("lease_until"))
        expected = json.loads(r.get("evidence_json") or "{}").get("runtime_plan")
        if expected:
            if any(run.get("status") == "FAILED" for run in mine):
                failure = next(run for run in mine if run.get("status") == "FAILED")
                out.append(_cp_row(ctx, c, "FAILED", st["attempts"], run_id=r["run_id"],
                                   error_code=classify_error(failure.get("error_message"))))
                continue
            valid = []
            for run in mine:
                try:
                    proof = json.loads(run.get("evidence_json") or "{}")
                    if proof.get("plan") != expected or str(run.get("source_from")) != str(c.start) or str(run.get("source_to")) != str(c.end):
                        raise BF.EvidenceError("scope mismatch")
                    BF.validate(expected, proof["state"])
                    if (run.get("status") == "OK") != proof["state"]["complete"] or run.get("status") not in {"OK", "IN_PROGRESS"}:
                        raise BF.EvidenceError("status mismatch")
                    valid.append(proof)
                except (KeyError, ValueError, BF.EvidenceError):
                    valid = []; break
            if mine and not valid:
                out.append(_cp_row(ctx, c, "FAILED", st["attempts"], run_id=r["run_id"], error_code="BACKFILL_PROOF_INVALID"))
            elif valid and len({BF.digest(p) for p in valid}) != 1:
                out.append(_cp_row(ctx, c, "FAILED", st["attempts"], run_id=r["run_id"], error_code="BACKFILL_PROOF_CONFLICT"))
            elif valid and valid[0]["state"]["complete"]:
                out.append(_cp_row(ctx, c, "DONE", st["attempts"], run_id=r["run_id"],
                    rows_written=valid[0]["state"]["rows"], completed_at=ctx.now.isoformat(),
                    runtime_plan=expected, timezone="Europe/Moscow", proof=valid[0]))
            elif valid:
                # Successful bounded progress is resumable, not a penalized failure. Keep the
                # existing lease/visibility grace; no active execution can be replaced early.
                C.log(event="chunk_progress", chunk=cid, sequence=valid[0]["state"]["sequence"])
            elif until and until + CK.VISIBILITY_GRACE < ctx.now:
                out.append(_cp_row(ctx, c, "FAILED", st["attempts"], run_id=r["run_id"], error_code="LEASE_EXPIRED"))
            continue
        if ok:
            out.append(_cp_row(ctx, c, "DONE", st["attempts"], run_id=r["run_id"], rows_written=ok[0].get("rows_received"),
                               completed_at=ctx.now.isoformat()))
        elif mine:
            # Успех с другим окном — исполнитель запущен не с тем SINCE/UNTIL: отрезок не покрыт.
            err = ("WINDOW_MISMATCH" if all(x.get("status") == "OK" for x in mine)
                   else classify_error(mine[-1].get("error_message")))
            out.append(_cp_row(ctx, c, "FAILED", st["attempts"], run_id=r["run_id"], error_code=err))
        elif until and until + CK.VISIBILITY_GRACE < ctx.now:
            # Журнал прогонов пишется потоковой вставкой: «прогона нет» — только после допуска видимости.
            out.append(_cp_row(ctx, c, "FAILED", st["attempts"], run_id=r["run_id"], error_code="LEASE_EXPIRED"))
    ctx.store.append("BACKFILL_CHECKPOINTS", out)
    C.log(event="verify_chunks_done", run_id=ctx.run_id, updated=len(out))
    return 0


# ───────────────────────────────────────────── DQ и переходы
MERGE_KEYS = {"RAW_OZON_POSTINGS_FBO": ("posting_number", "sku"),
              "RAW_OZON_FINANCE_ACCRUAL": ("accrual_id", "type_id", "sku"),
              "RAW_OZON_ADS_EXPENSE_DAILY": ("date", "campaign_id"),
              "RAW_OZON_ADS_SKU_DAILY": ("date", "campaign_id", "sku")}


def maturity(ctx):
    vals = {r.get("value_num") for r in ctx.store.rows("ref", "REF_TENANT_ECONOMICS")
            if r.get("parameter") == "FINANCE_SETTLEMENT_MATURITY_DAYS" and r.get("scope") == "STORE"
            and r.get("value_num") is not None and (r.get("effective_to") is None
                                                    or str(r.get("effective_to")) >= str(ctx.today_msk))}
    return float(next(iter(vals))) if len(vals) == 1 else None


def active_plan(ledger, chain, decisions):
    """(хеш, отрезки) плана в работе: подтверждённый владельцем в текущем цикле, иначе последняя
    полная версия, построенная в текущем цикле (не раньше последнего входа в VALIDATING)."""
    approved = approved_plan_hash(chain, decisions)
    versions = plan_versions(ledger)
    if approved:
        return approved, (versions.get(approved) or (None, None))[0]
    return CK.latest_plan(ledger, since=cycle_started_at(chain))


def gather_facts(ctx, binding, chain=None, decisions=None):
    if chain is None:
        _s, chain, decisions, _p = verified_state(ctx)
    ledger = list(ctx.store.rows("tenant_ops", "BACKFILL_CHECKPOINTS"))
    folded = CK.fold(ledger, owner_reopens(ctx))
    _ph, chunks = active_plan(ledger, chain, decisions)
    chunks = chunks or []
    done, done_msk = {}, {}
    for c in chunks:
        if (folded.get(c.chunk_id) or {}).get("status") == "DONE":
            done_row = next((r for r in ledger if r.get("backfill_id") == c.chunk_id
                             and r.get("status") == "DONE" and r.get("run_id") == folded[c.chunk_id]["run_id"]), {})
            evidence = json.loads(done_row.get("evidence_json") or "{}")
            if evidence.get("runtime_plan") and evidence.get("timezone") == "Europe/Moscow":
                BF.validate(evidence["runtime_plan"], evidence["proof"]["state"])
                done_msk.setdefault(c.domain, []).append((c.start, c.end))
            else:
                done.setdefault(c.domain, []).append((c.start, c.end))
    bounds = latest(ctx.store.rows("tenant_ops", "HISTORY_BOUNDARIES"), "entity", "determined_at")
    cutover = max((c.end for c in chunks), default=None)
    ranges = {d: (domain_start(bounds, d), cutover)
              for d in bounds if d in ctx.entities and domain_start(bounds, d) and cutover}
    runs = list(ctx.store.rows("ozon_raw", "OZON_INGESTION_RUNS"))
    bf_runs = [r for r in runs if str(r.get("ingestion_run_id", "")).startswith("bf-")]
    truncated = []
    for r in bf_runs:
        if r.get("status") == "FAILED" and classify_error(r.get("error_message")) == "TRUNCATION":
            later_ok = any(x.get("entity") == r.get("entity") and x.get("status") == "OK"
                           and x.get("source_from") == r.get("source_from") and x.get("source_to") == r.get("source_to")
                           for x in bf_runs)
            if not later_ok:
                truncated.append(r.get("ingestion_run_id"))
    dups = {}
    for table, keys in MERGE_KEYS.items():
        seen, n = set(), 0
        for row in ctx.store.rows("ozon_raw", table):
            k = hashlib.sha256("\x1f".join(str(row.get(x)) for x in keys).encode()).digest()
            n += k in seen
            seen.add(k)
        dups[table] = n
    unresolved = sum(1 for r in ctx.store.rows("ozon_raw", "RAW_OZON_FINANCE_ACCRUAL")
                     if (r.get("operation_name") or "UNKNOWN") == "UNKNOWN")
    caps = latest_caps(ctx.store.rows("tenant_ops", "CAPABILITY_PROFILE"))
    fbs = (caps.get(("seller", "fbs_activity")) or {}).get("status") == "AVAILABLE"
    return {"binding": binding, "ads_enabled": ctx.ads,
            "chunks": {c.chunk_id: (folded.get(c.chunk_id) or {}).get("status", "PENDING") for c in chunks},
            "required_ranges": ranges, "done_windows": done, "done_windows_msk": done_msk, "truncated_runs": truncated,
            "raw_duplicate_keys": dups, "finance_unresolved_rows": unresolved, "maturity_days": maturity(ctx),
            "fbs_activity": fbs, "ads_sum_worst_pct": None, "key_expires_soon": False}


def dq_rows(ctx, checks):
    return [{"result_id": hashlib.sha256(f"{ctx.run_id}|{cid}".encode()).hexdigest()[:32], "check_id": cid,
             "period_from": None, "period_to": None, "metric": v.get("metric"), "source_a": None, "source_b": None,
             "value_a": v.get("value") if isinstance(v.get("value"), (int, float)) else None, "value_b": None,
             "difference": None, "difference_pct": None, "tolerance_abs": None, "tolerance_pct": None,
             "status": v["status"], "severity": v["severity"],
             "explanation": json.dumps({k: x for k, x in v.items() if k not in ("status", "severity")},
                                       ensure_ascii=False, default=str)[:1500],
             "evaluated_at": ctx.now.isoformat(), "run_id": ctx.run_id} for cid, v in checks.items()]


def cmd_dq(ctx):
    require_state(ctx, "dq")
    _sv, b = require_bound(ctx)
    checks = DQ.evaluate(gather_facts(ctx, b))
    ctx.store.append("DQ_RESULTS", dq_rows(ctx, checks))
    C.log(event="dq_done", run_id=ctx.run_id, blocking=DQ.blocking_failures(checks))
    return 0


def _fresh(rows, ts, since):
    """Только доказательства текущего цикла (не старше последнего входа в VALIDATING)."""
    return [r for r in rows if since is None or (I.as_utc(r.get(ts)) or L._EPOCH) >= since]


def snapshot(ctx, binding, credentials, chain, decisions, dq=None):
    since = cycle_started_at(chain)
    caps = latest_caps(_fresh(ctx.store.rows("tenant_ops", "CAPABILITY_PROFILE"), "discovered_at", since))
    ledger = list(ctx.store.rows("tenant_ops", "BACKFILL_CHECKPOINTS"))
    folded = CK.fold(ledger, owner_reopens(ctx))
    ph, chunks = active_plan(ledger, chain, decisions)
    chunks = chunks or []
    states = {c.chunk_id: (folded.get(c.chunk_id) or {}).get("status", "PENDING") for c in chunks}
    required = tuple(k for e in ctx.entities for k in ENTITY_CAPABILITIES.get(e, ()))
    return L.Snapshot(
        tenant_id=ctx.tenant, now=ctx.now, events=chain, decisions=decisions, enabled_entities=ctx.entities,
        credentials=credentials, binding=binding,
        capabilities={k: r["status"] for k, r in caps.items()},
        required_capabilities=required,
        history=latest(_fresh(ctx.store.rows("tenant_ops", "HISTORY_BOUNDARIES"), "determined_at", since),
                       "entity", "determined_at"),
        plan_hash=ph if chunks else None, chunks=states,
        last_chunk_done_at=max(((folded.get(c.chunk_id) or {}).get("done_at") for c in chunks
                                if (folded.get(c.chunk_id) or {}).get("done_at")), default=None),
        dq=dq, maturity_days=maturity(ctx), hold=hold_on(ctx),
        accepted_limitations=accepted_limitations(owner_rows(ctx)))


CONTROL_NEXT = {L.VALIDATING: [L.CAPABILITY_DISCOVERY], L.CAPABILITY_DISCOVERY: [L.READY_FOR_BACKFILL],
                L.BACKFILLING: [L.RECONCILING], L.RECONCILING: [L.READY, L.BACKFILLING]}


def live_performance():
    """Вердикт Performance этого прогона: токен и безвредное чтение (не строка прошлой проверки)."""
    try:
        C.perf_token()
    except RuntimeError:
        return CR.evaluate_performance(False, False)
    code, _t = C.perf_get("/api/client/campaign")
    return CR.evaluate_performance(True, code == 200, http_read=code)


def cmd_advance(ctx):
    cur, chain, decisions, problems = verified_state(ctx)
    if hold_on(ctx):
        # Стоп-кран владельца: цепочка догоняет его приостановкой (это ребро control разрешено всегда).
        if cur == L.SUSPENDED:
            return 0
        s = L.Snapshot(tenant_id=ctx.tenant, now=ctx.now, events=chain, decisions=decisions, hold=True)
        status, failures, event = L.decide(s, L.SUSPENDED, ctx.actor, "OWNER_HOLD", {"hold": True}, ctx.run_id)
        C.log(event="owner_hold", run_id=ctx.run_id, frm=cur, status=status, failures=failures[:3])
        if status == "WRITE" and not ctx.store.record_transition(event):
            return 5
        return 0 if status in ("WRITE", "NOOP") else 4
    if problems:
        C.log(event="journal_invalid", run_id=ctx.run_id, problems=problems[:5])
        return 4
    sv, _row, sfp = observe_seller(ctx)
    pv, pfp = None, None
    if ctx.ads:
        pfp = I.performance_fingerprint(C.perf_client_id())
        pv = live_performance()
    binding = live_binding(ctx, sfp if sv["status"] == "PASS" else None, pfp, _row.get("company_ogrn"))
    dq = None
    if cur == L.RECONCILING:
        # DQ считается в этом же прогоне: строки прошлых прогонов DQ могут быть не видны или старше
        # данных. Результат дописывается в DQ_RESULTS как доказательство перехода.
        checks = DQ.evaluate(gather_facts(ctx, binding, chain, decisions))
        ctx.store.append("DQ_RESULTS", dq_rows(ctx, checks))
        dq = {"run_id": ctx.run_id, "evaluated_at": ctx.now,
              "checks": {c: {"status": v["status"], "severity": v["severity"]} for c, v in checks.items()}}
    s = snapshot(ctx, binding, {"seller": sv, **({"performance": pv} if pv else {})}, chain, decisions, dq)
    for to in CONTROL_NEXT.get(cur, []):
        status, failures, event = L.decide(s, to, ctx.actor, f"AUTO_{to}", {
            "binding": binding, "plan_hash": s.plan_hash, "dq_run": (s.dq or {}).get("run_id")}, ctx.run_id)
        C.log(event="transition_decision", run_id=ctx.run_id, frm=cur, to=to, status=status, failures=failures[:10])
        if status == "WRITE":
            if not ctx.store.record_transition(event):
                C.log(event="transition_conflict", run_id=ctx.run_id, seq=event["seq"])
                return 5                           # параллельный переход занял номер — повторить позже
            return 0
        if status == "NOOP":
            return 0
    return 1


def cmd_status(ctx):
    state, chain, _d, problems = verified_state(ctx)
    C.log(event="status", state=state, events=len(chain), owner_hold=hold_on(ctx), journal_problems=problems[:5])
    return 0


COMMANDS = {"validate": cmd_validate, "discover": cmd_discover, "history": cmd_history, "plan": cmd_plan,
            "claim-next": cmd_claim_next, "verify-chunks": cmd_verify_chunks, "dq": cmd_dq,
            "advance": cmd_advance, "status": cmd_status}


def main(argv):
    if len(argv) != 1 or argv[0] not in COMMANDS:
        print(__doc__, file=sys.stderr)
        return 2
    C._seller_execution_scope = frozenset({"control"})
    return COMMANDS[argv[0]](Ctx())


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
