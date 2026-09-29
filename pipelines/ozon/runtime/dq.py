"""Проверки DQ и сверки перед READY (Tenancy T5, §K) — чистые функции над уже агрегированными фактами.

Control читает таблицы Tables API (tabledata.list) и сворачивает строки потоково; здесь — только
решения. Уровни: INFO, WARNING, BLOCKING. READY невозможен, пока хоть одна BLOCKING-проверка не PASS
(lifecycle_core.ready_contract, READY_REQUIRED_DQ).

Покрытие FBO — сутки МСК: сутки D (МСК) = [D−1 21:00Z, D 21:00Z) покрыты, если UTC-сутки D−1 и D
обе внутри DONE-отрезков (окна FBO — UTC-сутки, T5-G). Начисления и реклама — сутки запроса/отчёта.
"""
from __future__ import annotations

from datetime import date, timedelta

PASS, FAIL = "PASS", "FAIL"
INFO, WARNING, BLOCKING = "INFO", "WARNING", "BLOCKING"
# Допуск расхождения суммы SKU-расхода с расходом CPC-кампании (EVETIS: 95/148 пар в пределах 1 %).
ADS_SUM_WARN_PCT = 5.0


def _check(checks, cid, ok, severity, metric=None, explanation=None, **values):
    checks[cid] = {"status": PASS if ok else FAIL, "severity": severity, "metric": metric,
                   "explanation": explanation, **values}


def days(start: date, end: date):
    return {start + timedelta(days=i) for i in range((end - start).days + 1)}


def covered_days(domain: str, done_windows) -> set:
    """Сутки покрытия домена по DONE-окнам [(start, end)]; для FBO — сутки МСК."""
    utc = set()
    for s, e in done_windows:
        utc |= days(s, e)
    if domain != "fbo_postings":
        return utc
    return {d for d in utc if (d - timedelta(days=1)) in utc}


def evaluate(f: dict) -> dict:
    """f — факты: см. ключи ниже. Возвращает {check_id: {...}}."""
    checks = {}
    b = f.get("binding") or {}
    need_perf = f.get("ads_enabled", False)
    ok_binding = b.get("seller") == "BOUND" and (not need_perf or b.get("performance") == "BOUND")
    _check(checks, "BINDING", ok_binding, BLOCKING, "binding_status", "привязка кабинета", binding=b)

    chunks = f.get("chunks") or {}
    not_done = sorted(c for c, st in chunks.items() if st != "DONE")
    permanent = sorted(c for c, st in chunks.items() if st == "FAILED_PERMANENT")
    _check(checks, "CHECKPOINTS_COMPLETE", bool(chunks) and not not_done, BLOCKING, "chunks_not_done",
           f"не завершено {len(not_done)}, навсегда упало {len(permanent)}", value=len(not_done))

    gaps = {}
    for dom, (need_from, need_to) in (f.get("required_ranges") or {}).items():
        need = days(need_from, need_to) if need_from and need_to and need_from <= need_to else set()
        have = covered_days(dom, (f.get("done_windows") or {}).get(dom, []))
        missing = sorted(need - have)
        if missing:
            gaps[dom] = {"days": len(missing), "first": str(missing[0])}
    _check(checks, "COVERAGE", not gaps, BLOCKING, "uncovered_days", "сутки без покрытия", gaps=gaps)

    trunc = f.get("truncated_runs") or []
    _check(checks, "TRUNCATION", not trunc, BLOCKING, "strict_failures_unresolved",
           "StrictLimitError/PaginationError без успешного повтора", value=len(trunc))

    dups = {t: n for t, n in (f.get("raw_duplicate_keys") or {}).items() if n}
    _check(checks, "RAW_UNIQUENESS", not dups and f.get("raw_duplicate_keys") is not None, BLOCKING,
           "duplicate_merge_keys", "повторы ключа слияния в RAW", dups=dups)

    unresolved = f.get("finance_unresolved_rows")
    _check(checks, "FIN_CLASSIFICATION", unresolved == 0, BLOCKING, "unresolved_accrual_rows",
           "начисления с типом вне справочника (UNKNOWN)", value=unresolved)

    m = f.get("maturity_days")
    _check(checks, "FIN_MATURITY", m is not None and m > 0, BLOCKING, "maturity_days",
           "FINANCE_SETTLEMENT_MATURITY_DAYS (D3) — обязателен для READY", value=m)

    worst = f.get("ads_sum_worst_pct")
    _check(checks, "ADS_SUM_RECONCILIATION", worst is None or worst <= ADS_SUM_WARN_PCT, WARNING,
           "worst_campaign_diff_pct", "сумма SKU-расхода против расхода CPC-кампании", value=worst)
    _check(checks, "FBS_ACTIVITY", not f.get("fbs_activity"), WARNING, "fbs_postings",
           "отправления FBS при контракте FBO-only (данные FBS не грузятся)")
    exp = f.get("key_expires_soon")
    _check(checks, "KEY_EXPIRY", not exp, WARNING, "key_expires_soon", "ключ Seller истекает скоро")
    _check(checks, "SNAPSHOT_HISTORY", True, INFO, None, "снимки без истории: каталог, цены, остатки")
    return checks


def blocking_failures(checks: dict) -> list:
    return sorted(c for c, v in checks.items() if v["severity"] == BLOCKING and v["status"] != PASS)
