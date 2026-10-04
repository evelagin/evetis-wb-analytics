"""Tenancy T5 — control plane арендатора: состязательные тесты (задача T5, п. 12).

Офлайн: Ozon API, BigQuery (Tables API) и Secret Manager подменяются. Каждый тест — попытка
обойти одно правило: записать состояние из runtime, подтвердить привязку, перепрыгнуть через
валидатор, дважды взять отрезок, выдать пустой ответ за отсутствие истории, и т. д.
"""
from __future__ import annotations

import ast
import copy
import json
import os
import subprocess
import sys
import textwrap
import types
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

import checkpoints as CK
import common as C
import credentials as CR
import dq as DQ
import history as H
import identity as I
import lifecycle as LC
import lifecycle_core as L
import quota as Q
from control_store import APPEND_TABLES, ControlStore

RUNTIME = Path(__file__).resolve().parents[1] / "runtime"
NOW = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
ALL_ENTITIES = ("ads_campaigns", "ads_expense_daily", "ads_sku_daily", "catalog", "clusters", "fbo_postings",
                "finance_accrual", "prices", "seller_info", "stocks", "supplies")
# Синтетика: не реальные значения какого-либо кабинета.
CLIENT_ID, INN, OGRN, PERF_ID = "5550199", "7700000000", "1027700000000", "synthetic-perf-id@clients"
SELLER_FP = I.seller_fingerprint(CLIENT_ID, INN, OGRN)
PERF_FP = I.performance_fingerprint(PERF_ID)


@pytest.fixture(autouse=True)
def canonical_external_backfill_ref(monkeypatch):
    # The T5 fixture models an external tenant; the module-wide legacy default
    # evetis_ref is not its dataset. Never relax the runtime gate for synthetic tests.
    monkeypatch.setattr(C, "REF_DATASET", "ref")


# ═════════════════════════════════════════ стенд
class FakeStore:
    """ControlStore без облака: те же методы и те же запреты (append только в журналы tenant_ops)."""

    def __init__(self, tables=None, leases=None, events=(), ref_markers=None):
        self.t = {k: list(v) for k, v in (tables or {}).items()}
        self.lease_tables = dict(leases or {})
        self.appended = []
        self.markers = {}                                          # tenant_locks.S_<seq>: (метки, created)
        for e in events:
            self.markers[L.marker_name(e["seq"])] = (L.marker_labels(e), I.as_utc(e["occurred_at"]))
        self.refm = dict(ref_markers or {})                        # знаки владельца в ref: (метки, описание)

    def ref_marker(self, name):
        return self.refm.get(name)

    def ref_series(self, name_of, limit=9999):
        out = []
        for n in range(1, limit + 1):
            m = self.refm.get(name_of(n))
            if m is None:
                return out
            out.append((name_of(n), m[0], m[1]))
        return out

    def rows(self, key, table):
        assert key in ("tenant_ops", "ref", "ozon_raw"), key
        return iter(copy.deepcopy(self.t.get((key, table), [])))

    def append(self, table, rows):
        assert table in APPEND_TABLES, table
        rows = [C.redact_value(r) for r in rows]
        self.t.setdefault(("tenant_ops", table), []).extend(copy.deepcopy(rows))
        self.appended.append((table, rows))
        return len(rows)

    def leases(self):
        return list(self.lease_tables.items())

    def create_lease(self, name, until, owner):
        if name in self.lease_tables:
            return False                                           # 409
        self.lease_tables[name] = until
        return True

    def state_chain(self):
        return L.chain_from_markers((n, lb, c) for n, (lb, c) in self.markers.items())

    def record_transition(self, event):
        name = L.marker_name(event["seq"])
        if name in self.markers:
            return False                                           # 409: номер занят
        self.markers[name] = (L.marker_labels(event), I.as_utc(event["occurred_at"]))
        self.append("TENANT_STATE_EVENTS", [event])
        return True


def make_ctx(store, entities=ALL_ENTITIES, now=NOW, seed=None):
    ctx = object.__new__(LC.Ctx)
    ctx.tenant, ctx.entities, ctx.seed = "client_x", tuple(entities), seed
    ctx.run_id, ctx.now, ctx.today_msk = "ctl-test", now, now.date()
    ctx.actor = f"{L.CONTROL}:ctl-test"
    ctx.store = store
    ctx.ads = any(e in L.ADS_DOMAINS for e in ctx.entities)
    return ctx


def roles_ok(entities=ALL_ENTITIES, extra=(), expires="2027-06-01T00:00:00Z"):
    methods = sorted(CR.required_seller_methods(entities) | set(extra))
    return {"roles": [{"name": "Синтетическая роль", "methods": methods}], "expires_at": expires}


def fake_api(monkeypatch, roles=None, company=None, perf_ok=True, cpc_skus=("111",), catalog=("111", "222"),
             client_id=CLIENT_ID, perf_id=PERF_ID):
    roles = roles_ok() if roles is None else roles
    company = {"inn": INN, "ogrn": OGRN, "name": "ООО Синтетика"} if company is None else company
    calls = []

    def seller_post(path, body):
        calls.append(("seller", path))
        if path not in C.SELLER_ALLOWED_PATHS:
            raise C.ApiPathDenied(path)
        if path == "/v1/roles":
            return 200, roles
        if path == "/v1/seller/info":
            return 200, {"company": company, "subscription": {"type": "PREMIUM"}}
        if path == "/v3/product/list":
            return 200, {"result": {"items": [{"product_id": int(s)} for s in catalog]}}
        if path == "/v3/product/info/list":
            return 200, {"items": [{"sku": s} for s in catalog]}
        return 200, {}

    def perf_get(path, raw_text=True):
        calls.append(("perf", path))
        if path == "/api/client/campaign":
            return 200, json.dumps({"list": [{"id": 1, "advObjectType": "SKU", "state": "CAMPAIGN_STATE_RUNNING"}]})
        if path.startswith("/api/client/campaign/1/v2/products"):
            return 200, json.dumps({"products": [{"sku": s} for s in cpc_skus]})
        return 200, ""

    def perf_token():
        if not perf_ok:
            raise RuntimeError("не удалось получить токен Performance API")
        return "tok"

    monkeypatch.setattr(C, "seller_post", seller_post)
    monkeypatch.setattr(C, "perf_get", perf_get)
    monkeypatch.setattr(C, "perf_token", perf_token)
    monkeypatch.setattr(C, "seller_client_id", lambda: client_id)
    monkeypatch.setattr(C, "perf_client_id", lambda: perf_id)
    return calls


def ev(frm, to, actor="CONTROL:r", at=NOW - timedelta(hours=1), evidence=None, eid=None):
    return {"event_id": eid or f"e-{frm}-{to}-{at.timestamp()}", "tenant_id": "client_x", "from_state": frm,
            "to_state": to, "actor": actor, "occurred_at": at.isoformat(),
            "evidence_json": json.dumps(evidence or {})}


def chain(*states, start=NOW - timedelta(days=5)):
    """Корректный журнал до последнего состояния списка: номера seq подряд, у рёбер оператора — решение."""
    actors = {(None, L.CREDENTIALS_PENDING): "OPERATOR", (L.CREDENTIALS_PENDING, L.VALIDATING): "OPERATOR",
              (L.READY_FOR_BACKFILL, L.BACKFILLING): "OPERATOR", (L.SUSPENDED, L.VALIDATING): "OPERATOR",
              (L.READY, L.VALIDATING): "OPERATOR"}
    out, prev = [], None
    for i, s in enumerate(states):
        actor = actors.get((prev, s), "CONTROL")
        e = ev(prev, s, f"{actor}:r{i}", start + timedelta(minutes=i), eid=f"e{i + 1:02d}")
        e["seq"], e["decision_id"] = i + 1, (f"dec-{i + 1:02d}" if actor == "OPERATOR" else None)
        out.append(e)
        prev = s
    return out


def decisions_for(events, plan_hash=None):
    """seq → решение владельца (знак ref.OPD_<seq>) для рёбер оператора цепочки."""
    return {e["seq"]: {"decision_id": e["decision_id"], "expect_state": e["from_state"], "to_state": e["to_state"],
                       "plan_hash": plan_hash if e["to_state"] == L.BACKFILLING else None}
            for e in events if e.get("decision_id")}


def owner_markers(events=(), plan_hash=None, bindings=(), holds=()):
    """Знаки владельца в ref: OPD_<seq> для рёбер оператора, OPB_<api>_<n> для событий привязки, OPH_<n>."""
    out = {}
    for seq, d in decisions_for(events, plan_hash).items():
        out[L.decision_marker_name(seq)] = L.decision_marker(d["expect_state"], d["to_state"],
                                                             decision_id=d["decision_id"], plan_hash=d["plan_hash"])
    per = {}
    for row in bindings:
        per[row["api"]] = per.get(row["api"], 0) + 1
        out[I.binding_marker_name(row["api"], per[row["api"]])] = I.binding_marker(row)
    for i, action in enumerate(holds, 1):
        out[f"OPH_{i:04d}"] = ({"action": action}, "{}")
    return out


def opb_items(rows):
    """[(имя, метки, описание)] знаков привязки по строкам событий (в порядке номеров)."""
    return [(n, lb, d) for n, (lb, d) in owner_markers(bindings=rows).items()]


def binding_row(api=I.SELLER, fp=SELLER_FP, obs_id="obs-1", at=NOW - timedelta(hours=2), status="CONFIRMED",
                bid="bnd-1", revoked_at=None):
    return {"binding_id": bid, "marketplace": "OZON", "api": api, "identity_fingerprint": fp,
            "status": status, "confirmed_by": "OPERATOR:owner", "confirmed_at": at.isoformat(),
            "source_observation_id": obs_id, "revoked_at": revoked_at, "revoked_by": None}


def obs_row(api=I.SELLER, fp=SELLER_FP, obs_id="obs-1", at=NOW - timedelta(hours=3), status="OBSERVED"):
    ident = ({"seller_client_id": CLIENT_ID, "company_inn": INN, "company_ogrn": OGRN} if api == I.SELLER
             else {"performance_client_id": PERF_ID})
    return {"observation_id": obs_id, "api": api, "observed_at": at.isoformat(), "identity_fingerprint": fp,
            "status": status, **ident}


def ready_snapshot(**over):
    """Снимок, в котором контракт READY выполнен целиком; тесты ломают по одному условию."""
    s = L.Snapshot(
        tenant_id="client_x", now=NOW, events=chain(L.CREDENTIALS_PENDING, L.VALIDATING, L.CAPABILITY_DISCOVERY,
                                                    L.READY_FOR_BACKFILL, L.BACKFILLING, L.RECONCILING),
        enabled_entities=ALL_ENTITIES,
        credentials={"seller": {"status": "PASS"}, "performance": {"status": "PASS"}},
        binding={"seller": "BOUND", "performance": "BOUND"},
        capabilities={k: "AVAILABLE" for e in ALL_ENTITIES for k in LC.ENTITY_CAPABILITIES[e]},
        required_capabilities=tuple(k for e in ALL_ENTITIES for k in LC.ENTITY_CAPABILITIES[e]),
        history={d: {"completeness_status": "COMPLETE"} for d in L.HISTORICAL_DOMAINS},
        plan_hash="h", chunks={"c1": "DONE", "c2": "DONE"}, last_chunk_done_at=NOW - timedelta(hours=2),
        dq={"run_id": "dq1", "evaluated_at": NOW - timedelta(hours=1),
            "checks": {c: {"status": "PASS", "severity": "BLOCKING"} for c in L.READY_REQUIRED_DQ}},
        maturity_days=14.0)
    for k, v in over.items():
        setattr(s, k, v)
    if "decisions" not in over:
        s.decisions = decisions_for(s.events)
    return s


# ═════════════════════════════════════════ 1–2. runtime не пишет состояние и не подтверждает привязку
RUNTIME_ONLY = ("common.py", "entities.py", "main.py", "promo.py")
CONTROL_WRITE_TARGETS = ("TENANT_STATE_EVENTS", "SELLER_BINDING", "SELLER_IDENTITY_OBSERVATIONS",
                         "BACKFILL_CHECKPOINTS", "CAPABILITY_PROFILE", "HISTORY_BOUNDARIES", "DQ_RESULTS")


@pytest.mark.parametrize("name", RUNTIME_ONLY)
def test_runtime_code_never_writes_control_or_binding_tables(name):
    tree = ast.parse((RUNTIME / name).read_text(encoding="utf-8"))
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in (
                "insert_rows_json", "insert_rows", "load_table_from_json", "load_table_from_file", "query"):
            src = ast.unparse(n)
            assert not any(t in src for t in CONTROL_WRITE_TARGETS), (name, src)
    text = (RUNTIME / name).read_text(encoding="utf-8")
    assert "tenant_ops" not in text and "TENANT_OPS_DATASET" not in text, name


def test_runtime_binding_gate_reads_only_table_metadata():
    src = ast.unparse(next(n for n in ast.walk(ast.parse((RUNTIME / "main.py").read_text(encoding="utf-8")))
                           if isinstance(n, ast.FunctionDef) and n.name == "binding_gate"))
    assert "list_tables" in src and "get_table" in src
    for w in ("insert", "query", "load_table", "update", "delete", "create", "list_rows"):
        assert f".{w}" not in src, w


def test_runtime_honours_owner_hold_before_anything_else(monkeypatch):
    (denied, _), calls = _gate(monkeypatch, [binding_row()], holds=("suspend",))
    assert denied == "tenant:SUSPENDED" and calls == []
    (denied, _), _c = _gate(monkeypatch, [binding_row()], holds=("suspend", "resume"))
    assert denied is None


def test_runtime_sees_revocation_immediately_from_owner_markers(monkeypatch):
    """Отзыв — знак владельца (метаданные): runtime видит его сразу, а не через ~90 мин строк."""
    rev = dict(binding_row(), binding_id="rev-1", status="REVOKED", revoked_at=(NOW - timedelta(minutes=1)).isoformat())
    (denied, _), calls = _gate(monkeypatch, [binding_row(), rev])
    assert denied == "seller:UNBOUND" and calls == []


class _RefBQ:
    """BigQuery runtime: только tables.list / tables.get знаков владельца в ref (READER)."""

    def __init__(self, markers):
        self.markers, self.calls = markers, []

    def list_tables(self, ref):
        self.calls.append(("list_tables", ref))
        return [types.SimpleNamespace(table_id=n) for n in self.markers]

    def get_table(self, ref):
        self.calls.append(("get_table", ref))
        lb, d = self.markers[ref.rsplit(".", 1)[1]]
        return types.SimpleNamespace(labels=lb, description=d)


def _gate(monkeypatch, bindings, fp_company=None, client_id=CLIENT_ID, want=("fbo_postings",), perf_id=PERF_ID,
          holds=()):
    import main as M
    calls = fake_api(monkeypatch, company=fp_company, client_id=client_id, perf_id=perf_id)
    bq = _RefBQ(owner_markers(bindings=bindings, holds=holds))
    monkeypatch.setattr(C, "bq", lambda: bq)
    return M.binding_gate(list(want), NOW), calls


