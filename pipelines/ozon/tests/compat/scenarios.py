"""Сценарии различающего прогона Ozon runtime (Tenancy T2.2, L8).

Всё синтетическое: ни одного реального ключа, ни одного обращения к Ozon или
BigQuery. Секреты отличаются друг от друга и от данных, поэтому утечка любого из
них в выводе однозначна.
"""
from __future__ import annotations

import io
import json
import zipfile

# Имена секретов EVETIS: оба runtime (до T2 и текущий с конфигурацией EVETIS)
# обязаны запрашивать ровно их.
N_CLIENT_ID = "EVETIS_OZON_CLIENT_ID"
N_API_KEY = "EVETIS_OZON_API_KEY"
N_PERF_ID = "EVETIS_OZON_PERFORMANCE_CLIENT_ID"
N_PERF_SECRET = "EVETIS_OZON_PERFORMANCE_CLIENT_SECRET"

CLIENT_ID = "5550199"                                   # ИДЕНТИФИКАТОР (L4): не вырезается
PERF_CLIENT_ID = "5550199-1700000000000@advertising.performance.ozon.ru"   # ИДЕНТИФИКАТОР
API_KEY = "SYNTH-apikey-0b7c0f3e-5a41-4c0e-9d1a-2f6b8e4c1a77"              # СЕКРЕТ
ESCAPEY_API_KEY = 'SYNTH"api\\key\nline\ttab-ключ-☃'                       # СЕКРЕТ, экранируемые символы
PERF_SECRET = "SYNTH-perfsecret-Qx7vL2pT9mN4"                              # СЕКРЕТ
PERF_TOKEN = "SYNTH-perftoken-Zk3q9XwP2vL8"                                # СЕКРЕТ

# Роль секрета → маркер, которым текущий runtime его заменяет (намеренная разница D1).
SECRET_MARKERS = {"seller_api_key": "<redacted:seller_api_key>",
                  "performance_client_secret": "<redacted:performance_client_secret>",
                  "performance_access_token": "<redacted:performance_access_token>"}

LEGACY_EVETIS_ENV = {"GCP_PROJECT_ID": "project-fa311fc0-4d87-4781-986",
                     "BQ_RAW_DATASET": "ozon_raw", "BQ_LOCATION": "EU",
                     "INGESTION_RUN_ID": "rt-compat", "SINCE": "2026-09-01", "UNTIL": "2026-09-02"}

SELLER_ENTITIES = "catalog,prices,seller_info,stocks,fbo_postings,finance_accrual,clusters,supplies,promo"
PERF_ENTITIES = "ads_campaigns,ads_expense_daily,ads_sku_daily"


def secrets_for(scenario: dict) -> dict:
    key = ESCAPEY_API_KEY if scenario.get("escapey_key") else API_KEY
    return {N_CLIENT_ID: CLIENT_ID, N_API_KEY: key, N_PERF_ID: PERF_CLIENT_ID,
            N_PERF_SECRET: PERF_SECRET}


def secret_values_by_role(scenario: dict) -> dict:
    """Только класс СЕКРЕТ: то, что не имеет права покинуть текущий runtime."""
    return {"seller_api_key": secrets_for(scenario)[N_API_KEY],
            "performance_client_secret": PERF_SECRET,
            "performance_access_token": PERF_TOKEN}


# ─────────────────────────────────────────────────────────── сценарии
# journal: "ok" | "fail" — insert_rows_json в OZON_INGESTION_RUNS падает всегда.
# api: флаги поведения фальшивого Ozon API.
# compare: "exact" — побайтное равенство после ПЕРЕЧИСЛЕННЫХ разниц (D1, D2);
#          "current_only" — у эталона нет этой функции (строгий режим), проверяется
#          только текущий runtime.
SCENARIOS = {
    "01_seller_success": dict(entities=SELLER_ENTITIES),
    "02_performance_success": dict(entities=PERF_ENTITIES),
    "03_normal_pagination": dict(entities="fbo_postings,finance_accrual",
                                 api={"fbo_pages": 3, "fin_pages_per_day": 2}),
    "04_seller_api_error": dict(entities="seller_info,catalog,stocks",
                                api={"seller_errors": {"/v1/seller/info": 500, "/v3/product/list": 401}}),
    "05_performance_api_error": dict(entities=PERF_ENTITIES, api={"perf_token_status": 401}),
    "06_page_caps_lenient": dict(entities="fbo_postings,finance_accrual", api={"endless": True}),
    "07_page_caps_strict": dict(entities="fbo_postings,finance_accrual", api={"endless": True},
                                env={"STRICT_PAGE_CAPS": "1"}, compare="current_only"),
    "08_journal_success": dict(entities="catalog,clusters"),
    "09_journal_failure": dict(entities="catalog,clusters", journal="fail"),
    "10_api_and_journal_failure_with_echo": dict(entities="seller_info,catalog",
                                                 api={"echo_key_on": "/v1/seller/info"},
                                                 journal="fail"),
    "11_credential_echo": dict(entities="seller_info,catalog", api={"echo_key_on": "/v1/seller/info"}),
    "12_escaped_secret_echo": dict(entities="seller_info,catalog", escapey_key=True,
                                   api={"echo_key_on": "/v1/seller/info"}),
    "13_client_id_as_data": dict(entities="seller_info,catalog,stocks",
                                 api={"client_id_as_data": True}),
    "14_performance_echo": dict(entities="ads_campaigns,ads_sku_daily",
                                api={"perf_echo_secret": True}),
}


