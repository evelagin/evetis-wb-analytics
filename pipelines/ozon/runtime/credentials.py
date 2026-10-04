"""Проверка capability inventory Ozon (Tenancy policy v2; supersedes historical D2).

Чистый модуль. Источник истины о возможностях ключа Seller API — фактический набор методов из
POST /v1/roles, а не название роли. Классы методов — seller_method_policy.json (генерирует
tools/tenancy/ozon_method_policy.py из Swagger; хеш спецификации записан в политике).

Strict model preserves historical inventory blockers. BROAD_READ_INVENTORY_V1
separates reported inventory risks from authentication, required reads, identity and
runtime/binding integrity. Broad model is explicit per dedicated tenant registry;
role names never authorize calls. Runtime profiles remain unchanged and deny-default.

Performance API: API инспекции прав нет. Проверка — токен и безвредное чтение; способность к
изменениям не наблюдаема (UNKNOWN), а сам runtime вызывает только пути из белого списка
(common.PERF_ALLOWED_PATHS) — изменяющие методы не вызываются никогда.
"""
from __future__ import annotations

import json
import hashlib
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

POLICY_FILE = Path(__file__).resolve().parent / "seller_method_policy.json"
EXPIRY_WARNING_DAYS = 30
STRICT_MODEL = "STRICT_CAPABILITY_V2"
BROAD_MODEL = "BROAD_READ_INVENTORY_V1"
CREDENTIAL_MODELS = frozenset({STRICT_MODEL, BROAD_MODEL})

# Методы Seller API, без которых сущность не работает (runtime entities.py).
ENTITY_SELLER_METHODS: dict[str, tuple[str, ...]] = {
    "catalog": ("/v3/product/list", "/v3/product/info/list"),
    "prices": ("/v5/product/info/prices",),
    "seller_info": ("/v1/seller/info", "/v1/rating/summary"),
    "stocks": ("/v3/product/list", "/v1/analytics/stocks"),
    "fbo_postings": ("/v3/posting/fbo/list",),
    "finance_accrual": ("/v1/finance/accrual/types", "/v1/finance/accrual/by-day"),
    "clusters": ("/v1/cluster/list",),
    "supplies": ("/v3/supply-order/list", "/v3/supply-order/get", "/v1/supply-order/bundle"),
}
# Методы самой проверки: инспекция ключа и наблюдение identity.
VALIDATION_SELLER_METHODS = ("/v1/roles", "/v1/seller/info")


def load_policy(path: Path = POLICY_FILE) -> dict:
    import seller_policy
    return seller_policy.load(path)


def required_seller_methods(entities) -> set[str]:
    req = set(VALIDATION_SELLER_METHODS)
    for e in entities:
        req.update(ENTITY_SELLER_METHODS.get(e, ()))
    return req