def test_runtime_refuses_without_binding_before_any_api_call(monkeypatch):
    (denied, _), calls = _gate(monkeypatch, [])
    assert denied == "seller:UNBOUND" and calls == []


def test_runtime_refuses_revoked_binding(monkeypatch):
    rows = [binding_row(), dict(binding_row(), binding_id="rev-1", status="REVOKED",
                                revoked_at=(NOW - timedelta(hours=1)).isoformat())]
    (denied, _), _c = _gate(monkeypatch, rows)
    assert denied == "seller:UNBOUND"


def test_runtime_refuses_fingerprint_mismatch(monkeypatch):
    """Ключ заменили на ключ другого кабинета: подтверждение есть, отпечаток прогона — другой."""
    (denied, reason), calls = _gate(monkeypatch, [binding_row()], client_id="5550200")
    assert denied == "seller:MISMATCH" and ("seller", "/v1/seller/info") in calls


def test_runtime_refuses_changed_company_behind_same_client_id(monkeypatch):
    (denied, _), _c = _gate(monkeypatch, [binding_row()], fp_company={"inn": "7700000001", "ogrn": OGRN})
    assert denied == "seller:MISMATCH"


def test_runtime_refuses_incomplete_identity(monkeypatch):
    (denied, _), _c = _gate(monkeypatch, [binding_row()], fp_company={"inn": INN, "ogrn": ""})
    assert denied == "seller:NOT_OBSERVED"


def test_runtime_ads_run_requires_performance_binding(monkeypatch):
    (denied, _), _c = _gate(monkeypatch, [binding_row()], want=("ads_sku_daily",))
    assert denied == "performance:UNBOUND"
    rows = [binding_row(), binding_row(I.PERFORMANCE, PERF_FP, "obs-p", bid="bnd-p")]
    (denied, _), _c = _gate(monkeypatch, rows, want=("ads_sku_daily",), perf_id="other@clients")
    assert denied == "performance:MISMATCH"
    (denied, _), _c = _gate(monkeypatch, rows, want=("ads_sku_daily",))
    assert denied is None


def test_runtime_accepts_exact_binding(monkeypatch):
    (denied, reason), _c = _gate(monkeypatch, [binding_row()])
    assert denied is None and reason == "ok"


def test_future_dated_or_conflicting_binding_is_invalid():
    fut = binding_row(at=NOW + timedelta(hours=1))
    assert I.effective_binding([fut], I.SELLER, NOW).status == I.INVALID_BINDING
    a, b = binding_row(bid="b1"), binding_row(bid="b2", fp="f" * 64)
    assert I.effective_binding([a, b], I.SELLER, NOW).status == I.INVALID_BINDING


def test_binding_without_matching_observation_is_invalid_for_control():
    b = binding_row(obs_id="obs-x")
    assert I.effective_binding([b], I.SELLER, NOW, observations=[obs_row()]).status == I.INVALID_BINDING
    assert I.effective_binding([b], I.SELLER, NOW, observations=[obs_row(obs_id="obs-x", fp="0" * 64)]).status \
        == I.INVALID_BINDING
    assert I.effective_binding([b], I.SELLER, NOW, observations=[obs_row(obs_id="obs-x")]).status == "CONFIRMED"


# ═════════════════════════════════════════ 3. control не обходит валидатор READY
def test_ready_contract_passes_only_when_everything_holds():
    status, failures, event = L.decide(ready_snapshot(), L.READY, "CONTROL:r", "AUTO_READY", {"x": 1}, "r")
    assert status == "WRITE" and not failures and event["to_state"] == L.READY


READY_BREAKERS = {
    "binding mismatch": dict(binding={"seller": "MISMATCH", "performance": "BOUND"}),
    "performance unbound": dict(binding={"seller": "BOUND", "performance": "UNBOUND"}),
    "seller key failed": dict(credentials={"seller": {"status": "FAIL"}, "performance": {"status": "PASS"}}),
    "capability denied": dict(capabilities={("seller", "catalog"): "DENIED"}),
    "history partial": dict(history={d: {"completeness_status": "PARTIAL"} for d in L.HISTORICAL_DOMAINS}),
    "history missing": dict(history={}),
    "history unknown": dict(history={d: {"completeness_status": "UNKNOWN"} for d in L.HISTORICAL_DOMAINS}),
    "chunk not done": dict(chunks={"c1": "DONE", "c2": "FAILED"}),
    "chunk permanent": dict(chunks={"c1": "FAILED_PERMANENT"}),
    "no chunks": dict(chunks={}),
    "no maturity (D3)": dict(maturity_days=None),
    "zero maturity": dict(maturity_days=0),
    "no dq": dict(dq=None),
    "dq older than last chunk": dict(last_chunk_done_at=NOW - timedelta(minutes=10)),
    "blocking dq": dict(dq={"run_id": "d", "evaluated_at": NOW, "checks": dict(
        {c: {"status": "PASS", "severity": "BLOCKING"} for c in L.READY_REQUIRED_DQ},
        EXTRA={"status": "FAIL", "severity": "BLOCKING"})}),
    "required dq missing": dict(dq={"run_id": "d", "evaluated_at": NOW, "checks": {
        c: {"status": "PASS", "severity": "BLOCKING"} for c in L.READY_REQUIRED_DQ if c != "TRUNCATION"}}),
}


@pytest.mark.parametrize("name", sorted(READY_BREAKERS))
def test_ready_is_rejected_when_any_condition_fails(name):
    status, failures, event = L.decide(ready_snapshot(**READY_BREAKERS[name]), L.READY, "CONTROL:r",
                                       "AUTO_READY", {}, "r")
    assert status == "REJECT" and failures and event is None, name


def op_decide(s, to, reason="OP", **fields):
    """Ребро оператора: решение владельца (как пишет tenant_lifecycle.py) + decide с его id."""
    dec = {"decision_id": "dec-new", "expect_state": L.current_state(s.events), "to_state": to, **fields}
    s.decisions = {**(s.decisions or {}), L.next_seq(s.events): dec}
    return L.decide(s, to, "OPERATOR:owner", reason, {}, "operator:x", decision_id="dec-new")


def test_no_activity_domain_does_not_block_ready():
    h = {d: {"completeness_status": "COMPLETE"} for d in L.HISTORICAL_DOMAINS}
    h["ads_sku_daily"] = {"completeness_status": "NOT_APPLICABLE"}
    assert L.decide(ready_snapshot(history=h), L.READY, "CONTROL:r", "A", {}, "r")[0] == "WRITE"


class _OwnerTables:
    """Tables владельца (tenant_tables.Tables) без облака: строки, маркеры tenant_locks, дописывание."""

    def __init__(self, t, events=(), ref_markers=None):
        self.t = t
        self.markers = {L.marker_name(e["seq"]): (L.marker_labels(e), I.as_utc(e["occurred_at"])) for e in events}
        self.refm = dict(ref_markers or {})

    def rows(self, dataset, table):
        return iter(copy.deepcopy(self.t.get((dataset, table), [])))

    def append(self, dataset, table, rows):
        self.t.setdefault((dataset, table), []).extend(copy.deepcopy(rows))

    def list_tables(self, dataset, with_expiry=False):
        tail = (None,) if with_expiry else ()
        if dataset == "ref":
            return [(n, lb, NOW) + tail for n, (lb, _d) in self.refm.items()]
        assert dataset == "tenant_locks"
        return [(n, lb, c) + tail for n, (lb, c) in self.markers.items()]

    def get_table(self, dataset, name):
        assert dataset == "ref"
        return self.refm.get(name)

    def series(self, dataset, prefix):
        return sorted((n, lb, d) for n, (lb, d) in self.refm.items() if n.startswith(prefix))

    def create_marker(self, dataset, name, labels, description):
        store = self.refm if dataset == "ref" else self.markers
        if name in store:
            return False
        store[name] = (labels, description) if dataset == "ref" else (labels, NOW)
        return True


def _owner_contract():
    return {"tenant_id": "client_x", "datasets": {"tenant_ops": "tenant_ops", "ref": "ref", "tenant_locks": "tenant_locks"},
            "marketplaces": {"ozon": {"jobs": {"j": {"entities": ["fbo_postings"]}}}}}


def _cap(api, cap, status):
    return {"api": api, "capability": cap, "status": status, "discovered_at": (NOW - timedelta(hours=1)).isoformat()}


def _owner_tables(case="ok"):
    chunks = CK.plan("fbo_postings", date(2026, 1, 1), date(2026, 1, 14))
    ph = CK.plan_hash(chunks)
    ledger = [_chunk_row(c, "PENDING", NOW - timedelta(hours=2), run_id="ctl", plan_hash=ph) for c in chunks]
    evs = chain(L.CREDENTIALS_PENDING, L.VALIDATING, L.CAPABILITY_DISCOVERY, L.READY_FOR_BACKFILL)
    binds = [binding_row()]
    caps = [_cap("seller", "credential_read_only", "AVAILABLE")]
    if case == "no binding":
        binds = []
    elif case == "revoked":
        binds.append(dict(binding_row(), binding_id="rev", status="REVOKED", revoked_at=(NOW - timedelta(minutes=5)).isoformat()))
    elif case == "key rejected":
        caps.append(dict(_cap("seller", "credential_read_only", "DENIED"), discovered_at=NOW.isoformat()))
    t = _OwnerTables({("tenant_ops", "BACKFILL_CHECKPOINTS"): ledger,
                      ("tenant_ops", "SELLER_IDENTITY_OBSERVATIONS"): [obs_row()], ("ref", "SELLER_BINDING"): binds,
                      ("tenant_ops", "CAPABILITY_PROFILE"): caps}, evs, owner_markers(evs, bindings=binds))
    return t, ph


@pytest.mark.parametrize("case,expect", [("ok", "WRITE"), ("no binding", "REJECT"), ("revoked", "REJECT"),
                                         ("key rejected", "REJECT"), ("wrong hash", "REJECT")])
def test_operator_plan_approval_uses_confirmed_binding_and_exact_hash(case, expect):
    sys.path.insert(0, str(RUNTIME.parents[2]))
    from tools.tenancy import tenant_lifecycle as TL
    t, ph = _owner_tables(case)
    status, text = TL.transition(_owner_contract(), t, NOW, L.BACKFILLING, "OPERATOR:owner", "APPROVE",
                                 ph if case != "wrong hash" else "0" * 64)
    assert status == expect, (case, text)
    if expect == "WRITE":
        dec = t.t[("ref", "OPERATOR_DECISIONS")][-1]
        assert dec["plan_hash"] == ph and dec["expect_state"] == L.READY_FOR_BACKFILL
        chain_now = L.chain_from_markers(t.list_tables("tenant_locks"))
        assert L.current_state(chain_now) == L.BACKFILLING
        _c, decisions, _l, _r, _h = TL.read_state(_owner_contract(), t)
        assert L.audit_history(chain_now, decisions) == []
        assert sorted(n for n in t.refm if n.startswith("OPD_"))[-1] == L.decision_marker_name(5)
    else:
        assert L.current_state(L.chain_from_markers(t.list_tables("tenant_locks"))) == L.READY_FOR_BACKFILL


def test_owner_concurrent_transition_loses_on_marker():
    sys.path.insert(0, str(RUNTIME.parents[2]))
    from tools.tenancy import tenant_lifecycle as TL
    t, ph = _owner_tables()
    t.create_marker("tenant_locks", L.marker_name(5), {"to": "suspended"}, "параллельный")     # номер уже занят
    status, _text = TL.transition(_owner_contract(), t, NOW, L.BACKFILLING, "OPERATOR:owner", "APPROVE", ph)
    assert status in ("CONFLICT", "REJECT")
    assert not [r for r in t.t.get(("tenant_ops", "TENANT_STATE_EVENTS"), []) if r.get("to_state") == L.BACKFILLING]


def test_owner_reopen_references_done_run_and_is_clock_free():
    sys.path.insert(0, str(RUNTIME.parents[2]))
    from tools.tenancy import tenant_lifecycle as TL
    t, _ph = _owner_tables()
    c = CK.plan("fbo_postings", date(2026, 1, 1), date(2026, 1, 14))[0]
    t.t[("tenant_ops", "BACKFILL_CHECKPOINTS")].append(_chunk_row(c, "DONE", NOW + timedelta(hours=5), run_id="bf-a"))
    status, _txt = TL.reopen_chunk(_owner_contract(), t, NOW - timedelta(days=3), "OPERATOR:owner", c.chunk_id, "ремонт")
    assert status == "WRITE"
    dec = t.t[("ref", "OPERATOR_DECISIONS")][-1]
    assert dec["decision_type"] == "REOPEN_CHUNK" and dec["reopens_run_id"] == "bf-a"
    ledger = t.t[("tenant_ops", "BACKFILL_CHECKPOINTS")]
    assert CK.fold(ledger, TL.reopened(t.t[("ref", "OPERATOR_DECISIONS")]))[c.chunk_id]["status"] == "PENDING"


def test_operator_cannot_set_ready_and_there_is_no_set_state_command():
    status, failures, _ = L.decide(ready_snapshot(), L.READY, "OPERATOR:owner", "MANUAL", {}, "r")
    assert status == "REJECT" and "OPERATOR" in failures[0]
    sys.path.insert(0, str(RUNTIME.parents[2]))
    from tools.tenancy import tenant_lifecycle as TL
    assert L.READY not in TL.OPERATOR_TARGETS.values()
    assert "set-state" not in (RUNTIME.parents[2] / "tools/tenancy/tenant_lifecycle.py").read_text(encoding="utf-8")


def test_control_writes_state_events_only_after_decide():
    """Единственная запись TENANT_STATE_EVENTS в lifecycle.py — событие, выданное decide()."""
    tree = ast.parse((RUNTIME / "lifecycle.py").read_text(encoding="utf-8"))
    writers = []
    for fn in (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)):
        for c in ast.walk(fn):
            if isinstance(c, ast.Call) and "TENANT_STATE_EVENTS" in ast.unparse(c) and ".append(" in ast.unparse(c):
                writers.append((fn.name, ast.unparse(c)))
    assert writers == []                                  # строк журнала напрямую нет вовсе
    callers = sorted({fn.name for fn in ast.walk(tree) if isinstance(fn, ast.FunctionDef)
                      for c in ast.walk(fn) if isinstance(c, ast.Call) and "record_transition" in ast.unparse(c.func)})
    assert callers == ["cmd_advance"]
    body = ast.unparse(next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "cmd_advance"))
    assert "L.decide(" in body and "if status == 'WRITE':" in body