# ─────────────────────────────────────────────────── фальшивый Ozon API
def _zip(files: dict) -> str:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, text in files.items():
            z.writestr(name, text)
    return buf.getvalue().decode("utf-8", "surrogateescape")


class FakeOzon:
    def __init__(self, scenario: dict):
        self.api = scenario.get("api", {})
        self.pages = {"fbo": 0, "fin": 0}

    def __call__(self, url: str, headers: dict, body):
        path = url.split(".ru", 1)[1]
        a = self.api
        if path == "/api/client/token":
            status = a.get("perf_token_status")
            if status:
                return status, {"_error": f"invalid client {body}"}
            return 200, {"access_token": PERF_TOKEN}
        if a.get("echo_key_on") == path:
            # Худший случай: сервер повторяет присланный Api-Key в теле ошибки.
            return 401, {"_error": f"invalid Api-Key {headers.get('Api-key')} "
                                   f"for Client-Id {headers.get('Client-id')}"}
        err = a.get("seller_errors", {}).get(path)
        if err:
            return err, {"_error": f"upstream said {err}"}
        if path == "/v3/product/list":
            return 200, {"result": {"items": [{"product_id": 1, "sku": 500, "offer_id": "o1"}],
                                    "total": 1, "last_id": "x"}}
        if path == "/v3/product/info/list":
            sku = int(CLIENT_ID) if a.get("client_id_as_data") else 500
            return 200, {"items": [{"sku": sku, "id": 1, "offer_id": "o1", "name": "n"}]}
        if path == "/v5/product/info/prices":
            return 200, {"items": [{"offer_id": "o1", "product_id": 1, "price": {"price": "100"},
                                    "commissions": {"sales_percent_fbo": 20, "new_field": 1}}],
                         "cursor": ""}
        if path == "/v1/seller/info":
            if a.get("client_id_as_data"):
                return 400, {"_error": f"campaign {CLIENT_ID} not found; retry after {CLIENT_ID} ms"}
            return 200, {"company": {"tax_system": "USN", "inn": "0"}, "subscription": {"is_premium": False}}
        if path == "/v1/rating/summary":
            return 200, {"premium": False}
        if path == "/v1/analytics/stocks":
            wh = int(CLIENT_ID) if a.get("client_id_as_data") else 7
            return 200, {"items": [{"sku": 500, "warehouse_id": wh, "available_stock_count": 3}]}
        if path == "/v3/posting/fbo/list":
            self.pages["fbo"] += 1
            n = self.pages["fbo"]
            more = a.get("endless") or n < a.get("fbo_pages", 1)
            return 200, {"postings": [{"posting_number": f"P{n}", "created_at": "2026-09-01T10:00:00Z",
                                       "products": [{"sku": 500, "quantity": 1, "price": "10"}]}],
                         "has_next": bool(more), "cursor": f"c{n}" if more else ""}
        if path == "/v1/finance/accrual/types":
            return 200, {"accrual_types": [{"id": 1, "description": "d"}]}
        if path == "/v1/finance/accrual/by-day":
            self.pages["fin"] += 1
            n = self.pages["fin"]
            per_day = a.get("fin_pages_per_day", 1)
            more = a.get("endless") or (n % per_day != 0)
            return 200, {"accruals": [{"accrual_id": f"A{n}", "accrued_category": "OTHER", "unit_number": n,
                                       "non_item_fee": {"type_id": 1, "accrued": {"amount": "1.5"}}}],
                         "last_id": f"L{n}" if more else ""}
        if path == "/api/client/campaign":
            if a.get("perf_echo_secret"):
                return 403, f"forbidden for token {headers.get('Authorization')}"
            return 200, json.dumps({"list": [{"id": "111", "title": "t"}, {"id": "222"}], "total": "2"})
        if path.startswith("/api/client/statistics/expense"):
            return 200, "ID;Название;Расход\n111;К;10,00\n222;К2;5,00\n"
        if path.startswith("/api/client/statistics/daily"):
            return 200, "ID;Показы;Клики\n111;10;1\n"
        if path == "/api/client/statistics":
            return 200, {"UUID": "u-1"}
        if path.startswith("/api/client/statistics/report"):
            return 200, _zip({"111_x.csv": "; h\nДень;sku;Расход, ₽, с НДС;Показы\n01.09.2026;500;1,00;1\n",
                              "222_x.csv": "; h\nДень;sku;Расход, ₽, с НДС;Показы\n02.09.2026;501;2,00;2\n"})
        if path.startswith("/api/client/statistics/"):
            return 200, {"state": "OK"}
        if path == "/v1/cluster/list":
            return 200, {"clusters": [{"id": 1, "logistic_clusters": [{"warehouses": [{"warehouse_id": 9}]}]}]}
        if path == "/v3/supply-order/list":
            return 200, {"order_ids": [], "last_id": ""}
        if path == "/v1/actions":
            return 200, {"result": [{"id": 77, "title": "a", "action_type": "DISCOUNT"}]}
        if path in ("/v1/actions/products", "/v1/actions/candidates"):
            return 200, {"result": {"products": [{"id": 1, "price": 100}], "total": 1, "last_id": ""}}
        if path.startswith("/v1/actions/auto-add"):
            return 200, {"result": {"products": []}}
        raise AssertionError(f"сценарий не знает пути {path}")