def _parse_ts(v):
    if v is None:
        return None
    if not isinstance(v, str) or not v.strip():
        raise ValueError("invalid expiry")
    parsed = datetime.fromisoformat(v.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("expiry timezone required")
    return parsed.astimezone(timezone.utc)


def runtime_authorization_evidence(policy, profiles):
    """Local release consistency evidence, not a substitute for independent AST/image CI.

    Binds the observation to exact source/profile hashes. The immutable release must
    separately pass the scanner and offline adversarial qualification before deployment.
    Does not obtain credentials or perform network requests.
    """
    import seller_policy as SP
    SP.validate(policy)
    root = POLICY_FILE.parent
    contract = json.loads((root / "runtime_execution_contract.json").read_text())
    good = (type(contract.get("schema_version")) is int and contract["schema_version"] == 1
            and contract.get("external_binding_required") is True
            and contract.get("external_promo_enabled") is False)
    selected = {k: sorted(profiles.get(k, ())) for k in ("runtime", "control")}
    for routes in selected.values():
        good = good and bool(routes)
        for method, path in routes:
            record = policy["methods"].get(path, {})
            good = good and (record.get("http") == method and record.get("lifecycle") == "ACTIVE"
                            and record.get("semantics") == "READ" and record.get("side_effects") == "NONE"
                            and record.get("confidence") == "REVIEWED")
    hashes = {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in
              ("common.py", "main.py", "credentials.py", "lifecycle.py", "identity.py", "runtime_execution_contract.json", "seller_method_policy.json")}
    return {"status": "PASS" if good else "FAIL", "binding_gate_integrity": "PASS" if good else "FAIL",
            "basis": "IMMUTABLE_RELEASE_CONSISTENCY_REQUIRES_INDEPENDENT_CI_QUALIFICATION",
            "source_hashes": hashes, "selected_profiles": selected,
            "profiles_sha256": hashlib.sha256(json.dumps(selected, sort_keys=True).encode()).hexdigest(),
            "external_promo_enabled": contract.get("external_promo_enabled")}


def capability_discovery(policy, matrix, profiles, entities, now):
    """Durable minimal non-secret inventory, no automatic runtime expansion.

    USED means implemented dependency of selected entities/control, not live traffic.
    External callable profiles exclude promo, even when a shared legacy profile exists.
    """
    used = required_seller_methods(entities)
    out = []
    for path in sorted({p for methods in matrix.values() for p in methods}):
        record = policy["methods"].get(path, {})
        life, sem = record.get("lifecycle", "UNRESOLVED"), record.get("semantics", "UNPROVEN")
        reviewed = record.get("confidence") == "REVIEWED"
        supported = sorted(k for k, routes in profiles.items() if (record.get("http"), path) in routes)
        callable_profiles = [k for k in supported if k in ("runtime", "control")]
        dangerous = sem in ("BUSINESS_STATE_MUTATION", "EXTERNAL_SIDE_EFFECT")
        if dangerous: state = "DANGEROUS_NEVER_CALL"
        elif life == "RETIRED": state = "RETIRED"
        elif life == "UNRESOLVED" or not reviewed: state = "UNREVIEWED"
        elif sem == "ARTIFACT_GENERATION": state = "ARTIFACT_CANDIDATE"
        elif callable_profiles: state = "SUPPORTED_AND_USED" if path in used else "SUPPORTED_NOT_USED"
        elif sem == "READ" and record.get("side_effects") == "NONE": state = "REVIEWED_READ_CANDIDATE"
        else: state = "UNREVIEWED"
        out.append({"path": path, "http": record.get("http"), "roles": sorted(k for k, v in matrix.items() if path in v),
                    "observed_at": now.isoformat(), "lifecycle": life, "semantics": sem,
                    "confidence": record.get("confidence", "UNRESOLVED"), "reviewed": reviewed,
                    "runtime_supported": bool(supported), "supported_profiles": supported,
                    "runtime_callable_profiles": callable_profiles, "dangerous": dangerous,
                    "product_state": state, "usage_basis": "SELECTED_IMPLEMENTED_DEPENDENCY_NOT_LIVE_TRAFFIC",
                    "review": record.get("review"), "retirement": record.get("retirement"),
                    "alias_target": record.get("alias_target"), "alias_evidence": record.get("alias_evidence")})
    return {"schema_version": 1, "observed_at": now.isoformat(), "methods": out,
            "role_methods": matrix, "policy_version": policy["schema_version"],
            "policy_spec_sha256": policy["spec_sha256"],
            "policy_sha256": hashlib.sha256(json.dumps(policy, sort_keys=True).encode()).hexdigest(),
            "product_state_counts": {s: sum(m["product_state"] == s for m in out) for s in sorted({m["product_state"] for m in out})}}


def evaluate_seller_roles(roles_response, entities, now: datetime, policy: dict | None = None, *,
                          model=STRICT_MODEL, authentication_ok=None, identity_valid=None,
                          runtime_integrity=None, profiles=None) -> dict:
    """Вердикт по ключу Seller. Только имена ролей и методов — значения ключа сюда не попадают."""
    import seller_policy as SP
    policy = SP.validate(load_policy() if policy is None else policy)
    classes = policy["methods"]
    blocking, warnings = [], []
    broad = model == BROAD_MODEL
    if model not in CREDENTIAL_MODELS:
        blocking.append("KEY_MODEL_INVALID: unapproved credential model")
    if broad:
        if authentication_ok is not True: blocking.append("KEY_AUTH_FAILED: Seller Info not accepted")
        if identity_valid is not True: blocking.append("KEY_IDENTITY_INVALID: valid V2 identity required")
        if not isinstance(runtime_integrity, dict) or runtime_integrity.get("status") != "PASS" or runtime_integrity.get("binding_gate_integrity") != "PASS":
            blocking.append("KEY_RUNTIME_INTEGRITY_FAILED: runtime/binding evidence required")
        if not profiles:
            blocking.append("KEY_RUNTIME_PROFILES_MISSING: exact profiles required")
        else:
            actual_integrity = runtime_authorization_evidence(policy, profiles)
            if runtime_integrity != actual_integrity or actual_integrity["status"] != "PASS":
                blocking.append("KEY_RUNTIME_INTEGRITY_FAILED: proof does not match policy/profiles/source")
    roles = roles_response.get("roles") if isinstance(roles_response, dict) else None
    if not isinstance(roles, list) or not roles:
        blocking.append("KEY_NO_ROLES: inspection has no valid roles")
        roles = []
    if any(not isinstance(r, dict) or not isinstance(r.get("name"), str)
           or not isinstance(r.get("methods"), list)
           or any(not isinstance(m, str) or not re.fullmatch(r"/[A-Za-z0-9/_{}.*-]+", m) for m in r.get("methods", []))
           for r in roles):
        blocking.append("KEY_ROLES_MALFORMED: inspection matrix invalid")
        roles = []
    methods = sorted({m for r in roles for m in r["methods"]})
    matrix = {name: sorted({m for r in roles if r["name"] == name for m in r["methods"]})
              for name in {r["name"] for r in roles}}
    outcomes = {m: SP.treatment(classes.get(m), policy["schema_version"]) for m in methods}
    unknown = [m for m in methods if m not in classes or
               (policy["schema_version"] == 2 and classes[m]["lifecycle"] == "UNRESOLVED") or
               (policy["schema_version"] == 1 and classes[m]["class"] == "UNKNOWN")]
    mutation = [m for m in methods if outcomes[m] == "FAIL" and
                (classes.get(m, {}).get("semantics") in ("BUSINESS_STATE_MUTATION", "EXTERNAL_SIDE_EFFECT") or
                 (policy["schema_version"] == 1 and classes.get(m, {}).get("class") == "MUTATION"))]
    denied = [m for m in methods if outcomes[m] == "FAIL"]
    required = required_seller_methods(entities)
    if broad and profiles:
        required |= {p for k in ("runtime", "control") for _method, p in profiles.get(k, ())}
    missing = sorted(required - {m for m in methods if outcomes[m] != "FAIL"
                    and classes.get(m, {}).get("lifecycle") not in ("RETIRED", "ALIAS")})
    known_entities = set(ENTITY_SELLER_METHODS) | {"ads_campaigns", "ads_expense_daily", "ads_sku_daily"}
    if set(entities) - known_entities:
        blocking.append("KEY_UNKNOWN_ENTITY: unreviewed execution entity")
    if unknown:
        (warnings if broad else blocking).append(f"KEY_UNKNOWN_METHODS: {len(unknown)} unresolved methods")
    if mutation:
        (warnings if broad else blocking).append(f"KEY_MUTATION_CAPABLE: {len(mutation)} dangerous reported capabilities")
    if set(denied) - set(unknown) - set(mutation):
        (warnings if broad else blocking).append("KEY_SEMANTICS_UNPROVEN: capability side effects unresolved")
    if missing:
        blocking.append(f"KEY_MISSING_REQUIRED: {len(missing)} required methods unavailable")
    warnings.extend(f"KEY_CAPABILITY_WARNING: {m}" for m in methods if outcomes[m] == "WARN")
    exp = None
    try:
        exp = _parse_ts((roles_response or {}).get("expires_at"))
    except (ValueError, TypeError, AttributeError):
        blocking.append("KEY_EXPIRY_UNPARSEABLE: expires_at не разобран")
    if exp is not None and exp <= now:
        blocking.append("KEY_EXPIRED: срок ключа истёк")
    elif exp is not None and exp - now < timedelta(days=EXPIRY_WARNING_DAYS):
        warnings.append(f"KEY_EXPIRES_SOON: меньше {EXPIRY_WARNING_DAYS} суток")
    elif exp is None and "KEY_EXPIRY_UNPARSEABLE" not in " ".join(blocking):
        warnings.append("KEY_EXPIRY_UNKNOWN: срок ключа не указан")
    risk = []
    if mutation: risk.append("WARN_DANGEROUS_REPORTED")
    if unknown or set(denied) - set(mutation): risk.append("WARN_UNREVIEWED")
    if any(classes.get(m, {}).get("lifecycle") == "RETIRED" for m in methods): risk.append("WARN_RETIRED")
    if any(classes.get(m, {}).get("semantics") == "ARTIFACT_GENERATION" for m in methods): risk.append("WARN_ARTIFACT_REPORTED")
    proof = runtime_integrity if isinstance(runtime_integrity, dict) else {}
    dimensions = {"authentication": "PASS" if authentication_ok is True else "FAIL" if authentication_ok is False else "UNPROVEN",
                  "identity": "PASS" if identity_valid is True else "FAIL" if identity_valid is False else "UNPROVEN",
                  "required_runtime_coverage": "FAIL_MISSING_REQUIRED" if missing else "PASS",
                  "capability_inventory_risk": risk or ["CLEAN"],
                  "runtime_authorization_integrity": proof.get("status", "UNPROVEN"),
                  "binding_gate_integrity": proof.get("binding_gate_integrity", "UNPROVEN"),
                  "inspection_integrity": "FAIL" if any(b.startswith(("KEY_NO_ROLES", "KEY_ROLES_MALFORMED")) for b in blocking) else "PASS",
                  "expiry_integrity": "FAIL" if any(b.startswith(("KEY_EXPIRED", "KEY_EXPIRY_UNPARSEABLE")) for b in blocking) else "WARN_UNKNOWN" if exp is None else "PASS"}
    return {
        "api": "seller", "credential_model": model, "dimensions": dimensions,
        "runtime_integrity": runtime_integrity,
        "capability_discovery": capability_discovery(policy, matrix, profiles or {}, entities, now),
        "status": "FAIL" if blocking else "PASS",
        "blocking": blocking, "warnings": warnings,
        "role_names": sorted({str(r.get("name")) for r in roles if isinstance(r, dict)}),
        "method_count": len(methods),
        "mutation_methods": mutation, "unknown_methods": unknown, "missing_methods": missing,
        "expires_at": exp.isoformat() if exp else None,
        "policy_spec_sha256": policy.get("spec_sha256"),
        "policy_version": policy["schema_version"], "role_methods": matrix,
        "capability_outcomes": outcomes, "permission_execution": "UNPROVEN",
    }


def evaluate_performance(token_ok: bool, read_ok: bool, http_token=None, http_read=None) -> dict:
    """Вердикт по Performance API: токен и безвредное чтение (GET /api/client/campaign)."""
    blocking = []
    if not token_ok:
        blocking.append("PERF_AUTH_FAILED: токен не получен")
    elif not read_ok:
        blocking.append("PERF_READ_FAILED: список кампаний не прочитан")
    return {"api": "performance", "status": "FAIL" if blocking else "PASS", "blocking": blocking,
            "warnings": ["PERF_MUTATION_CAPABILITY_UNKNOWN: у Performance API нет инспекции прав"],
            "http_token": http_token, "http_read": http_read}