def test_state_jump_is_rejected():
    s = ready_snapshot(events=chain(L.CREDENTIALS_PENDING, L.VALIDATING))
    for to in (L.READY, L.READY_FOR_BACKFILL, L.BACKFILLING, L.RECONCILING):
        status, failures, _ = L.decide(s, to, "CONTROL:r", "X", {}, "r")
        assert status == "REJECT" and ("не разрешено" in failures[0] or "не может" in failures[0]), to


def test_corrupted_journal_blocks_every_transition_except_suspend():
    bad = chain(L.CREDENTIALS_PENDING, L.VALIDATING) + [dict(ev(L.VALIDATING, L.READY, "CONTROL:x", NOW - timedelta(minutes=1)), seq=3)]
    s = ready_snapshot(events=bad)
    for to in (L.CAPABILITY_DISCOVERY, L.VALIDATING, L.RECONCILING):
        status, failures, _ = L.decide(s, to, "CONTROL:c", "X", {}, "r")
        assert status == "REJECT" and "журнал состояний некорректен" in failures[0], to
    status, _f, event = L.decide(s, L.SUSPENDED, "OPERATOR:o", "INCIDENT", {}, "r")
    assert status == "WRITE" and event["seq"] == 4                  # приостановка возможна всегда
    assert L.decide(s, L.SUSPENDED, "OPERATOR:o", "", {}, "r")[0] == "REJECT"       # но только с причиной


def test_forged_operator_edge_is_rejected_by_audit():
    """control записал маркер «OPERATOR» сам: решения владельца в ref нет — журнал недействителен."""
    evs = chain(L.CREDENTIALS_PENDING, L.VALIDATING, L.CAPABILITY_DISCOVERY, L.READY_FOR_BACKFILL, L.BACKFILLING)
    decisions = decisions_for(evs)
    decisions.pop(evs[-1]["seq"])
    s = ready_snapshot(events=evs, decisions=decisions)
    assert L.audit_history(evs, decisions)
    assert L.decide(s, L.RECONCILING, "CONTROL:c", "X", {}, "r")[0] == "REJECT"
    st, failures, _ = L.decide(ready_snapshot(events=evs[:4]), L.BACKFILLING, "OPERATOR:forged", "X", {}, "r",
                               decision_id="dec-missing")
    assert st == "REJECT" and "решения владельца" in failures[0]


def test_clock_skew_does_not_reorder_seq_chain():
    evs = chain(L.CREDENTIALS_PENDING, L.VALIDATING, L.CAPABILITY_DISCOVERY)
    evs[1]["occurred_at"] = (NOW + timedelta(days=1)).isoformat()         # часы ноутбука впереди
    assert L.current_state(evs) == L.CAPABILITY_DISCOVERY and L.audit_history(evs, decisions_for(evs)) == []


def test_seq_gap_is_a_violation():
    evs = chain(L.CREDENTIALS_PENDING, L.VALIDATING)
    evs[1]["seq"] = 3
    assert any("подряд" in p for p in L.audit_history(evs, decisions_for(evs)))


def test_transition_is_idempotent_on_same_evidence():
    s = ready_snapshot()
    _st, _f, event = L.decide(s, L.READY, "CONTROL:r", "AUTO_READY", {"dq": "dq1"}, "r")
    s.events = s.events + [event]
    assert L.decide(s, L.READY, "CONTROL:r", "AUTO_READY", {"dq": "dq1"}, "r2")[0] == "NOOP"


def test_maturity_is_required_only_for_ready_d3():
    """D3: VALIDATING, CAPABILITY_DISCOVERY и BACKFILLING без срока созревания не блокируются."""
    base = dict(maturity_days=None)
    s = ready_snapshot(events=chain(L.CREDENTIALS_PENDING, L.VALIDATING), **base)
    assert L.decide(s, L.CAPABILITY_DISCOVERY, "CONTROL:r", "A", {}, "r")[0] == "WRITE"
    s = ready_snapshot(events=chain(L.CREDENTIALS_PENDING, L.VALIDATING, L.CAPABILITY_DISCOVERY), **base)
    assert L.decide(s, L.READY_FOR_BACKFILL, "CONTROL:r", "A", {}, "r")[0] == "WRITE"
    s = ready_snapshot(events=chain(L.CREDENTIALS_PENDING, L.VALIDATING, L.CAPABILITY_DISCOVERY,
                                    L.READY_FOR_BACKFILL), operator_plan_hash="h", **base)
    assert op_decide(s, L.BACKFILLING)[0] == "WRITE"
    s = ready_snapshot(**base)
    st, failures, _ = L.decide(s, L.READY, "CONTROL:r", "A", {}, "r")
    assert st == "REJECT" and any("D3" in f for f in failures)


def test_operator_must_approve_exact_plan_hash():
    s = ready_snapshot(events=chain(L.CREDENTIALS_PENDING, L.VALIDATING, L.CAPABILITY_DISCOVERY,
                                    L.READY_FOR_BACKFILL), operator_plan_hash="other")
    st, failures, _ = op_decide(s, L.BACKFILLING)
    assert st == "REJECT" and any("хеш" in f for f in failures)


def test_credentials_inserted_requires_four_secret_versions():
    s = ready_snapshot(events=chain(L.CREDENTIALS_PENDING), secret_versions={"a": 1, "b": 1, "c": 1})
    assert op_decide(s, L.VALIDATING)[0] == "REJECT"
    s.secret_versions = {"a": 1, "b": 1, "c": 1, "d": 0}
    assert op_decide(s, L.VALIDATING)[0] == "REJECT"
    s.secret_versions = {"a": 1, "b": 1, "c": 1, "d": 2}
    assert op_decide(s, L.VALIDATING)[0] == "WRITE"
    s.decisions = {}
    assert L.decide(s, L.VALIDATING, "OPERATOR:o", "C", {}, "r")[0] == "REJECT"          # без знака OPD


def test_dq_blocking_failures_block_ready_end_to_end():
    facts = {"binding": {"seller": "BOUND"}, "chunks": {"c": "DONE"}, "required_ranges": {},
             "done_windows": {}, "truncated_runs": ["bf-1"], "raw_duplicate_keys": {"T": 0},
             "finance_unresolved_rows": 0, "maturity_days": 10}
    checks = DQ.evaluate(facts)
    assert DQ.blocking_failures(checks) == ["TRUNCATION"]
    s = ready_snapshot(dq={"run_id": "d", "evaluated_at": NOW,
                           "checks": {c: {"status": v["status"], "severity": v["severity"]} for c, v in checks.items()}})
    st, failures, _ = L.decide(s, L.READY, "CONTROL:r", "A", {}, "r")
    assert st == "REJECT" and any("TRUNCATION" in f for f in failures)


def test_dq_warnings_do_not_block():
    facts = {"binding": {"seller": "BOUND"}, "chunks": {"c": "DONE"}, "required_ranges": {},
             "done_windows": {}, "truncated_runs": [], "raw_duplicate_keys": {"T": 0},
             "finance_unresolved_rows": 0, "maturity_days": 10, "ads_sum_worst_pct": 40.0,
             "fbs_activity": True, "key_expires_soon": True}
    checks = DQ.evaluate(facts)
    assert DQ.blocking_failures(checks) == []
    assert {c for c, v in checks.items() if v["status"] == "FAIL"} == {"ADS_SUM_RECONCILIATION", "FBS_ACTIVITY",
                                                                        "KEY_EXPIRY"}


def test_dq_fbo_coverage_needs_both_utc_days_for_msk_day():
    need = {"fbo_postings": (date(2026, 1, 2), date(2026, 1, 3))}
    only_d = DQ.evaluate({"required_ranges": need, "done_windows": {"fbo_postings": [(date(2026, 1, 2), date(2026, 1, 3))]}})
    assert only_d["COVERAGE"]["status"] == "FAIL"          # сутки МСК 02.01 требуют UTC 01.01
    ok = DQ.evaluate({"required_ranges": need, "done_windows": {"fbo_postings": [(date(2026, 1, 1), date(2026, 1, 3))]}})
    assert ok["COVERAGE"]["status"] == "PASS"


def test_dq_unknown_uniqueness_is_not_pass():
    assert DQ.evaluate({"raw_duplicate_keys": None})["RAW_UNIQUENESS"]["status"] == "FAIL"
    assert DQ.evaluate({"finance_unresolved_rows": None})["FIN_CLASSIFICATION"]["status"] == "FAIL"


# ═════════════════════════════════════════ 4–5. наблюдения и подтверждение привязки
def test_observation_status_rejects_stale_multiple_empty_and_non_observed():
    fresh = obs_row(at=NOW - timedelta(hours=1))
    assert I.observation_status([fresh], I.SELLER, NOW)[0] == fresh
    assert I.observation_status([obs_row(at=NOW - timedelta(hours=25))], I.SELLER, NOW)[0] is None
    two = [fresh, obs_row(obs_id="obs-2", fp="e" * 64, at=NOW - timedelta(hours=1))]
    assert I.observation_status(two, I.SELLER, NOW)[0] is None
    assert I.observation_status([obs_row(fp="", at=NOW)], I.SELLER, NOW)[0] is None
    assert I.observation_status([obs_row(status="FOREIGN_SKUS", at=NOW)], I.SELLER, NOW)[0] is None
    assert I.observation_status([], I.SELLER, NOW)[0] is None


def _binding_tool():
    sys.path.insert(0, str(RUNTIME.parents[2]))
    from tools.tenancy import tenant_binding as TB
    return TB


def test_owner_confirm_happy_path_and_idempotency():
    TB = _binding_tool()
    o = obs_row(at=NOW - timedelta(hours=1))
    st, why, row = TB.decide_confirm([o], opb_items([]), I.SELLER, "obs-1", SELLER_FP, "NONE", NOW, "OPERATOR:owner")
    assert st == "WRITE" and row["status"] == "CONFIRMED" and row["identity_fingerprint"] == SELLER_FP
    assert row["source_observation_id"] == "obs-1" and row["confirmed_by"] == "OPERATOR:owner"
    st2, _w, _r = TB.decide_confirm([o], opb_items([row]), I.SELLER, "obs-1", SELLER_FP, row["binding_id"], NOW,
                                    "OPERATOR:owner")
    assert st2 == "NOOP"


@pytest.mark.parametrize("case", ["stale", "multiple", "wrong fingerprint", "wrong observation", "concurrent",
                                  "not observed", "invalid current"])
def test_owner_confirm_rejections(case):
    TB = _binding_tool()
    o = obs_row(at=NOW - timedelta(hours=1))
    obs, binds, oid, fp, expect = [o], [], "obs-1", SELLER_FP, "NONE"
    if case == "stale":
        obs = [obs_row(at=NOW - timedelta(hours=30))]
    elif case == "multiple":
        obs = [o, obs_row(obs_id="obs-2", fp="d" * 64, at=NOW - timedelta(hours=1))]
    elif case == "wrong fingerprint":
        fp = "c" * 64
    elif case == "wrong observation":
        oid = "obs-9"
    elif case == "concurrent":
        binds = [binding_row(bid="bnd-other", obs_id="obs-0")]      # параллельно подтвердили другое
    elif case == "not observed":
        obs = [obs_row(status="NO_EVIDENCE", at=NOW - timedelta(hours=1))]
    elif case == "invalid current":
        binds, expect = [binding_row(at=NOW + timedelta(hours=1), bid="bnd-f")], "bnd-f"
    st, why, row = TB.decide_confirm(obs, opb_items(binds), I.SELLER, oid, fp, expect, NOW, "OPERATOR:owner")
    assert st == "REJECT" and row is None, (case, why)


def test_owner_revoke_requires_current_event_and_reason():
    TB = _binding_tool()
    b = binding_row()
    items = opb_items([b])
    assert TB.decide_revoke(items, I.SELLER, "bnd-x", "r", NOW, "OPERATOR:o")[0] == "REJECT"
    assert TB.decide_revoke(items, I.SELLER, "OPB_seller_0001", "", NOW, "OPERATOR:o")[0] == "REJECT"
    st, _w, row = TB.decide_revoke(items, I.SELLER, "OPB_seller_0001", "ротация", NOW, "OPERATOR:o")
    assert st == "WRITE" and row["status"] == "REVOKED"
    assert I.binding_from_markers(opb_items([b, row]), I.SELLER, NOW).status == I.UNBOUND


def test_control_cannot_write_binding():
    assert "SELLER_BINDING" not in APPEND_TABLES
    with pytest.raises(Exception):
        ControlStore(None, "p", {"tenant_ops": "o", "tenant_locks": "l", "ref": "r", "ozon_raw": "w"}).append(
            "SELLER_BINDING", [{"x": 1}])


def test_validate_records_observation_but_never_binding(monkeypatch):
    fake_api(monkeypatch)
    store = FakeStore()
    assert LC.cmd_validate(make_ctx(store)) == 0
    tables = {t for t, _r in store.appended}
    assert tables == {"SELLER_IDENTITY_OBSERVATIONS", "CAPABILITY_PROFILE"}
    obs = store.t[("tenant_ops", "SELLER_IDENTITY_OBSERVATIONS")]
    assert {o["api"]: o["status"] for o in obs} == {"seller": "OBSERVED", "performance": "OBSERVED"}
    assert obs[0]["identity_fingerprint"] == SELLER_FP


def test_performance_foreign_skus_is_not_observed(monkeypatch):
    fake_api(monkeypatch, cpc_skus=("111", "999"))
    store = FakeStore()
    LC.cmd_validate(make_ctx(store))
    perf = [o for o in store.t[("tenant_ops", "SELLER_IDENTITY_OBSERVATIONS")] if o["api"] == "performance"][0]
    assert perf["status"] == "FOREIGN_SKUS"


def test_performance_without_cpc_evidence_is_not_observed(monkeypatch):
    fake_api(monkeypatch, cpc_skus=())
    store = FakeStore()
    LC.cmd_validate(make_ctx(store))
    perf = [o for o in store.t[("tenant_ops", "SELLER_IDENTITY_OBSERVATIONS")] if o["api"] == "performance"][0]
    assert perf["status"] == "NO_EVIDENCE"


# ═════════════════════════════════════════ 6–7. ключ Seller: методы изменения, неизвестные, срок
def test_read_only_key_passes():
    v = CR.evaluate_seller_roles(roles_ok(), ALL_ENTITIES, NOW)
    assert v["status"] == "PASS" and not v["blocking"]


def test_mutation_capable_key_is_rejected_and_not_modified():
    v = CR.evaluate_seller_roles(roles_ok(extra=("/v1/cargoes/create",)), ALL_ENTITIES, NOW)
    assert v["status"] == "FAIL" and any(b.startswith("KEY_MUTATION_CAPABLE") for b in v["blocking"])
    assert v["mutation_methods"] == ["/v1/cargoes/create"]


def test_approved_mutation_list_is_empty_and_explicit():
    assert CR.load_policy()["approved_mutation_methods"] == []
    pol = copy.deepcopy(CR.load_policy())
    pol["approved_mutation_methods"] = ["/v1/cargoes/create"]
    with pytest.raises(ValueError):
        CR.evaluate_seller_roles(roles_ok(extra=("/v1/cargoes/create",)), ALL_ENTITIES, NOW, pol)


