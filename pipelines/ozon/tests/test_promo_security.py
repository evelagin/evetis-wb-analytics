"""Механическая проверка границы записи наблюдателя акций Ozon.

Ключ EVETIS имеет роль Admin: мутирующие методы акций ему технически доступны,
и в отличие от WB (там бит 30 в токене отрезает запись физически) здесь
единственная настоящая граница — программная. Поэтому она проверяется дважды:

  1. по ИСХОДНОМУ ТЕКСТУ — ни один запрещённый путь не должен встречаться в
     наблюдателе даже в виде константы или комментария;
  2. по ПОВЕДЕНИЮ — promo_call отвергает путь вне закрытого списка до выхода в сеть.

Список запрещённых путей живёт здесь, а не в runtime: иначе проверка №1 нашла бы
собственное определение.
"""

from __future__ import annotations

from pathlib import Path

import pytest

RUNTIME = Path(__file__).resolve().parents[1] / "runtime"
PROMO_SRC = RUNTIME / "promo.py"

DENIED_PATHS = [
    "/v1/actions/products/activate",
    "/v1/actions/products/deactivate",
    "/v1/actions/auto-add/products/update",
    "/v1/actions/auto-add/products/delete",
    "/v1/actions/discounts-task/approve",
    "/v1/actions/discounts-task/decline",
    "/v1/actions/hotsales/activate",
    "/v1/actions/hotsales/deactivate",
    "/v1/seller-actions/create",
    "/v1/seller-actions/update",
    "/v1/seller-actions/products/add",
    "/v1/product/import/prices",
    "/v1/product/update/discount",
    "/v1/pricing-strategy/create",
    "/v1/pricing-strategy/update",
    "/v1/pricing-strategy/products/add",
    "/api/v1/calendar/promotions/upload",
]

# Источник ПДн покупателей. Решение владельца (фаза 0.5):
# DEFERRED_PRIVACY_SENSITIVE_SOURCE — в V1 не загружается.
PII_SOURCE = "/v1/actions/discounts-task/list"
PII_FIELDS = ["customer_name", "first_name", "last_name", "patronymic",
              "email", "user_comment"]


@pytest.fixture(scope="module")
def src() -> str:
    return PROMO_SRC.read_text(encoding="utf-8")


def test_source_file_exists(src):
    assert len(src) > 0


def test_no_mutating_path_in_source(src):
    hits = [p for p in DENIED_PATHS if p in src]
    assert hits == [], f"в наблюдателе найдены мутирующие пути: {hits}"


def test_pii_source_absent(src):
    assert PII_SOURCE not in src, "источник ПДн покупателей не должен упоминаться в V1"


def test_pii_fields_absent(src):
    hits = [f for f in PII_FIELDS if f in src]
    assert hits == [], f"поля ПДн покупателя в наблюдателе: {hits}"


def test_allowed_paths_are_read_only():
    import promo as M

    assert sorted(M.ALLOWED_PATHS) == sorted([
        "/v1/actions",
        "/v1/actions/products",
        "/v1/actions/candidates",
        "/v1/actions/auto-add/products/list",
        "/v1/actions/auto-add/products/candidates",
        "/v5/product/info/prices",
    ])


@pytest.mark.parametrize("path", DENIED_PATHS)
def test_promo_call_refuses_denied_path(path, monkeypatch):
    """Путь вне списка роняет вызов ДО формирования запроса и до сети."""
    import promo as M

    def _boom(*_a, **_k):
        raise AssertionError("запрос ушёл в сеть, хотя путь запрещён")

    monkeypatch.setattr(M.C, "_request", _boom)
    monkeypatch.setattr(M.C, "secret", lambda n: "fake")
    with pytest.raises(M.PromoPathDenied):
        M.promo_call(path, {})


def test_promo_call_allows_listed_path(monkeypatch):
    import promo as M

    captured = {}

    def _fake_request(req, *_a, **_k):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        return 200, {"result": []}

    monkeypatch.setattr(M.C, "_request", _fake_request)
    monkeypatch.setattr(M.C, "secret", lambda n: "fake")
    code, _ = M.promo_call("/v1/actions")
    assert code == 200
    assert captured["method"] == "GET"
    assert captured["url"].endswith("/v1/actions")


def test_no_secret_value_in_source(src):
    """Секреты читаются по ИМЕНИ из Secret Manager и не хардкодятся.

    Tenancy T2: имена секретов promo не знает вовсе — заголовки собирает
    common.seller_headers() по конфигурации процесса. Литерал имени секрета
    EVETIS здесь означал бы, что наблюдатель привязан к одному продавцу.
    """
    assert "seller_headers()" in src             # единственный путь к учётным данным
    assert "EVETIS_OZON_" not in src             # имя секрета конкретного продавца — нельзя
    for marker in ("Api-Key: ", "client_secret=", "-----BEGIN"):
        assert marker not in src                 # значение — нельзя
