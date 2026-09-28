"""STRICT_PAGE_CAPS (Tenancy T2): молчаливая обрезка → явный отказ, но только по флагу.

Для каждого места, найденного аудитом T0, проверяются обе стороны:
  * флаг выключен — поведение прежнее: те же запросы, та же частичная загрузка;
  * флаг включён — StrictLimitError с диагностикой, ничего не записано.
"""
from __future__ import annotations

import io
import json
import zipfile

import pytest

import common as C


@pytest.fixture
def strict(monkeypatch):
    monkeypatch.setattr(C, "STRICT_PAGE_CAPS", True)


@pytest.fixture
def lenient(monkeypatch):
    monkeypatch.setattr(C, "STRICT_PAGE_CAPS", False)


# ─────────────────────────────────────────────────────── fbo_postings
def _endless_postings(calls):
    def seller_post(path, body):
        assert path == "/v3/posting/fbo/list"
        calls.append(body["cursor"])
        n = len(calls)
        return 200, {"postings": [{"posting_number": f"P-{n}", "created_at": "2026-09-01T10:00:00Z",
                                   "products": [{"sku": 1, "quantity": 1, "price": "10"}]}],
                     "has_next": True, "cursor": f"c{n}"}
    return seller_post


def test_fbo_postings_cap_is_silent_without_flag(entities, captured_merges, monkeypatch, lenient):
    calls = []
    monkeypatch.setattr(entities, "seller_post", _endless_postings(calls))
    entities.fbo_postings("rt-x", "ts", "2026-09-01", "2026-09-02")
    # прежнее поведение: страницы 1..201, затем обрезка и запись того, что успели
    assert len(calls) == entities.FBO_POSTINGS_MAX_PAGES + 1
    (table, rows, _keys, _kw), = captured_merges
    assert table == "RAW_OZON_POSTINGS_FBO" and len(rows) == 201


def test_fbo_postings_cap_fails_in_strict_mode(entities, captured_merges, monkeypatch, strict):
    monkeypatch.setattr(entities, "seller_post", _endless_postings([]))
    with pytest.raises(entities.StrictLimitError) as e:
        entities.fbo_postings("rt-x", "ts", "2026-09-01", "2026-09-02")
    msg = str(e.value)
    assert "fbo_postings" in msg and "200 страниц" in msg and "2026-09-01..2026-09-02" in msg
    assert captured_merges == [], "в строгом режиме частичная партия не пишется"


def test_fbo_postings_normal_end_is_not_a_cap(entities, captured_merges, monkeypatch, strict):
    pages = iter([{"postings": [], "has_next": True, "cursor": "c1"},
                  {"postings": [], "has_next": False, "cursor": ""}])
    monkeypatch.setattr(entities, "seller_post", lambda p, b: (200, next(pages)))
    entities.fbo_postings("rt-x", "ts", "2026-09-01", "2026-09-01")
    assert captured_merges[0][0] == "RAW_OZON_POSTINGS_FBO"


# ───────────────────────────────────────────────────── finance_accrual
def _endless_accruals(calls):
    def seller_post(path, body):
        if path == "/v1/finance/accrual/types":
            return 200, {"accrual_types": [{"id": 1, "description": "x"}]}
        calls.append(body)
        n = len(calls)
        return 200, {"accruals": [{"accrual_id": f"A{n}", "accrued_category": "OTHER",
                                   "unit_number": n,
                                   "non_item_fee": {"type_id": 1, "accrued": {"amount": "1"}}}],
                     "last_id": f"L{n}"}
    return seller_post


def test_finance_cap_is_silent_without_flag(entities, captured_merges, monkeypatch, lenient):
    calls = []
    monkeypatch.setattr(entities, "seller_post", _endless_accruals(calls))
    entities.finance_accrual("rt-x", "ts", "2026-09-01", "2026-09-01")
    assert len(calls) == entities.FINANCE_ACCRUAL_MAX_PAGES_PER_DAY + 1
    assert len(captured_merges[0][1]) == 61


def test_finance_cap_fails_in_strict_mode(entities, captured_merges, monkeypatch, strict):
    monkeypatch.setattr(entities, "seller_post", _endless_accruals([]))
    with pytest.raises(entities.StrictLimitError, match="finance_accrual.*60 страниц.*day=2026-09-01"):
        entities.finance_accrual("rt-x", "ts", "2026-09-01", "2026-09-01")
    assert captured_merges == []


# ─────────────────────────────────────────────────────── ads_sku_daily
CAMPAIGNS = json.dumps({"list": [{"id": "111"}], "total": "1"})
EXPENSE = "ID;Название;Расход\n111;Кампания;10,00\n"
REPORT_CSV = "; Кампания 111\nДень;sku;Расход, ₽, с НДС;Показы;Клики\n01.09.2026;500;1,00;1;1\n"