def test_unknown_method_is_blocking_not_guessed():
    v = CR.evaluate_seller_roles(roles_ok(extra=("/v9/brand-new/method",)), ALL_ENTITIES, NOW)
    assert v["status"] == "FAIL" and any(b.startswith("KEY_UNKNOWN_METHODS") for b in v["blocking"])


def test_role_name_is_irrelevant_only_methods_count():
    r = roles_ok(extra=("/v2/products/delete",))
    r["roles"][0]["name"] = "Только чтение"
    assert CR.evaluate_seller_roles(r, ALL_ENTITIES, NOW)["status"] == "FAIL"


@pytest.mark.parametrize("expires,status,code", [
    ("2026-09-01T00:00:00Z", "FAIL", "KEY_EXPIRED"), (NOW.isoformat(), "FAIL", "KEY_EXPIRED"),
    ("2026-10-10T00:00:00Z", "PASS", "KEY_EXPIRES_SOON"), (None, "PASS", "KEY_EXPIRY_UNKNOWN"),
    ("вчера", "FAIL", "KEY_EXPIRY_UNPARSEABLE"),
])
def test_key_expiry(expires, status, code):
    v = CR.evaluate_seller_roles(roles_ok(expires=expires), ALL_ENTITIES, NOW)
    assert v["status"] == status and any(x.startswith(code) for x in v["blocking"] + v["warnings"])


def test_missing_required_method_and_empty_roles():
    r = roles_ok()
    r["roles"][0]["methods"].remove("/v3/posting/fbo/list")
    assert "KEY_MISSING_REQUIRED" in " ".join(CR.evaluate_seller_roles(r, ALL_ENTITIES, NOW)["blocking"])
    assert CR.evaluate_seller_roles({}, ALL_ENTITIES, NOW)["status"] == "FAIL"
    assert CR.evaluate_seller_roles({"roles": "x"}, ALL_ENTITIES, NOW)["status"] == "FAIL"


def test_rejected_key_yields_credential_rejected_observation(monkeypatch):
    fake_api(monkeypatch, roles=roles_ok(extra=("/v1/cargoes/create",)))
    store = FakeStore()
    assert LC.cmd_validate(make_ctx(store, entities=("catalog",))) == 1
    o = store.t[("tenant_ops", "SELLER_IDENTITY_OBSERVATIONS")][0]
    assert o["status"] == "CREDENTIAL_REJECTED"
    TB = _binding_tool()
    st, _w, _r = TB.decide_confirm([o], opb_items([]), I.SELLER, o["observation_id"], o["identity_fingerprint"], "NONE",
                                   NOW + timedelta(minutes=1), "OPERATOR:owner")
    assert st == "REJECT"


def test_performance_evaluation_never_claims_read_only():
    v = CR.evaluate_performance(True, True)
    assert v["status"] == "PASS" and v["warnings"] == ["PERF_MUTATION_CAPABILITY_UNKNOWN: у Performance API нет инспекции прав"]
    assert CR.evaluate_performance(False, False)["status"] == "FAIL"


@pytest.mark.parametrize("path", ["/api/client/campaign/1/activate", "/api/client/search_promo/products/delete",
                                  "/api/client/all_sku_promo/activate", "/api/client/campaign/cpc/v2/product"])
def test_performance_mutating_paths_are_not_callable(path):
    with pytest.raises(C.ApiPathDenied):
        C.perf_get(path)


def test_seller_paths_used_by_control_are_all_read_only():
    pol = CR.load_policy()["methods"]
    assert all(pol[p]["class"] == "READ" for p in C.SELLER_ALLOWED_PATHS), \
        {p: pol[p]["class"] for p in C.SELLER_ALLOWED_PATHS if pol[p]["class"] != "READ"}


# ═════════════════════════════════════════ 8. секреты не уходят (control-процесс)
SECRET = 'SYNTH-ctl-key"with\\esc-ключ'
_STUBS = ("import sys, types\n"
          "b, s, c, g = (types.ModuleType(n) for n in ('google.cloud.bigquery','google.cloud.secretmanager','google.cloud','google'))\n"
          "b.Client = object; b.SchemaField = lambda *a, **k: None\n"
          "b.LoadJobConfig = b.QueryJobConfig = b.ScalarQueryParameter = b.ArrayQueryParameter = object\n"
          "b.SourceFormat = types.SimpleNamespace(NEWLINE_DELIMITED_JSON='N'); b.WriteDisposition = types.SimpleNamespace(WRITE_APPEND='A')\n"
          "s.SecretManagerServiceClient = object\n"
          "c.bigquery, c.secretmanager, g.cloud = b, s, c\n"
          "sys.modules.update({'google':g,'google.cloud':c,'google.cloud.bigquery':b,'google.cloud.secretmanager':s})\n")


def _forms(v):
    import urllib.parse
    return {v, json.dumps(v)[1:-1], json.dumps(v, ensure_ascii=False)[1:-1], repr(v)[1:-1],
            urllib.parse.quote(v, safe=""), urllib.parse.quote_plus(v)}


@pytest.mark.parametrize("echo", ["raw", "json", "repr", "url", "header", "chain"])
def test_control_process_never_leaks_seller_key(echo):
    code = _STUBS + textwrap.dedent(f"""
        import json, types, urllib.parse
        sys.path.insert(0, {str(RUNTIME)!r})
        import common as C, lifecycle as LC
        key = {SECRET!r}
        vals = {{C.CONFIG.secret_seller_client_id: {CLIENT_ID!r}, C.CONFIG.secret_seller_api_key: key,
                 C.CONFIG.secret_perf_client_id: 'p@c', C.CONFIG.secret_perf_client_secret: 'ps'}}
        class SM:
            def access_secret_version(self, request):
                n = request["name"].split("/secrets/")[1].split("/")[0]
                return types.SimpleNamespace(payload=types.SimpleNamespace(data=vals[n].encode()))
        C._sm = SM()
        echo = {echo!r}
        def fake(req, *a, **k):
            k_ = req.get_header("Api-key")
            body = {{"raw": k_, "json": json.dumps({{"k": k_}}), "repr": repr({{"k": k_}}),
                     "url": urllib.parse.quote(k_, safe=""), "header": str(req.headers), "chain": k_}}[echo]
            if echo == "chain":
                try:
                    raise ValueError("inner " + k_)
                except ValueError as e:
                    raise RuntimeError("outer " + repr(k_)) from e
            return 401, {{"_error": body, "company": {{"name": body}}}}
        C._request = fake
        rows = []
        class NotFound(Exception):
            code = 404
        class BQ:
            def list_rows(self, ref):
                return []
            def get_table(self, ref):
                raise NotFound(ref)
            def insert_rows_json(self, ref, r, row_ids=None):
                rows.extend(r); print("WROTE " + json.dumps(r, ensure_ascii=False)); return []
        C._bq = BQ()
        import os
        os.environ.update({{"TENANT_ID": "client_x", "TENANT_OPS_DATASET": "tenant_ops",
                            "TENANT_LOCKS_DATASET": "tenant_locks", "ENABLED_ENTITIES": "catalog"}})
        sys.exit(LC.main(["validate"]))
    """)
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60,
                       env={"PATH": os.environ["PATH"], "GCP_PROJECT_ID": "offline-test-project"})
    blob = r.stdout + r.stderr
    assert r.returncode != 0
    leaked = [f for f in _forms(SECRET) if f in blob]
    assert not leaked, (echo, leaked, blob[-600:])
    if echo != "chain":
        assert "WROTE" in r.stdout                               # наблюдение записано, без ключа
    else:
        assert "<redacted:seller_api_key>" in r.stderr           # трассировка сохранена, ключ вырезан


# ═════════════════════════════════════════ 9–11. аренда и чекпойнты
def _chunk_row(c, status, at, run_id="r", attempts=1, err=None, plan_hash=None):
    return {"backfill_id": c.chunk_id, "entity": c.domain, "window_from": str(c.start), "window_to": str(c.end),
            "status": status, "attempts": attempts, "run_id": run_id, "error_code": err, "updated_at": at.isoformat(),
            "plan_hash": plan_hash}


C1 = CK.Chunk("fbo_postings", date(2026, 1, 1), date(2026, 1, 7))


def test_double_claim_only_one_wins():
    leases = {}
    store = FakeStore(leases=leases)
    g1 = CK.next_lease_generation(store.leases(), C1.chunk_id, NOW)
    g2 = CK.next_lease_generation(store.leases(), C1.chunk_id, NOW)       # оба видят свободно
    assert g1 == g2 == 1
    assert store.create_lease(CK.lease_name(C1.chunk_id, g1), NOW + CK.LEASE_TTL, "a") is True
    assert store.create_lease(CK.lease_name(C1.chunk_id, g2), NOW + CK.LEASE_TTL, "b") is False


def test_live_lease_blocks_and_expired_lease_is_taken_over():
    live = {CK.lease_name(C1.chunk_id, 1): NOW + timedelta(minutes=5)}
    assert CK.next_lease_generation(live.items(), C1.chunk_id, NOW) is None
    # Срок вышел, но допуск видимости журнала прогонов (3 ч) ещё не прошёл — аренда держится.
    recent = {CK.lease_name(C1.chunk_id, 1): NOW - timedelta(minutes=5)}
    assert CK.next_lease_generation(recent.items(), C1.chunk_id, NOW) is None
    expired = {CK.lease_name(C1.chunk_id, 1): NOW - CK.VISIBILITY_GRACE - timedelta(minutes=5)}
    assert CK.next_lease_generation(expired.items(), C1.chunk_id, NOW) == 2
    mixed = {CK.lease_name(C1.chunk_id, 1): NOW - timedelta(hours=5), CK.lease_name(C1.chunk_id, 2): NOW + timedelta(hours=1)}
    assert CK.next_lease_generation(mixed.items(), C1.chunk_id, NOW) is None
    unbounded = {CK.lease_name(C1.chunk_id, 1): None}
    assert CK.next_lease_generation(unbounded.items(), C1.chunk_id, NOW) is None


def test_generation_is_never_reused_after_table_expiry():
    """Таблица поколения 1 удалена BigQuery по сроку: номер берётся из журнала, 1 не повторяется."""
    assert CK.next_lease_generation([], C1.chunk_id, NOW, ledger_generations=[1, 2]) == 3
    assert CK.next_lease_generation([], C1.chunk_id, NOW, ledger_generations=[None]) == 1


def test_lease_table_outlives_lease_and_carries_until_label():
    class Client:
        created = []

        def create_table(self, t, exists_ok=False):
            self.created.append(t)
    cl = Client()
    st = ControlStore(cl, "p", {"tenant_ops": "o", "tenant_locks": "locks", "ref": "r", "ozon_raw": "w"})
    until = NOW + CK.LEASE_TTL
    assert st.create_lease(CK.lease_name(C1.chunk_id, 1), until, "ctl-x")
    t = cl.created[0]
    assert t.labels["until"] == str(int(until.timestamp())) and t.expires == until + CK.LEASE_TABLE_KEEP


def test_other_chunks_leases_are_ignored():
    other = CK.Chunk("fbo_postings", date(2026, 1, 8), date(2026, 1, 14))
    assert CK.next_lease_generation({CK.lease_name(other.chunk_id, 1): NOW + timedelta(hours=1)}.items(),
                                    C1.chunk_id, NOW) == 1


def test_done_is_immutable_until_operator_reopens():
    t = NOW - timedelta(days=1)
    rows = [_chunk_row(C1, "PENDING", t), _chunk_row(C1, "RUNNING", t + timedelta(minutes=1)),
            _chunk_row(C1, "DONE", t + timedelta(minutes=2), run_id="bf-1"),
            _chunk_row(C1, "FAILED", t + timedelta(minutes=3), run_id="bf-2"),
            _chunk_row(C1, "RUNNING", t + timedelta(minutes=4), run_id="bf-3"),
            _chunk_row(C1, "REOPENED", t + timedelta(minutes=5), run_id="operator:forged-by-control")]
    st = CK.fold(rows)[C1.chunk_id]
    assert st["status"] == "DONE" and st["run_id"] == "bf-1"            # строка журнала ремонтом не является
    assert CK.fold(rows, frozenset({"bf-other"}))[C1.chunk_id]["status"] == "DONE"
    reopened = CK.fold(rows, frozenset({"bf-1"}))[C1.chunk_id]            # решение владельца отменяет bf-1
    assert reopened["status"] == "RUNNING" and reopened["run_id"] == "bf-3"
    rows.append(_chunk_row(C1, "DONE", t + timedelta(minutes=7), run_id="bf-4"))
    assert CK.fold(rows, frozenset({"bf-1"}))[C1.chunk_id]["status"] == "DONE"
    assert not CK.claimable({"status": "DONE"}) and not CK.claimable({"status": "FAILED_PERMANENT"})


def test_failed_attempts_become_permanent_but_quota_is_not_penalized():
    t = NOW - timedelta(days=1)
    rows = [_chunk_row(C1, "FAILED", t + timedelta(minutes=i), run_id=f"bf-{i}", attempts=i + 1, err="RUNTIME_ERROR")
            for i in range(CK.MAX_ATTEMPTS)]
    assert CK.fold(rows)[C1.chunk_id]["status"] == "FAILED_PERMANENT"
    q = [_chunk_row(C1, "FAILED", t + timedelta(minutes=i), run_id=f"bf-{i}", attempts=i + 1, err=CK.QUOTA_ERROR_CLASS)
         for i in range(CK.MAX_ATTEMPTS * 2)]
    st = CK.fold(q)[C1.chunk_id]
    assert st["status"] == "FAILED" and st["penalized"] == 0 and CK.claimable(st)
    mixed = q + [_chunk_row(C1, "FAILED", t + timedelta(hours=1), run_id="bf-x", err="RUNTIME_ERROR")]
    assert CK.fold(mixed)[C1.chunk_id]["status"] == "FAILED"


def test_plan_is_contiguous_deterministic_and_hash_stable():
    p = CK.plan("ads_sku_daily", date(2025, 1, 1), date(2025, 12, 31))
    assert p[0].start == date(2025, 1, 1) and p[-1].end == date(2025, 12, 31)
    assert all((b.start - a.end).days == 1 for a, b in zip(p, p[1:]))
    assert all((c.end - c.start).days + 1 <= 62 for c in p)
    assert CK.plan_hash(p) == CK.plan_hash(list(reversed(p)))
    with pytest.raises(ValueError):
        CK.plan("catalog", date(2025, 1, 1), date(2025, 1, 2))


