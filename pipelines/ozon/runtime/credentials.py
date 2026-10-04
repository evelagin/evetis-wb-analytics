"""Проверка capability inventory Ozon (Tenancy policy v2; supersedes historical D2).

Чистый модуль. Источник истины о возможностях ключа Seller API — фактический набор методов из
POST /v1/roles, а не название роли. Классы методов — seller_method_policy.json (генерирует
tools/tenancy/ozon_method_policy.py из Swagger; хеш спецификации записан в политике).

Решение по ключу Seller:
  FAIL (BLOCKING), если
    * методов нет вовсе или ответ /v1/roles не разобран;
    * активная business mutation, external side effect или unresolved semantics;
    * хоть один метод НЕ известен политике (UNKNOWN) — новый метод классифицируется только
      reviewed изменением политики, а не по regex или названию роли;
    * нет метода, обязательного для включённой сущности (или для самой проверки);
    * срок ключа истёк;
  WARNING — срок истекает меньше чем через EXPIRY_WARNING_DAYS или не указан.
Ключ никогда не меняется и не ограничивается автоматически.
RETIRED и reviewed ARTIFACT_GENERATION дают WARN, но не runtime permission.
Legacy class — provenance/suggestion; runtime routes — отдельный exact allowlist.

Performance API: API инспекции прав нет. Проверка — токен и безвредное чтение; способность к
изменениям не наблюдаема (UNKNOWN), а сам runtime вызывает только пути из белого списка
(common.PERF_ALLOWED_PATHS) — изменяющие методы не вызываются никогда.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

POLICY_FILE = Path(__file__).resolve().parent / "seller_method_policy.json"
EXPIRY_WARNING_DAYS = 30

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
    if not v:
        return None
    return datetime.fromisoformat(str(v).replace("Z", "+00:00")).astimezone(timezone.utc)


def evaluate_seller_roles(roles_response, entities, now: datetime, policy: dict | None = None) -> dict:
    """Вердикт по ключу Seller. Только имена ролей и методов — значения ключа сюда не попадают."""
    import seller_policy as SP
    policy = SP.validate(load_policy() if policy is None else policy)
    classes = policy["methods"]
    blocking, warnings = [], []
    roles = roles_response.get("roles") if isinstance(roles_response, dict) else None
    if not isinstance(roles, list) or not roles:
        blocking.append("KEY_NO_ROLES: inspection has no valid roles")
        roles = []
    if any(not isinstance(r, dict) or not isinstance(r.get("name"), str)
           or not isinstance(r.get("methods"), list)
           or any(not isinstance(m, str) or not m.startswith("/") for m in r.get("methods", []))
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
    missing = sorted(required_seller_methods(entities) - {m for m in methods if outcomes[m] != "FAIL"
                    and classes.get(m, {}).get("lifecycle") not in ("RETIRED", "ALIAS")})
    known_entities = set(ENTITY_SELLER_METHODS) | {"ads_campaigns", "ads_expense_daily", "ads_sku_daily"}
    if set(entities) - known_entities:
        blocking.append("KEY_UNKNOWN_ENTITY: unreviewed execution entity")
    if unknown:
        blocking.append(f"KEY_UNKNOWN_METHODS: {len(unknown)} unresolved methods")
    if mutation:
        blocking.append(f"KEY_MUTATION_CAPABLE: {len(mutation)} dangerous reported capabilities")
    if set(denied) - set(unknown) - set(mutation):
        blocking.append("KEY_SEMANTICS_UNPROVEN: capability side effects unresolved")
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
    return {
        "api": "seller",
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