def _zip(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, text in files.items():
            z.writestr(name, text)
    return buf.getvalue().decode("utf-8", "surrogateescape")


def _perf(monkeypatch, entities, *, campaign=(200, CAMPAIGNS), expense=(200, EXPENSE),
          submit=(200, {"UUID": "u-1"}), state="OK", report=None):
    calls = []
    report = report or (200, _zip({"111_report.csv": REPORT_CSV}))

    def perf_get(path, raw_text=True):
        calls.append(path)
        if path == "/api/client/campaign":
            return campaign
        if path.startswith("/api/client/statistics/expense"):
            return expense
        if path.startswith("/api/client/statistics/report"):
            return report
        if path.startswith("/api/client/statistics/"):
            return 200, {"state": state}
        raise AssertionError(path)

    def perf_post(path, body):
        calls.append(path)
        return submit

    monkeypatch.setattr(entities, "perf_get", perf_get)
    monkeypatch.setattr(entities, "perf_post", perf_post)
    return calls


def test_ads_happy_path_is_unchanged_in_both_modes(entities, captured_merges, monkeypatch, strict):
    _perf(monkeypatch, entities)
    entities.ads_sku_daily("rt-x", "ts", "2026-09-01", "2026-09-02")
    (table, rows, _k, _kw), = captured_merges
    assert table == "RAW_OZON_ADS_SKU_DAILY" and len(rows) == 1


@pytest.mark.parametrize("scenario,kwargs,expect", [
    ("campaign_http", {"campaign": (503, "")}, "список кампаний не получен"),
    ("expense_http", {"expense": (503, "")}, "расход кампаний за окно не получен"),
    ("no_uuid", {"submit": (200, {})}, "нет UUID"),
    ("report_error", {"state": "ERROR"}, "отчёт не готов или завершился ошибкой"),
    ("report_http", {"report": (404, "")}, "отчёт не скачан"),
])
def test_ads_silent_skips(entities, captured_merges, monkeypatch, scenario, kwargs, expect):
    # без флага — прежнее поведение: пропуск, запись пустой (или частичной) партии
    monkeypatch.setattr(C, "STRICT_PAGE_CAPS", False)
    _perf(monkeypatch, entities, **kwargs)
    entities.ads_sku_daily("rt-x", "ts", "2026-09-01", "2026-09-02")
    assert captured_merges[-1][0] == "RAW_OZON_ADS_SKU_DAILY"
    rows_lenient = len(captured_merges[-1][1])
    if scenario != "report_error":            # ERROR-отчёт всё равно скачивается, как раньше
        assert rows_lenient == 0

    # с флагом — явный отказ с диагностикой, ничего не записано
    captured_merges.clear()
    monkeypatch.setattr(C, "STRICT_PAGE_CAPS", True)
    _perf(monkeypatch, entities, **kwargs)
    with pytest.raises(entities.StrictLimitError, match=expect):
        entities.ads_sku_daily("rt-x", "ts", "2026-09-01", "2026-09-02")
    assert captured_merges == []


def test_single_campaign_csv_is_still_lost_without_flag(entities, captured_merges, monkeypatch,
                                                        lenient):
    """P2-6 без флага — прежнее поведение EVETIS: CSV одной кампании не разбирается (пусто)."""
    _perf(monkeypatch, entities, report=(200, REPORT_CSV))
    entities.ads_sku_daily("rt-x", "ts", "2026-09-01", "2026-09-02")
    assert captured_merges[-1][1] == []


# Строгий режим T5 больше не отвергает длинное окно и CSV одной кампании, а исправляет их:
# tests pipelines/ozon/tests/test_t5_runtime_blockers.py (окна по датам МСК, P2-5/P2-6).


def test_ads_long_window_without_flag_keeps_old_single_request(entities, captured_merges,
                                                               monkeypatch, lenient):
    """Без флага окно не дробится и не проверяется: запрос один, как раньше."""
    calls = _perf(monkeypatch, entities)
    entities.ads_sku_daily("rt-x", "ts", "2026-01-01", "2026-03-05")
    assert calls.count("/api/client/statistics") == 1


def test_strict_messages_carry_no_credentials(entities, monkeypatch, strict):
    monkeypatch.setitem(C._secrets, "EVETIS_OZON_API_KEY", "sentinel-api-key-value-0001")
    _perf(monkeypatch, entities, campaign=(503, ""))
    with pytest.raises(entities.StrictLimitError) as e:
        entities.ads_sku_daily("rt-x", "ts", "2026-09-01", "2026-09-02")
    assert "sentinel-api-key-value-0001" not in str(e.value)