def _backfilling_store(chunks, extra_ledger=(), caps=(), events=None, approved=None):
    ph = CK.plan_hash(chunks)
    t0 = NOW - timedelta(days=1)
    ledger = [_chunk_row(c, "PENDING", t0, run_id="ctl-plan", plan_hash=ph) for c in chunks] + list(extra_ledger)
    evs = events or chain(L.CREDENTIALS_PENDING, L.VALIDATING, L.CAPABILITY_DISCOVERY, L.READY_FOR_BACKFILL,
                          L.BACKFILLING)
    binds = [binding_row(), binding_row(I.PERFORMANCE, PERF_FP, "obs-p", bid="bnd-p")]
    return FakeStore({("tenant_ops", "BACKFILL_CHECKPOINTS"): ledger,
                      ("tenant_ops", "CAPABILITY_PROFILE"): list(caps),
                      ("tenant_ops", "SELLER_IDENTITY_OBSERVATIONS"): [obs_row(), obs_row(I.PERFORMANCE, PERF_FP, "obs-p")]},
                     events=evs, ref_markers=owner_markers(evs, approved or ph, bindings=binds))


def _claimed(store):
    return [r for t, rows in store.appended if t == "BACKFILL_CHECKPOINTS" for r in rows if r["status"] == "RUNNING"]


def test_claim_next_takes_one_chunk_with_lease(monkeypatch):
    fake_api(monkeypatch)
    chunks = CK.plan("fbo_postings", date(2026, 1, 1), date(2026, 1, 21))
    store = _backfilling_store(chunks)
    assert LC.cmd_claim_next(make_ctx(store, entities=("fbo_postings",))) == 0
    got = _claimed(store)
    assert len(got) == 1 and got[0]["backfill_id"] == chunks[0].chunk_id and got[0]["lease_generation"] == 1
    assert list(store.lease_tables) == [CK.lease_name(chunks[0].chunk_id, 1)]
    # второй исполнитель не берёт тот же отрезок, пока аренда жива
    LC.cmd_claim_next(make_ctx(store, entities=("fbo_postings",)))
    assert [r["backfill_id"] for r in _claimed(store)] == [chunks[0].chunk_id, chunks[1].chunk_id]


def test_claim_next_refuses_outside_backfilling_or_unapproved_plan(monkeypatch):
    fake_api(monkeypatch)
    chunks = CK.plan("fbo_postings", date(2026, 1, 1), date(2026, 1, 7))
    early = _backfilling_store(chunks, events=chain(L.CREDENTIALS_PENDING, L.VALIDATING, L.CAPABILITY_DISCOVERY))
    with pytest.raises(SystemExit) as e:
        LC.cmd_claim_next(make_ctx(early, entities=("fbo_postings",)))
    assert e.value.code == 4 and not _claimed(early)
    other = _backfilling_store(chunks, approved="0" * 64)
    assert LC.cmd_claim_next(make_ctx(other, entities=("fbo_postings",))) == 4 and not _claimed(other)


def test_claim_next_refuses_without_binding(monkeypatch):
    fake_api(monkeypatch, client_id="5550200")                        # кабинет подменён
    chunks = CK.plan("fbo_postings", date(2026, 1, 1), date(2026, 1, 7))
    store = _backfilling_store(chunks)
    with pytest.raises(SystemExit) as e:
        LC.cmd_claim_next(make_ctx(store, entities=("fbo_postings",)))
    assert e.value.code == 3 and not _claimed(store)


def _caps(cpc, running):
    return [{"profile_id": "p1", "api": "performance", "capability": "async_report", "status": "AVAILABLE",
             "discovered_at": (NOW - timedelta(days=2)).isoformat(), "evidence_json": json.dumps({"cpc_campaigns": cpc})},
            {"profile_id": "p2", "api": "performance", "capability": "active_campaigns", "status": "AVAILABLE",
             "discovered_at": (NOW - timedelta(days=2)).isoformat(), "evidence_json": json.dumps({"running": running})}]


def test_quota_exhaustion_defers_and_resumes_next_window(monkeypatch, capsys):
    fake_api(monkeypatch)
    chunks = CK.plan("ads_sku_daily", date(2025, 1, 1), date(2025, 4, 30))
    # 1 активная кампания: оценка 240, полезно 240·0.5 − 50 = 70 выгрузок/сутки; 30 CPC-кампаний на отрезок.
    spent = [dict(_chunk_row(chunks[0], "RUNNING", NOW - timedelta(hours=3), run_id="bf-a"),
                  started_at=(NOW - timedelta(hours=3)).isoformat(), evidence_json=json.dumps({"exports": 60}))]
    store = _backfilling_store(chunks, extra_ledger=spent, caps=_caps(30, 1))
    assert LC.cmd_claim_next(make_ctx(store, entities=("ads_sku_daily",))) == 0
    assert not _claimed(store)
    assert '"quota_deferred"' in capsys.readouterr().out
    later = make_ctx(store, entities=("ads_sku_daily",), now=NOW + timedelta(hours=22))
    assert LC.cmd_claim_next(later) == 0
    got = _claimed(store)
    assert len(got) == 1 and json.loads(got[0]["evidence_json"])["exports"] == 30


def test_quota_unknown_export_count_never_claims(monkeypatch):
    fake_api(monkeypatch)
    chunks = CK.plan("ads_sku_daily", date(2025, 1, 1), date(2025, 2, 28))
    store = _backfilling_store(chunks)
    LC.cmd_claim_next(make_ctx(store, entities=("ads_sku_daily",)))
    assert not _claimed(store)


def test_quota_model_is_conservative_and_never_invents_quota():
    assert Q.estimated_daily_limit(None) == Q.CONSERVATIVE_FLOOR == Q.estimated_daily_limit(0)
    assert Q.estimated_daily_limit(100) == Q.ACCOUNT_CAP
    b = Q.budget(None, [], NOW)
    assert b["basis"] == "conservative_floor" and 0 < b["usable"] < Q.CONSERVATIVE_FLOOR
    assert Q.budget(1, [(NOW - timedelta(hours=25), 1000)], NOW)["used_24h"] == 0
    assert not Q.can_run(b["remaining"] + 1, b) and Q.can_run(0, b)


def test_verify_chunks_marks_done_from_exact_run_and_never_rewrites_done(monkeypatch):
    chunks = [C1]
    rid = "bf-" + C1.chunk_id + "-0001-x"
    running = dict(_chunk_row(C1, "RUNNING", NOW - timedelta(hours=1), run_id=rid),
                   lease_until=(NOW + timedelta(hours=1)).isoformat())
    store = _backfilling_store(chunks, extra_ledger=[running])
    store.t[("ozon_raw", "OZON_INGESTION_RUNS")] = [
        {"ingestion_run_id": rid, "entity": "fbo_postings", "status": "OK", "source_from": "2025-12-01",
         "source_to": "2026-01-07", "rows_received": 5},                    # чужое окно — не засчитывается
    ]
    LC.cmd_verify_chunks(make_ctx(store, entities=("fbo_postings",)))
    st = CK.fold(store.t[("tenant_ops", "BACKFILL_CHECKPOINTS")])[C1.chunk_id]
    assert st["status"] == "FAILED" and st["last_error_class"] == "WINDOW_MISMATCH"
    store = _backfilling_store(chunks, extra_ledger=[running])
    store.t[("ozon_raw", "OZON_INGESTION_RUNS")] = [
        {"ingestion_run_id": rid, "entity": "fbo_postings", "status": "OK", "source_from": "2026-01-01",
         "source_to": "2026-01-07", "rows_received": 5}]
    LC.cmd_verify_chunks(make_ctx(store, entities=("fbo_postings",)))
    assert CK.fold(store.t[("tenant_ops", "BACKFILL_CHECKPOINTS")])[C1.chunk_id]["status"] == "DONE"
    n = len(store.appended)
    LC.cmd_verify_chunks(make_ctx(store, entities=("fbo_postings",), now=NOW + timedelta(minutes=1)))
    assert [r for t, rows in store.appended[n:] for r in rows] == []         # повтор DONE — ничего


def test_verify_chunks_classifies_truncation_and_cursor_replay(monkeypatch):
    for msg, cls in (("StrictLimitError: /v3/product/list: страница упёрлась в лимит", "TRUNCATION"),
                     ("PaginationError: курсор повторился", "TRUNCATION"),
                     ("HTTP 429 Too Many Requests", CK.QUOTA_ERROR_CLASS),
                     ("RuntimeError('boom')", "RUNTIME_ERROR")):
        assert LC.classify_error(msg) == cls, msg


def test_expired_lease_without_run_fails_chunk_for_retry(monkeypatch):
    running = dict(_chunk_row(C1, "RUNNING", NOW - timedelta(hours=6), run_id="bf-dead"),
                   lease_until=(NOW - CK.VISIBILITY_GRACE - timedelta(minutes=1)).isoformat())
    store = _backfilling_store([C1], extra_ledger=[running])
    store.t[("ozon_raw", "OZON_INGESTION_RUNS")] = []
    LC.cmd_verify_chunks(make_ctx(store, entities=("fbo_postings",)))
    st = CK.fold(store.t[("tenant_ops", "BACKFILL_CHECKPOINTS")])[C1.chunk_id]
    assert st["status"] == "FAILED" and st["last_error_class"] == "LEASE_EXPIRED" and CK.claimable(st)


def test_commands_are_bound_to_states(monkeypatch):
    fake_api(monkeypatch)
    store = _backfilling_store([C1])                                       # состояние BACKFILLING
    for cmd in (LC.cmd_plan, LC.cmd_discover, LC.cmd_history, LC.cmd_dq):
        with pytest.raises(SystemExit) as e:
            cmd(make_ctx(store, entities=("fbo_postings",)))
        assert e.value.code == 4, cmd.__name__


# ═════════════════════════════════════════ 12–13. граница истории: предел хранения ≠ пустота
def _fbo_probe_factory(first: date, retention_from: date | None = None):
    def probe(s, e):
        if retention_from and s < retention_from:
            return H.ProbeResult(H.REJECTED, None, 400)
        if e < first:
            return H.ProbeResult(H.EMPTY, None, 200)
        return H.ProbeResult(H.DATA, max(first, s), 200)
    return probe


def test_history_finds_exact_first_fbo_activity_within_budget():
    b = H.windowed_first_activity("fbo_postings", _fbo_probe_factory(date(2024, 3, 17)), date(2026, 9, 28),
                                  exact_first=True)
    assert b.first_observed_activity == date(2024, 3, 17) and b.confidence == "VERIFIED"
    assert b.calls <= 10 and b.completeness_status == "COMPLETE"


def test_retention_rejection_is_not_absence_of_activity():
    """API отказывает окна раньше 2025-01-01: данные есть с первого принятого окна — это предел, а не старт."""
    probe = _fbo_probe_factory(date(2023, 1, 1), retention_from=date(2025, 1, 1))
    b = H.windowed_first_activity("fbo_postings", probe, date(2026, 9, 28), exact_first=True)
    assert b.limitation_reason == "API_RETENTION"
    assert b.completeness_status == "PARTIAL"


def test_empty_after_retention_is_not_verified_empty():
    def probe(s, e):
        return H.ProbeResult(H.REJECTED if s < date(2026, 1, 1) else H.EMPTY, None, 400 if s < date(2026, 1, 1) else 200)
    b = H.windowed_first_activity("fbo_postings", probe, date(2026, 9, 28), exact_first=True)
    assert b.confidence == "EMPTY_AFTER_RETENTION" and b.first_observed_activity is None


def test_history_error_aborts_fail_closed():
    with pytest.raises(H.HistoryProbeError):
        H.windowed_first_activity("fbo_postings", lambda s, e: H.ProbeResult(H.ERROR, None, 500), date(2026, 9, 28))


def test_seed_is_only_a_starting_point():
    """Подсказка «примерно с 2025» не становится границей: данные раньше seed находятся."""
    b = H.windowed_first_activity("fbo_postings", _fbo_probe_factory(date(2023, 6, 1)), date(2026, 9, 28),
                                  seed=date(2025, 1, 1), exact_first=True)
    assert b.first_observed_activity == date(2023, 6, 1)
    b2 = H.windowed_first_activity("fbo_postings", _fbo_probe_factory(date(2025, 4, 2)), date(2026, 9, 28),
                                   seed=date(2025, 1, 1), exact_first=True)
    assert b2.first_observed_activity == date(2025, 4, 2)


def test_binary_search_for_ads_first_day():
    first = date(2024, 5, 19)

    def probe(s, e):
        return H.ProbeResult(H.DATA if e >= first else H.EMPTY, None, 200)
    b = H.windowed_first_activity("ads_expense_daily", probe, date(2026, 9, 28), max_calls=160)
    assert b.first_observed_activity == first and b.calls <= 110


def test_finance_documented_floor_is_partial_and_sampled():
    b = H.sampled_first_activity("finance_accrual", lambda d: H.ProbeResult(H.DATA, d, 200), date(2026, 9, 28))
    assert b.first_observed_activity == date(2022, 1, 1)
    assert b.completeness_status == "PARTIAL" and b.limitation_reason == "API_DOCUMENTED_FLOOR"
    first = date(2024, 2, 11)
    b = H.sampled_first_activity("finance_accrual", lambda d: H.ProbeResult(H.DATA if d >= first else H.EMPTY, None, 200),
                                 date(2026, 9, 28))
    assert b.first_observed_activity == first and b.confidence == "SAMPLED" and b.completeness_status == "COMPLETE"


# ═════════════════════════════════════════ хранилище: только Tables API
def test_control_store_uses_no_query_jobs():
    tree = ast.parse((RUNTIME / "control_store.py").read_text(encoding="utf-8"))
    used = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and isinstance(n.func.value, ast.Attribute) and n.func.value.attr == "client"}
    assert used == {"list_rows", "insert_rows_json", "list_tables", "create_table", "get_table"}


def test_control_code_never_queries():
    for name in ("lifecycle.py", "control_store.py", "lifecycle_core.py", "checkpoints.py", "dq.py",
                 "history.py", "quota.py", "identity.py", "credentials.py"):
        tree = ast.parse((RUNTIME / name).read_text(encoding="utf-8"))
        for n in ast.walk(tree):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute):
                assert n.func.attr not in ("query", "load_table_from_file", "load_table_from_json", "delete_table",
                                           "update_table", "delete_dataset"), (name, ast.unparse(n))


def test_control_store_rejects_writes_outside_journals_and_redacts():
    class Client:
        def __init__(self):
            self.calls = []

        def insert_rows_json(self, ref, rows, row_ids=None):
            self.calls.append((ref, rows, row_ids))
            return []
    cl = Client()
    st = ControlStore(cl, "p", {"tenant_ops": "ops", "tenant_locks": "locks", "ref": "ref", "ozon_raw": "raw"},
                      redact=lambda r: {k: ("<r>" if v == "S" else v) for k, v in r.items()})
    for t in ("SELLER_BINDING", "RAW_OZON_CATALOG", "V_TENANT_STATE_AUDIT"):
        with pytest.raises(Exception):
            st.append(t, [{"a": 1}])
    st.append("DQ_RESULTS", [{"a": "S"}])
    ref, rows, ids = cl.calls[0]
    assert ref == "p.ops.DQ_RESULTS" and rows == [{"a": "<r>"}] and len(ids[0]) == 32
    with pytest.raises(Exception):
        list(st.rows("analytics_share", "x"))


