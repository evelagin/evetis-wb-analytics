"""Проверка учётных данных Ozon (Tenancy T5, решение D2): политика «только чтение».

Чистый модуль. Источник истины о возможностях ключа Seller API — фактический набор методов из
POST /v1/roles, а не название роли. Классы методов — seller_method_policy.json (генерирует
tools/tenancy/ozon_method_policy.py из Swagger; хеш спецификации записан в политике).

Решение по ключу Seller:
  FAIL (BLOCKING), если
    * методов нет вовсе или ответ /v1/roles не разобран;
    * хоть один метод класса MUTATION вне approved_mutation_methods (сейчас пусто);
    * хоть один метод НЕ известен политике (UNKNOWN) — новый метод классифицируется только
      изменением политики, а не по догадке;
    * нет метода, обязательного для включённой сущности (или для самой проверки);
    * срок ключа истёк;
  WARNING — срок истекает меньше чем через EXPIRY_WARNING_DAYS или не указан.
Ключ никогда не меняется и не ограничивается автоматически.

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
    policy = json.loads(path.read_text(encoding="utf-8"))
    if policy.get("schema_version") != 1 or not isinstance(policy.get("methods"), dict):
        raise ValueError("политика методов Seller API повреждена")
    return policy


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
    policy = policy or load_policy()
    classes = policy["methods"]
    approved = set(policy.get("approved_mutation_methods") or [])
    blocking, warnings = [], []
    roles = (roles_response or {}).get("roles")
    if not isinstance(roles, list) or not roles:
        blocking.append("KEY_NO_ROLES: /v1/roles не вернул ни одной роли")
        roles = []
    methods = sorted({m for r in roles if isinstance(r, dict) for m in (r.get("methods") or [])
                      if isinstance(m, str)})
    unknown = [m for m in methods if m not in classes]
    mutation = [m for m in methods if classes.get(m, {}).get("class") == "MUTATION" and m not in approved]
    missing = sorted(required_seller_methods(entities) - set(methods))
    if unknown:
        blocking.append(f"KEY_UNKNOWN_METHODS: {len(unknown)} методов вне политики")
    if mutation:
        blocking.append(f"KEY_MUTATION_CAPABLE: {len(mutation)} методов изменения кабинета")
    if missing:
        blocking.append(f"KEY_MISSING_REQUIRED: нет {len(missing)} обязательных методов")
    exp = None
    try:
        exp = _parse_ts((roles_response or {}).get("expires_at"))
    except ValueError:
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