# ═════════════════════════════════════════ мутационная проверка критичных валидаторов
VALIDATOR_MUTATIONS = {
    # имя: (модуль, атрибут, замена, тест, который обязан упасть)
    "ready_contract ← always ok": (L, "ready_contract", lambda s: [], "test_ready_is_rejected_when_any_condition_fails"),
    "binding validator ← always ok": (L, "_v_binding", lambda s, p: [], "test_ready_is_rejected_when_any_condition_fails"),
    "audit_history ← always ok": (L, "audit_history", lambda e, d=None: [],
                                  "test_corrupted_journal_blocks_every_transition_except_suspend"),
    "decision proof ← none": (L, "decision_problem", lambda e, d: None, "test_forged_operator_edge_is_rejected_by_audit"),
    "live_status ← always BOUND": (I, "live_status", lambda b, fp: ("BOUND", "x"), "test_runtime_refuses_fingerprint_mismatch"),
    "effective_binding ignores revoke": (I, "binding_from_markers",
                                         lambda items, api, now, observations=None: I.Binding(api, "CONFIRMED", SELLER_FP, "b", "o", now, "x"),
                                         "test_runtime_refuses_revoked_binding"),
    "observation_status ← last any": (I, "observation_status", lambda o, api, now, max_age_hours=24: (o[-1] if o else None, "ok"),
                                      "test_observation_status_rejects_stale_multiple_empty_and_non_observed"),
    "fold ← last wins": (CK, "fold", lambda rows, reopened_run_ids=frozenset(): {r["backfill_id"]: {"status": r["status"], "attempts": 1, "penalized": 0,
                                                                      "done_at": None, "last_error_class": None,
                                                                      "run_id": r.get("run_id")} for r in rows},
                         "test_done_is_immutable_until_operator_reopens"),
    "lease ← always free": (CK, "next_lease_generation", lambda leases, cid, now, ledger_generations=(): 1,
                            "test_live_lease_blocks_and_expired_lease_is_taken_over"),
    "quota ← always can run": (Q, "can_run", lambda n, b: True, "test_quota_exhaustion_defers_and_resumes_next_window"),
    "seller roles ← always pass": (CR, "evaluate_seller_roles",
                                   lambda r, e, now, policy=None: {"status": "PASS", "blocking": [], "warnings": [],
                                                                   "mutation_methods": [], "unknown_methods": [],
                                                                   "role_names": [], "method_count": 0, "expires_at": None},
                                   "test_mutation_capable_key_is_rejected_and_not_modified"),
    "dq blocking ← none": (DQ, "blocking_failures", lambda c: [], "test_dq_blocking_failures_block_ready_end_to_end"),
    "covered_days ← utc": (DQ, "covered_days", lambda d, w: set().union(*(DQ.days(s, e) for s, e in w)) if w else set(),
                           "test_dq_fbo_coverage_needs_both_utc_days_for_msk_day"),
}


@pytest.mark.parametrize("name", sorted(VALIDATOR_MUTATIONS))
def test_mutation_is_killed(name):
    """Каждая мутация критичного валидатора ломает хотя бы один тест этого файла (проверка по делу)."""
    module, attr, repl, target = VALIDATOR_MUTATIONS[name]

    def child(mutant):
        env = {k: v for k, v in os.environ.items() if k != "T5_MUTANT"}
        if mutant:
            env["T5_MUTANT"] = mutant
        return subprocess.run([sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider",
                               f"{__file__}::{target}"], capture_output=True, text=True, timeout=180,
                              cwd=str(RUNTIME.parent), env=env)
    base = child(None)
    assert base.returncode == 0, f"{target} падает и без мутации — проверка мутации недостоверна"
    r = child(name)
    assert r.returncode == 1 and "failed" in r.stdout, f"мутация «{name}» выжила:\n{r.stdout[-800:]}"


@pytest.fixture(autouse=True)
def _apply_mutant(monkeypatch):
    name = os.environ.get("T5_MUTANT")
    if name:
        module, attr, repl, _t = VALIDATOR_MUTATIONS[name]
        monkeypatch.setattr(module, attr, repl)
        if module is L and attr == "ready_contract":           # EDGES держит ссылку на функцию
            monkeypatch.setitem(L.EDGES, (L.RECONCILING, L.READY), ({L.CONTROL}, repl))
    yield


def test_control_entrypoint_fails_closed_without_project():
    """Как шаг control-fail-closed-without-project сборки v2: без GCP_PROJECT_ID — отказ до сети."""
    r = subprocess.run([sys.executable, "-c", _STUBS + f"sys.path.insert(0, {str(RUNTIME)!r})\n"
                        "sys.argv = ['lifecycle.py', 'status']\nimport runpy\n"
                        f"runpy.run_path({str(RUNTIME / 'lifecycle.py')!r}, run_name='__main__')\n"],
                       capture_output=True, text=True, timeout=60, env={"PATH": os.environ["PATH"]})
    assert r.returncode != 0 and "GCP_PROJECT_ID не задан" in r.stderr


# ═════════════════════════════════════════ ревью PR #226: история (находки 1, 2, 8)
def test_finance_is_searched_to_today_not_to_anchor():
    """Начисления после первого отправления: поиск до якоря давал NOT_APPLICABLE (READY без финансов)."""
    first = date(2024, 3, 13)
    b = H.sampled_first_activity("finance_accrual", lambda d: H.ProbeResult(H.DATA if d >= first else H.EMPTY, None, 200),
                                 date(2026, 9, 28), anchor=date(2024, 3, 10))
    assert b.first_observed_activity == first and b.completeness_status == "COMPLETE"


def test_finance_plan_starts_at_earliest_activity_of_any_domain():
    bounds = {"fbo_postings": {"first_observed_activity": "2024-01-10"},
              "finance_accrual": {"first_observed_activity": "2024-03-13"}}
    assert LC.domain_start(bounds, "finance_accrual") == date(2024, 1, 10)
    plan = LC.build_plan(bounds, date(2024, 3, 31))
    assert min(c.start for c in plan if c.domain == "finance_accrual") == date(2024, 1, 10)


def test_all_rejected_is_an_error_not_empty_history():
    with pytest.raises(H.HistoryProbeError):
        H.windowed_first_activity("fbo_postings", lambda s, e: H.ProbeResult(H.REJECTED, None, 400), date(2026, 9, 28))
    with pytest.raises(H.HistoryProbeError):
        H.sampled_first_activity("finance_accrual", lambda d: H.ProbeResult(H.REJECTED, None, 400), date(2026, 9, 28))


def test_rejected_window_is_split_and_data_inside_it_is_found():
    """Удержание с 2025-01-01, продавец активен каждые 11 суток с 2023: 2025 год не теряется."""
    ret = date(2025, 1, 1)

    def probe(s, e):
        if s < ret:
            return H.ProbeResult(H.REJECTED, None, 400)
        d = s
        while d <= e:
            if (d - date(2023, 1, 1)).days % 11 == 0:
                return H.ProbeResult(H.DATA, d, 200)
            d += timedelta(days=1)
        return H.ProbeResult(H.EMPTY, None, 200)
    b = H.windowed_first_activity("fbo_postings", probe, date(2026, 9, 28), exact_first=True)
    assert b.first_observed_activity is not None and b.first_observed_activity < date(2025, 1, 13)
    assert b.completeness_status == "PARTIAL" and b.limitation_reason == "API_RETENTION"


def test_seed_after_retention_still_marks_partial():
    probe = _fbo_probe_factory(date(2025, 6, 1), retention_from=date(2025, 1, 1))
    b = H.windowed_first_activity("fbo_postings", probe, date(2026, 9, 28), seed=date(2025, 5, 1), exact_first=True)
    assert b.first_observed_activity == date(2025, 6, 1) and b.completeness_status == "PARTIAL"


def test_ignored_sort_order_is_detected_by_confirming_probe():
    """Ozon проигнорировал sort_dir: первой строкой пришла поздняя дата — граница уточняется."""
    first = date(2024, 2, 3)

    def probe(s, e):
        if e < first:
            return H.ProbeResult(H.EMPTY, None, 200)
        return H.ProbeResult(H.DATA, min(e, date(2024, 11, 30)), 200)       # «последняя», а не первая
    b = H.windowed_first_activity("fbo_postings", probe, date(2026, 9, 28), exact_first=True)
    assert b.first_observed_activity == first


def test_empty_history_with_rejections_is_not_ready_material():
    def probe(s, e):
        return H.ProbeResult(H.REJECTED if s < date(2026, 1, 1) else H.EMPTY, None, 200)
    b = H.windowed_first_activity("fbo_postings", probe, date(2026, 9, 28))
    assert b.completeness_status == "PARTIAL"
    h = {d: {"completeness_status": "COMPLETE"} for d in L.HISTORICAL_DOMAINS}
    h["fbo_postings"] = {"completeness_status": b.completeness_status}
    assert L.decide(ready_snapshot(history=h), L.READY, "CONTROL:r", "A", {}, "r")[0] == "REJECT"


# ═════════════════════════════════════════ ревью PR #226: чекпойнты, квота, свежесть, гонки
def test_verify_uses_running_row_of_current_attempt_not_last_in_order():
    """Находка 5: старая RUNNING пришла последней в выдаче — отрезок всё равно становится DONE."""
    old = dict(_chunk_row(C1, "RUNNING", NOW - timedelta(hours=9), run_id="bf-old"),
               lease_until=(NOW - timedelta(hours=7)).isoformat())
    failed = _chunk_row(C1, "FAILED", NOW - timedelta(hours=4), run_id="bf-old", err="LEASE_EXPIRED")
    new = dict(_chunk_row(C1, "RUNNING", NOW - timedelta(hours=1), run_id="bf-new"),
               lease_until=(NOW + timedelta(hours=1)).isoformat())
    store = _backfilling_store([C1], extra_ledger=[new, failed, old])       # старая RUNNING — последней
    store.t[("ozon_raw", "OZON_INGESTION_RUNS")] = [
        {"ingestion_run_id": "bf-new", "entity": "fbo_postings", "status": "OK", "source_from": "2026-01-01",
         "source_to": "2026-01-07", "rows_received": 3}]
    LC.cmd_verify_chunks(make_ctx(store, entities=("fbo_postings",)))
    assert CK.fold(store.t[("tenant_ops", "BACKFILL_CHECKPOINTS")])[C1.chunk_id]["status"] == "DONE"


def test_run_not_yet_visible_is_not_failed_within_grace():
    running = dict(_chunk_row(C1, "RUNNING", NOW - timedelta(hours=3), run_id="bf-slow"),
                   lease_until=(NOW - timedelta(hours=1)).isoformat())
    store = _backfilling_store([C1], extra_ledger=[running])
    store.t[("ozon_raw", "OZON_INGESTION_RUNS")] = []                       # строка прогона ещё не видна
    LC.cmd_verify_chunks(make_ctx(store, entities=("fbo_postings",)))
    assert CK.fold(store.t[("tenant_ops", "BACKFILL_CHECKPOINTS")])[C1.chunk_id]["status"] == "RUNNING"


def test_backfill_run_ids_give_distinct_staging_tables():
    a, b = LC.run_id_for(C1, 1), LC.run_id_for(C1, 2)
    staging = lambda rid: rid.replace("-", "")[:10]                        # common.merge_rows
    assert staging(a) != staging(b) and a.startswith("bf-") and a.endswith("-0001")


def test_performance_slot_serializes_export_chunks(monkeypatch):
    """Находка 10: одна выгрузка на аккаунт — второй отрезок ads_sku_daily ждёт слот."""
    fake_api(monkeypatch)
    chunks = CK.plan("ads_sku_daily", date(2025, 1, 1), date(2025, 4, 30))
    store = _backfilling_store(chunks, caps=_caps(2, 5))
    assert LC.cmd_claim_next(make_ctx(store, entities=("ads_sku_daily",))) == 0
    assert LC.cmd_claim_next(make_ctx(store, entities=("ads_sku_daily",))) == 0
    assert len(_claimed(store)) == 1
    assert any(n.startswith(f"L_{LC.PERF_SLOT}_") for n in store.lease_tables)
    later = NOW + CK.LEASE_TTL + timedelta(minutes=1)
    LC.cmd_claim_next(make_ctx(store, entities=("ads_sku_daily",), now=later))
    assert len(_claimed(store)) == 2


def test_only_the_approved_plan_version_is_worked():
    """Находка 12: перестроенный план (другая сетка) в работу не идёт, пока его не подтвердили."""
    fake_chunks = CK.plan("fbo_postings", date(2026, 1, 1), date(2026, 1, 14))
    other = CK.plan("fbo_postings", date(2025, 12, 30), date(2026, 1, 14))
    ph_other = CK.plan_hash(other)
    extra = [_chunk_row(c, "PENDING", NOW - timedelta(hours=1), run_id="ctl-plan2", plan_hash=ph_other) for c in other]
    store = _backfilling_store(fake_chunks, extra_ledger=extra)
    vs = CK.plan_versions(store.t[("tenant_ops", "BACKFILL_CHECKPOINTS")])
    assert set(vs) == {CK.plan_hash(fake_chunks), ph_other}
    assert CK.latest_plan(store.t[("tenant_ops", "BACKFILL_CHECKPOINTS")])[0] == ph_other
    s = LC.active_plan(store.t[("tenant_ops", "BACKFILL_CHECKPOINTS")], store.state_chain(),
                       LC.owner_decisions(make_ctx(store), store.state_chain()))
    assert s[0] == CK.plan_hash(fake_chunks) and s[1] == fake_chunks


def test_partially_visible_plan_is_not_usable():
    chunks = CK.plan("fbo_postings", date(2026, 1, 1), date(2026, 1, 21))
    rows = [_chunk_row(c, "PENDING", NOW, plan_hash=CK.plan_hash(chunks)) for c in chunks[:-1]]
    assert CK.plan_versions(rows) == {}


def _advance_store(state_chain, extra=None, caps=None):
    store = _backfilling_store([C1], events=state_chain)
    store.t[("tenant_ops", "CAPABILITY_PROFILE")] = caps or []
    store.t.update(extra or {})
    return store


def test_advance_checks_performance_live_not_stored_verdict(monkeypatch):
    """Находка 9: вердикт Performance — из этого прогона. Токен не выдан — перехода нет."""
    fake_api(monkeypatch, perf_ok=False)
    evs = chain(L.CREDENTIALS_PENDING, L.VALIDATING)
    store = _advance_store(evs, caps=[_cap("performance", "credential", "AVAILABLE")])
    assert LC.cmd_advance(make_ctx(store)) == 1
    assert L.current_state(store.state_chain()) == L.VALIDATING


def test_advance_writes_through_marker_and_detects_race(monkeypatch):
    fake_api(monkeypatch)
    evs = chain(L.CREDENTIALS_PENDING, L.VALIDATING)
    store = _advance_store(evs)
    store.markers[L.marker_name(3)] = ({"to": "suspended", "from": "validating", "actor": "operator",
                                        "decision": "none", "event": "x"}, NOW)        # параллельно заняли номер
    # цепочка уже SUSPENDED (seq 3) — control видит другое состояние и ребро не пишет
    assert LC.cmd_advance(make_ctx(store)) in (1, 4)
    store2 = _advance_store(chain(L.CREDENTIALS_PENDING, L.VALIDATING))
    orig = store2.record_transition
    store2.record_transition = lambda e: False                              # 409 на маркере
    assert LC.cmd_advance(make_ctx(store2)) == 5
    store2.record_transition = orig
    assert LC.cmd_advance(make_ctx(store2)) == 0
    assert L.current_state(store2.state_chain()) == L.CAPABILITY_DISCOVERY


def test_evidence_from_previous_cycle_does_not_count(monkeypatch):
    """После возобновления (новый вход в VALIDATING) старые возможности и границы не годятся."""
    fake_api(monkeypatch)
    evs = chain(L.CREDENTIALS_PENDING, L.VALIDATING, L.CAPABILITY_DISCOVERY, L.SUSPENDED, L.VALIDATING,
                L.CAPABILITY_DISCOVERY, start=NOW - timedelta(hours=2))
    old = NOW - timedelta(days=3)
    caps = [dict(_cap(api, cap, "AVAILABLE"), discovered_at=old.isoformat(), profile_id=f"{api}{cap}")
            for e in ("fbo_postings",) for api, cap in LC.ENTITY_CAPABILITIES[e]]
    store = _advance_store(evs, caps=caps)
    store.t[("tenant_ops", "HISTORY_BOUNDARIES")] = [{"entity": "fbo_postings", "completeness_status": "COMPLETE",
                                                      "first_observed_activity": "2026-01-01",
                                                      "determined_at": old.isoformat()}]
    ctx = make_ctx(store, entities=("fbo_postings",))
    s = LC.snapshot(ctx, {"seller": "BOUND"}, {"seller": {"status": "PASS"}}, store.state_chain(),
                    LC.owner_decisions(ctx, store.state_chain()))
    assert s.capabilities == {} and s.history == {}
    assert L.decide(s, L.READY_FOR_BACKFILL, "CONTROL:c", "A", {}, "r")[0] == "REJECT"


def test_ready_uses_dq_computed_in_the_same_run(monkeypatch):
    """В RECONCILING advance считает DQ сам; старый «зелёный» прогон DQ в журнале не используется."""
    fake_api(monkeypatch)
    evs = chain(L.CREDENTIALS_PENDING, L.VALIDATING, L.CAPABILITY_DISCOVERY, L.READY_FOR_BACKFILL, L.BACKFILLING,
                L.RECONCILING)
    store = _advance_store(evs)
    store.t[("tenant_ops", "DQ_RESULTS")] = [{"run_id": "old", "check_id": c, "status": "PASS", "severity": "BLOCKING",
                                              "evaluated_at": NOW.isoformat()} for c in L.READY_REQUIRED_DQ]
    LC.cmd_advance(make_ctx(store, entities=("fbo_postings",)))
    # Отрезок не DONE и срока созревания нет: не READY, а законный возврат в BACKFILLING.
    assert L.current_state(store.state_chain()) == L.BACKFILLING
    written = [r for t, rows in store.appended if t == "DQ_RESULTS" for r in rows]
    assert written and {r["run_id"] for r in written} == {"ctl-test"}


def test_commands_refuse_on_forged_journal(monkeypatch):
    fake_api(monkeypatch)
    evs = chain(L.CREDENTIALS_PENDING, L.VALIDATING, L.CAPABILITY_DISCOVERY, L.READY_FOR_BACKFILL, L.BACKFILLING)
    store = _backfilling_store([C1], events=evs)
    del store.refm[L.decision_marker_name(evs[-1]["seq"])]                         # знака APPROVE нет
    with pytest.raises(SystemExit) as e:
        LC.cmd_claim_next(make_ctx(store, entities=("fbo_postings",)))
    assert e.value.code == 4 and not _claimed(store)


def test_validate_log_carries_no_fingerprint(monkeypatch, capsys):
    """Находка 13: даже префикс отпечатка в журнал не пишется (Client-Id восстановим перебором)."""
    fake_api(monkeypatch)
    LC.cmd_validate(make_ctx(FakeStore()))
    out = capsys.readouterr().out
    assert SELLER_FP[:12] not in out and PERF_FP[:12] not in out and "fingerprint" not in out


def test_ads_history_with_long_retention_gap_fits_the_probe_budget():
    """2-й проход, находка 6: удержание рекламы с 2024-09 — граница находится в бюджете 160 проб."""
    ret, first = date(2024, 9, 1), date(2025, 2, 11)

    def probe(s, e):
        if s < ret:
            return H.ProbeResult(H.REJECTED, None, 400)
        return H.ProbeResult(H.DATA if e >= first else H.EMPTY, None, 200)
    b = H.windowed_first_activity("ads_expense_daily", probe, date(2026, 9, 28), max_calls=160)
    assert b.first_observed_activity == first and b.completeness_status == "PARTIAL" and b.calls < 60



# ═════════════════════════════════════════ ревью PR #226, 2-й проход
def test_owner_decision_cannot_be_replayed_for_another_seq():
    """Находка 1: control создаёт маркер «OPERATOR resume» с номером N и старым решением владельца —
    знак ref.OPD_<N> отсутствует (решение привязано к своему номеру), журнал недействителен."""
    evs = chain(L.CREDENTIALS_PENDING, L.VALIDATING, L.SUSPENDED, L.VALIDATING)       # seq 4 — resume владельца
    ok = decisions_for(evs)
    assert L.audit_history(evs, ok) == []
    forged = evs + [dict(ev(L.VALIDATING, L.SUSPENDED, "CONTROL:c", NOW), seq=5, decision_id=None),
                    dict(ev(L.SUSPENDED, L.VALIDATING, "OPERATOR:replay", NOW), seq=6, decision_id=evs[3]["decision_id"])]
    assert any("OPD_000006" in p for p in L.audit_history(forged, ok))
    s = ready_snapshot(events=forged, decisions=ok)
    assert L.decide(s, L.CAPABILITY_DISCOVERY, "CONTROL:c", "A", {}, "r")[0] == "REJECT"


def test_control_store_can_create_tables_only_in_tenant_locks():
    """Знаки владельца (OPD/OPH/OPB) — в ref; control создаёт таблицы только в tenant_locks."""
    src = (RUNTIME / "control_store.py").read_text(encoding="utf-8")
    fn = ast.unparse(next(n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.FunctionDef) and n.name == "_create"))
    assert "self.ds['tenant_locks']" in fn and "'ref'" not in fn


def test_out_of_format_marker_poisons_chain_but_not_owner_hold(monkeypatch):
    """Находка 2: маркер S_1000000 (или любой S_* вне формата) — нарушение; стоп-кран владельца
    действует независимо от журнала (control и runtime его видят)."""
    evs = chain(L.CREDENTIALS_PENDING, L.VALIDATING)
    bad = evs + [{"invalid_marker": "S_1000000", "occurred_at": NOW}]
    assert any("вне формата" in p for p in L.audit_history(bad, decisions_for(evs)))
    assert L.chain_from_markers([("S_1000000", {"to": "ready"}, NOW)])[0]["invalid_marker"] == "S_1000000"
    big = evs + [dict(ev(L.VALIDATING, L.CAPABILITY_DISCOVERY, "CONTROL:c", NOW), seq=L.MAX_SEQ)]
    s = ready_snapshot(events=big)
    assert L.decide(s, L.SUSPENDED, "CONTROL:c", "X", {}, "r")[0] == "REJECT"       # номер вне формата
    store = _backfilling_store([C1])
    store.refm["OPH_0001"] = ({"action": "suspend"}, "{}")
    fake_api(monkeypatch)
    with pytest.raises(SystemExit) as e:
        LC.cmd_claim_next(make_ctx(store, entities=("fbo_postings",)))
    assert e.value.code == 4


def test_advance_follows_owner_hold_into_suspended(monkeypatch):
    fake_api(monkeypatch)
    store = _backfilling_store([C1])
    store.refm["OPH_0001"] = ({"action": "suspend"}, "{}")
    assert LC.cmd_advance(make_ctx(store, entities=("fbo_postings",))) == 0
    assert L.current_state(store.state_chain()) == L.SUSPENDED
    s = ready_snapshot(hold=True)
    assert L.decide(s, L.READY, "CONTROL:c", "A", {}, "r")[0] == "REJECT"


def test_cycle_two_cannot_reuse_previous_cycle_plan():
    """Находка 4: после ротации ключа (READY → VALIDATING) план и одобрение прошлого цикла не годятся."""
    evs = chain(L.CREDENTIALS_PENDING, L.VALIDATING, L.CAPABILITY_DISCOVERY, L.READY_FOR_BACKFILL, L.BACKFILLING,
                L.RECONCILING, L.READY, L.VALIDATING, L.CAPABILITY_DISCOVERY, start=NOW - timedelta(hours=1))
    store = _backfilling_store([C1], events=evs)             # план (сутки назад) и APPROVE — из цикла 1
    ledger = store.t[("tenant_ops", "BACKFILL_CHECKPOINTS")]
    decisions = LC.owner_decisions(make_ctx(store), store.state_chain())
    assert LC.approved_plan_hash(store.state_chain(), decisions) is None
    ph, chunks = LC.active_plan(ledger, store.state_chain(), decisions)
    assert ph is None and chunks == []


def test_owner_can_accept_a_partial_history_limitation():
    """Находка 7: PARTIAL не пропускает READY, пока владелец не принял именно эту границу."""
    h = {d: {"completeness_status": "COMPLETE"} for d in L.HISTORICAL_DOMAINS}
    h["finance_accrual"] = {"completeness_status": "PARTIAL", "boundary_id": "b-fin-1"}
    assert L.decide(ready_snapshot(history=h), L.READY, "CONTROL:r", "A", {}, "r")[0] == "REJECT"
    assert L.decide(ready_snapshot(history=h, accepted_limitations={("finance_accrual", "b-other")}),
                    L.READY, "CONTROL:r", "A", {}, "r")[0] == "REJECT"
    assert L.decide(ready_snapshot(history=h, accepted_limitations={("finance_accrual", "b-fin-1")}),
                    L.READY, "CONTROL:r", "A", {}, "r")[0] == "WRITE"
    rows = [{"decision_id": "d1", "decision_type": "ACCEPT_LIMITATION", "domain": "finance_accrual", "boundary_id": "b-fin-1"},
            {"decision_id": "d2", "decision_type": "ACCEPT_LIMITATION", "domain": "finance_accrual", "boundary_id": "b-fin-0"}]
    # Порядок чтения строк не гарантирован: принятия — множество, а не «последнее».
    assert LC.accepted_limitations(rows) == LC.accepted_limitations(rows[::-1]) == {
        ("finance_accrual", "b-fin-1"), ("finance_accrual", "b-fin-0")}


def test_control_sees_revocation_from_owner_markers(monkeypatch):
    """Находка 8: control берёт привязку из знаков владельца — отзыв виден сразу."""
    fake_api(monkeypatch)
    store = _backfilling_store([C1])
    rev = dict(binding_row(), binding_id="rev-1", status="REVOKED", revoked_at=(NOW - timedelta(minutes=1)).isoformat())
    store.refm[I.binding_marker_name(I.SELLER, 2)] = I.binding_marker(rev)
    with pytest.raises(SystemExit) as e:
        LC.cmd_claim_next(make_ctx(store, entities=("fbo_postings",)))
    assert e.value.code == 3


def test_owner_transition_conflict_leaves_only_a_seq_bound_orphan():
    """409 на S_<seq>: знак OPD_<seq> остаётся, но он привязан к номеру и чужому событию не подходит."""
    sys.path.insert(0, str(RUNTIME.parents[2]))
    from tools.tenancy import tenant_lifecycle as TL
    t, ph = _owner_tables()
    control_event = dict(ev(L.READY_FOR_BACKFILL, L.SUSPENDED, "CONTROL:c", NOW), seq=5)
    t.markers[L.marker_name(5)] = (L.marker_labels(control_event), NOW)          # control занял номер 5
    # Владелец видит цепочку уже с seq 5 и одобряет из SUSPENDED — ребро недопустимо: REJECT без знаков.
    status, _txt = TL.transition(_owner_contract(), t, NOW, L.BACKFILLING, "OPERATOR:owner", "APPROVE", ph)
    assert status == "REJECT" and L.decision_marker_name(6) not in t.refm
    t2, ph2 = _owner_tables()
    real = t2.create_marker
    t2.create_marker = lambda ds, name, lb, d: False if name.startswith("S_") else real(ds, name, lb, d)
    status, _txt = TL.transition(_owner_contract(), t2, NOW, L.BACKFILLING, "OPERATOR:owner", "APPROVE", ph2)
    assert status == "CONFLICT" and L.decision_marker_name(5) in t2.refm
    orphan = L.decision_from_marker(*t2.refm[L.decision_marker_name(5)])
    evs = chain(L.CREDENTIALS_PENDING, L.VALIDATING, L.CAPABILITY_DISCOVERY, L.READY_FOR_BACKFILL)
    squat = evs + [dict(ev(L.READY_FOR_BACKFILL, L.SUSPENDED, "CONTROL:c", NOW), seq=5),
                   dict(ev(L.SUSPENDED, L.VALIDATING, "OPERATOR:forged", NOW), seq=6)]
    assert any("OPD_000006" in p for p in L.audit_history(squat, {**decisions_for(evs), 5: orphan}))



# ═════════════════════════════════════════ ревью PR #226, 3-й проход
def test_owner_retry_completes_an_orphan_decision_idempotently():
    """M1: знак OPD_5 создан, маркер S_5 — нет (сбой). Повтор с тем же ребром и планом достраивает маркер."""
    sys.path.insert(0, str(RUNTIME.parents[2]))
    from tools.tenancy import tenant_lifecycle as TL
    t, ph = _owner_tables()
    real = t.create_marker
    t.create_marker = lambda ds, name, lb, d: False if name.startswith("S_") else real(ds, name, lb, d)
    assert TL.transition(_owner_contract(), t, NOW, L.BACKFILLING, "OPERATOR:owner", "APPROVE", ph)[0] == "CONFLICT"
    t.create_marker = real
    status, text = TL.transition(_owner_contract(), t, NOW, L.BACKFILLING, "OPERATOR:owner", "APPROVE", ph)
    assert status == "WRITE", text
    assert L.current_state(L.chain_from_markers(t.list_tables("tenant_locks"))) == L.BACKFILLING
    t2, ph2 = _owner_tables()
    t2.create_marker("ref", L.decision_marker_name(5), *L.decision_marker(L.READY_FOR_BACKFILL, L.BACKFILLING,
                                                                            plan_hash="0" * 64))
    assert TL.transition(_owner_contract(), t2, NOW, L.BACKFILLING, "OPERATOR:owner", "APPROVE", ph2)[0] == "CONFLICT"


def test_expiring_state_marker_is_a_violation():
    """M1: маркер S_* со сроком жизни (самоудаляемая история) — нарушение."""
    ch = L.chain_from_markers([("S_000001", {"to": "credentials_pending", "from": "none", "actor": "operator",
                                             "decision": "none", "event": "e"}, NOW, NOW + timedelta(days=1))])
    assert ch[0].get("invalid_marker") == "S_000001"


def test_reopen_is_an_owner_sign_seen_without_lag():
    """M2: ремонт — знак ref.OPR_<n>; control видит его сразу (tables.get), строки не нужны."""
    rows = [_chunk_row(C1, "DONE", NOW - timedelta(hours=1), run_id="bf-a")]
    store = _backfilling_store([C1], extra_ledger=rows)
    assert LC.owner_reopens(make_ctx(store)) == frozenset()
    store.refm["OPR_0001"] = ({"chunk": C1.chunk_id}, json.dumps({"chunk_id": C1.chunk_id, "reopens_run_id": "bf-a"}))
    assert LC.owner_reopens(make_ctx(store)) == frozenset({"bf-a"})
    assert CK.fold(store.t[("tenant_ops", "BACKFILL_CHECKPOINTS")], LC.owner_reopens(make_ctx(store)))[C1.chunk_id]["status"] != "DONE"


def test_long_binding_event_is_not_truncated_and_invalid_head_is_revocable():
    """M3: длинная причина отзыва не обрезает описание знака; испорченную голову можно отозвать."""
    TB = _binding_tool()
    b = binding_row()
    rev = dict(b, binding_id="rev-1", status="REVOKED", revoked_at=NOW.isoformat(), notes="«x»\n" * 500)
    lb, desc = I.binding_marker(rev)
    assert len(desc) > 1000 and json.loads(desc)["notes"] == rev["notes"]
    broken = [("OPB_seller_0001", {"api": "seller"}, '{"api": "seller", "status": "CONF')]           # обрезанный JSON
    assert I.binding_from_markers(broken, I.SELLER, NOW).status == I.INVALID_BINDING
    st, _w, row = TB.decide_revoke(broken, I.SELLER, "OPB_seller_0001", "испорченный знак", NOW, "OPERATOR:o")
    assert st == "WRITE" and row["status"] == "REVOKED" and row["api"] == "seller"
    items = broken + [("OPB_seller_0002",) + I.binding_marker(row)]
    assert I.binding_from_markers(items, I.SELLER, NOW).status == I.UNBOUND


def test_confirm_recomputes_fingerprint_from_shown_identity():
    """M4: control показал «правильное» лицо, а отпечаток — другого кабинета: подтверждения нет."""
    TB = _binding_tool()
    other_fp = I.seller_fingerprint("5550200", INN, OGRN)
    forged = dict(obs_row(fp=other_fp, at=NOW - timedelta(hours=1)))            # лицо наше, отпечаток чужой
    st, why, _row = TB.decide_confirm([forged], opb_items([]), I.SELLER, "obs-1", other_fp, "NONE", NOW, "OPERATOR:o")
    assert st == "REJECT" and "пересчитывается" in why[0]


def test_validate_respects_owner_hold(monkeypatch):
    """L1: под стоп-краном validate не читает ключи и не ходит в Ozon."""
    calls = fake_api(monkeypatch)
    store = FakeStore(ref_markers={"OPH_0001": ({"action": "suspend"}, "{}")})
    assert LC.cmd_validate(make_ctx(store)) == 4 and calls == [] and not store.appended


def test_owner_sign_series_gaps_and_unlabelled_hold_fail_closed(monkeypatch):
    """L2: стоп-кран без метки — остановка; пропуск в серии знаков — отказ и у control, и у runtime."""
    assert L.hold_active([("OPH_0001", {})]) and L.hold_active([("OPH_0001", {"action": "weird"})])
    assert not L.hold_active([("OPH_0001", {"action": "suspend"}), ("OPH_0002", {"action": "resume"})])
    store = FakeStore()
    real = ControlStore(types.SimpleNamespace(get_table=lambda ref: (_ for _ in ()).throw(
        type("NotFound", (Exception,), {"code": 404})(ref)) if not ref.endswith("OPH_0003") else
        types.SimpleNamespace(labels={"action": "suspend"}, description="{}")), "p",
        {"tenant_ops": "o", "tenant_locks": "l", "ref": "ref", "ozon_raw": "w"})
    from control_store import StoreError
    with pytest.raises(StoreError):
        real.ref_series(lambda n: f"OPH_{n:04d}")                                   # 1 и 2 удалены, 3 — suspend
    (denied, _), calls = _gate(monkeypatch, [binding_row()], holds=("suspend", "resume"))
    assert denied is None
    import main as M
    bq = _RefBQ({**owner_markers(bindings=[binding_row()]), "OPB_seller_0003": I.binding_marker(binding_row())})
    monkeypatch.setattr(C, "bq", lambda: bq)
    denied, _r = M.binding_gate(["fbo_postings"], NOW)
    assert denied == "tenant:INVALID_OWNER_SIGNS"


def test_retention_search_records_the_found_limit():
    """L4: api_verified_from — найденный предел хранения, а не первая принятая проба поиска."""
    ret = date(2024, 9, 29)

    def probe(s, e):
        return H.ProbeResult(H.REJECTED if s < ret else H.EMPTY, None, 200)
    b = H.windowed_first_activity("ads_expense_daily", probe, date(2026, 9, 28), max_calls=160)
    assert b.api_verified_from == ret


# ═════════════════════════════════════════ ревью PR #226, 4-й проход (низкие)
def test_orphan_retry_carries_the_orphans_decision_id_and_secret_versions():
    """Н-1/Н-2: повтор несёт id знака-сироты; другие числа версий секретов — CONFLICT."""
    sys.path.insert(0, str(RUNTIME.parents[2]))
    from tools.tenancy import tenant_lifecycle as TL
    t, ph = _owner_tables()
    real = t.create_marker
    t.create_marker = lambda ds, name, lb, d: False if name.startswith("S_") else real(ds, name, lb, d)
    TL.transition(_owner_contract(), t, NOW, L.BACKFILLING, "OPERATOR:owner", "APPROVE", ph)
    t.create_marker = real
    assert TL.transition(_owner_contract(), t, NOW, L.BACKFILLING, "OPERATOR:owner", "APPROVE", ph)[0] == "WRITE"
    opd = L.decision_from_marker(*t.refm[L.decision_marker_name(5)])
    s_labels = t.markers[L.marker_name(5)][0]
    assert s_labels["decision"] == L._label(opd["decision_id"])
    assert t.t[("tenant_ops", "TENANT_STATE_EVENTS")][-1]["decision_id"] == opd["decision_id"]
    evs = chain(L.CREDENTIALS_PENDING)
    t3 = _OwnerTables({("tenant_ops", "BACKFILL_CHECKPOINTS"): []}, evs, owner_markers(evs))
    t3.create_marker("ref", L.decision_marker_name(2), *L.decision_marker(
        L.CREDENTIALS_PENDING, L.VALIDATING, secret_versions={"a": 1, "b": 1, "c": 1, "d": 1}))
    st, _txt = TL.transition(_owner_contract(), t3, NOW, L.VALIDATING, "OPERATOR:owner", "CRED",
                             secret_counts={"a": 3, "b": 1, "c": 1, "d": 2})
    assert st == "CONFLICT"


def test_revoke_of_partial_head_fills_required_mirror_fields():
    """Н-3: голова без части полей — строка отзыва всё равно содержит все REQUIRED-поля зеркала."""
    TB = _binding_tool()
    partial = [("OPB_seller_0001", {"api": "seller"}, json.dumps({"api": "seller", "status": "CONFIRMED",
                                                                  "binding_id": "bnd-x"}))]
    st, _w, row = TB.decide_revoke(partial, I.SELLER, "OPB_seller_0001", "ручная правка", NOW, "OPERATOR:o")
    assert st == "WRITE"
    for f in ("binding_id", "marketplace", "api", "identity_fingerprint", "status", "confirmed_by", "confirmed_at",
              "source_observation_id"):
        assert row.get(f) is not None, f


def test_current_state_uses_highest_seq_and_ignores_invalid_markers():
    """Текущее — наибольший seq (как V_TENANT_STATE_CURRENT); маркер вне формата не роняет current_state."""
    evs = [dict(ev(None, L.BACKFILLING, "CONTROL:c", NOW), seq=10),
           dict(ev(L.BACKFILLING, L.RECONCILING, "CONTROL:c", NOW - timedelta(hours=3)), seq=11)]
    assert L.current_state(evs) == L.RECONCILING
    poisoned = chain(L.CREDENTIALS_PENDING, L.VALIDATING) + [{"invalid_marker": "S_1000000", "occurred_at": NOW}]
    assert L.current_state(poisoned) == L.VALIDATING
    assert L.audit_history(poisoned, decisions_for(poisoned[:2]))              # но журнал недействителен
    legacy = [dict(ev(None, L.VALIDATING, "CONTROL:c", NOW), seq=None), dict(ev(None, L.READY, "CONTROL:c", NOW - timedelta(days=1)), seq=None)]
    assert L.current_state(legacy) == L.VALIDATING                              # без номеров — по времени


def test_window_claim_emits_pinned_resumable_execution_contract(monkeypatch,capsys):
    fake_api(monkeypatch)
    store=_backfilling_store([C1])
    LC.cmd_claim_next(make_ctx(store,entities=("fbo_postings",)))
    event=next(json.loads(x) for x in capsys.readouterr().out.splitlines() if '"chunk_claimed"' in x)
    env=event["executor_env"]
    assert env["BACKFILL_MODE"]=="WINDOW_V1" and env["TENANT_BINDING_REQUIRED"]=="1"
    first=json.loads(_claimed(store)[0]["evidence_json"])["runtime_plan"]
    assert first["generation"]==CK.plan_hash([C1]) and first["origin"]==env["BACKFILL_ORIGIN"]


def window_running_store(complete=False,status=None):
    import backfill_core as B
    env={"BACKFILL_MODE":B.VERSION,"TENANT_BINDING_REQUIRED":"1","STRICT_PAGE_CAPS":"1",
         "BACKFILL_TARGET_PROJECT":C.PROJECT,"SINCE":str(C1.start),"UNTIL":str(C1.end),
         "BACKFILL_GENERATION":CK.plan_hash([C1]),"BACKFILL_ORIGIN":(NOW-timedelta(days=1)).isoformat()}
    p=B.plan(env,C1.domain,C.PROJECT,"ozon_raw","ref",NOW.date(),NOW)
    state=B.initial(p)
    if complete:
        state["progress"]={"pending":[],"completed_to":B.utc_ms(str(C1.end+timedelta(days=1)))}
        state["complete"]=True
    proof={"version":B.VERSION,"plan":p,"state":state}
    running=dict(_chunk_row(C1,"RUNNING",NOW-timedelta(hours=1),run_id="bf-synthetic"),
                 lease_until=(NOW+timedelta(hours=1)).isoformat(),evidence_json=json.dumps({"runtime_plan":p}))
    store=_backfilling_store([C1],extra_ledger=[running])
    store.t[("ozon_raw","OZON_INGESTION_RUNS")]=[{"ingestion_run_id":"bf-synthetic","entity":C1.domain,
        "status":status or ("OK" if complete else "IN_PROGRESS"),"source_from":str(C1.start),"source_to":str(C1.end),
        "evidence_json":json.dumps(proof)}]
    return store


def test_window_progress_is_not_failed_and_does_not_bypass_lease():
    store=window_running_store();LC.cmd_verify_chunks(make_ctx(store,entities=(C1.domain,)))
    assert CK.fold(store.t[("tenant_ops","BACKFILL_CHECKPOINTS")])[C1.chunk_id]["status"]=="RUNNING"
    store.lease_tables[CK.lease_name(C1.chunk_id,1)] = NOW+CK.LEASE_TTL
    assert CK.next_lease_generation(store.leases(),C1.chunk_id,NOW,[1]) is None


@pytest.mark.parametrize("fault",["missing","false-ok","wrong-scope"])
def test_window_done_cannot_be_forged_from_legacy_or_partial_success(fault):
    store=window_running_store(status="OK")
    run=store.t[("ozon_raw","OZON_INGESTION_RUNS")][0]
    if fault=="missing":run["evidence_json"]=None
    elif fault=="wrong-scope":run["source_to"]="2026-01-01"
    LC.cmd_verify_chunks(make_ctx(store,entities=(C1.domain,)))
    assert CK.fold(store.t[("tenant_ops","BACKFILL_CHECKPOINTS")])[C1.chunk_id]["status"]=="FAILED"


def test_valid_window_complete_proof_preserves_moscow_coverage_semantics():
    store=window_running_store(complete=True)
    LC.cmd_verify_chunks(make_ctx(store,entities=(C1.domain,)))
    row=next(r for r in store.t[("tenant_ops","BACKFILL_CHECKPOINTS")] if r["status"]=="DONE")
    assert json.loads(row["evidence_json"])["timezone"]=="Europe/Moscow"
    f={"required_ranges":{C1.domain:(C1.start,C1.end)},"done_windows_msk":{C1.domain:[(C1.start,C1.end)]}}
    assert DQ.evaluate(f)["COVERAGE"]["status"]=="PASS"
    assert DQ.evaluate({"required_ranges":f["required_ranges"],"done_windows":f["done_windows_msk"]})["COVERAGE"]["status"]=="FAIL"



def test_authorized_finance_reopen_creates_new_generation_but_retry_does_not():
    finance=CK.Chunk("finance_accrual",date(2026,1,1),date(2026,1,1))
    approved=CK.plan_hash([finance])
    ledger=[dict(_chunk_row(finance,"DONE",NOW,run_id="bf-original"))]
    assert LC.window_generation(approved,finance.chunk_id,ledger,frozenset())==approved
    refresh=LC.window_generation(approved,finance.chunk_id,ledger,frozenset({"bf-original"}))
    assert refresh!=approved
    retry=ledger+[dict(_chunk_row(finance,"RUNNING",NOW+timedelta(hours=1),run_id="bf-retry"))]
    assert LC.window_generation(approved,finance.chunk_id,retry,frozenset({"bf-original"}))==refresh
    assert LC.window_generation(approved,C1.chunk_id,ledger,frozenset({"bf-original"}))==approved
