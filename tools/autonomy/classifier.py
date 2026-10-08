"""Детерминированный классификатор инженерных сигналов AE (AE-R1). Ни одного вызова модели.

Отвечает на вопрос «это дефект кода, который AE вправе чинить, или нет» ДО создания задачи
инженеру. Fail closed: сигнал, не попавший ни в одно правило, — UNCLASSIFIED (задачи нет).
Бизнесовые условия (устаревший ФФ, неутверждённый план продаж, ручные операции, отсутствие
данных у маркетплейса) инженерной задачей не становятся никогда.

Вход — нормализованный сигнал `tools/autonomy/signals.py` (только перечисления, счётчики и
отпечатки, без текста ошибок и строк данных). Классы и их allowlist путей — `policy.json`
(`task_classes`).
"""
from __future__ import annotations

import re

ENGINEERING = ("RETRY_CLASSIFIER_DEFECT", "PARITY_DEFECT", "SCHEMA_DRIFT", "DETECTOR_DEFECT", "LOADER_DEFECT")
NON_ENGINEERING = ("NOT_ENGINEERING", "UNCLASSIFIED")

# Коды, которыми загрузчик сам объявляет отказ временным (cloud/src/failure.ts и аналоги).
_RECORDED_TRANSIENT = re.compile(r"(?:^|_)(?:TRANSIENT|RATE_LIMIT|TIMEOUT|RETRY|UNAVAILABLE)(?:_|$)")


def _codes(policy: dict | None) -> dict:
    if policy is None:
        from tools.autonomy.policy import load_policy
        policy = load_policy()
    return policy["classifier_codes"]


# DRO-1 reason_code → решение. Причины, означающие «данных нет / опоздали», сами по себе — не
# дефект кода: инженерную задачу по прогону создаёт только журнал отказов (с кодом ошибки).
_DRO_NOT_ENGINEERING = {"OK", "CHECK_NOT_IMPLEMENTED", "NO_DATA_OBSERVED", "DATA_LOSS_CONFIRMED",
                        "DATA_LOSS_IMMINENT", "SLOT_MISSED", "SLOT_LATE", "FRESHNESS_STALE", "FRESHNESS_LATE",
                        "MISSING_DATES_RECOVERABLE"}
_DRO_DETECTOR = {"DETECTOR_STALE", "DETECTOR_NEVER_RAN"}
_DRO_RUN = {"RUN_FAILED", "RUN_PARTIAL"}


def _result(task_class: str, rule: str, reason: str) -> dict:
    return {"task_class": task_class, "engineering": task_class in ENGINEERING, "rule": rule, "reason": reason}


def classify_run_failure(sig: dict, recurrence_threshold: int, policy: dict | None = None) -> dict:
    """Строка журнала отказов (V_RUN_FAILURE_LEDGER). Инженерный класс — только для кодов из явных
    списков policy.json (classifier_codes); иначе UNCLASSIFIED. Код состояния данных/бизнеса — никогда."""
    cc = _codes(policy)
    code = (sig.get("error_code") or "").upper()
    signature = sig.get("failure_signature") or "OTHER"
    recorded_transient = bool(sig.get("recorded_as_transient")) or bool(_RECORDED_TRANSIENT.search(code))
    recurring = int(sig.get("occurrences_7d") or 0) >= recurrence_threshold
    if any(re.search(p, code) for p in cc["business_code_patterns"]):
        return _result("NOT_ENGINEERING", "R1_BUSINESS_CODE", f"код {code or '—'}: состояние данных/бизнеса, не дефект кода")
    if signature in ("AUTH", "QUOTA"):
        return _result("NOT_ENGINEERING", "R6_ACCESS_OR_QUOTA",
                       "доступ/квота источника: решение владельца (токен, тариф), не код")
    if signature == "TRANSIENT_UPSTREAM":
        if recorded_transient:
            return _result("NOT_ENGINEERING", "R2_TRANSIENT_HANDLED",
                           "внешний временный сбой, загрузчик сам распознал его как временный")
        if code in cc["retry_wrapper_codes"]:
            return _result("RETRY_CLASSIFIER_DEFECT", "R3_TRANSIENT_MISCLASSIFIED",
                           f"сигнатура источника — временный сбой, а обёрточный код {code} записан как детерминированный")
        return _result("UNCLASSIFIED", "R3X_TRANSIENT_UNKNOWN_CODE",
                       f"временная сигнатура при коде {code or '—'} вне списка обёрток — решение владельца")
    if code in cc["schema_codes"]:
        return _result("SCHEMA_DRIFT", "R4_SCHEMA", f"схема ответа источника: код {code}")
    if code in cc["parity_codes"]:
        if recurring:
            return _result("PARITY_DEFECT", "R5_PARITY", f"паритет/QA: код {code}, {sig.get('occurrences_7d')} раз за 7 суток")
        return _result("UNCLASSIFIED", "R5X_PARITY_SINGLE", f"паритет/QA {code} однократно — может быть временем данных")
    if code in cc["loader_defect_codes"] and not recorded_transient:
        if recurring:
            return _result("LOADER_DEFECT", "R7_RECURRING_DETERMINISTIC",
                           f"код {code} повторился {sig.get('occurrences_7d')} раз за 7 суток")
        return _result("UNCLASSIFIED", "R7X_SINGLE", f"код {code} однократно — ждём повторения")
    return _result("UNCLASSIFIED", "R0_NO_RULE", f"код {code or '—'} вне явных списков — fail closed")


def classify_health(sig: dict) -> dict:
    """Строка DRO-1 (V_DATA_HEALTH_CURRENT + открытый инцидент)."""
    reason = (sig.get("reason_code") or "").upper()
    if sig.get("serving_status") == "HEALTHY":
        return _result("NOT_ENGINEERING", "H0_HEALTHY", "конвейер здоров")
    if (sig.get("data_class") or "").upper() == "MANUAL" or (sig.get("source_system") or "").upper() == "MANUAL":
        return _result("NOT_ENGINEERING", "H1_MANUAL_INPUT",
                       "ручной/бизнесовый вход (ФФ, план продаж, ручная операция) — только владелец")
    if reason in _DRO_DETECTOR:
        # Устаревший снимок бывает операционным (планировщик на паузе — документированный откат DRO-1):
        # без доказательства сбоя самого детектора задачи нет. DETECTOR_DEFECT — только цель владельца.
        return _result("UNCLASSIFIED", "H2_DETECTOR_STALE_NEEDS_EVIDENCE",
                       f"детектор DRO-1: {reason}; причина может быть операционной — решение владельца")
    if reason in _DRO_NOT_ENGINEERING:
        return _result("NOT_ENGINEERING", "H3_DATA_ABSENT_OR_LATE",
                       f"{reason}: данных нет или они опаздывают — сам по себе не дефект кода")
    if reason in _DRO_RUN:
        # Падение прогона в DRO — подтверждение, а не диагноз: класс даёт журнал отказов с кодом ошибки.
        return _result("UNCLASSIFIED", "H4_RUN_FAILED_NEEDS_LEDGER",
                       f"{reason}: диагноз только по журналу отказов (код ошибки), не по DRO")
    return _result("UNCLASSIFIED", "H0_NO_RULE", f"причина {reason or '—'} не классифицирована")


def classify(sig: dict, recurrence_threshold: int = 3, policy: dict | None = None) -> dict:
    kind = sig.get("kind")
    if kind == "run_failure":
        return classify_run_failure(sig, recurrence_threshold, policy)
    if kind == "dro_health":
        return classify_health(sig)
    return _result("UNCLASSIFIED", "X0_UNKNOWN_KIND", f"неизвестный вид сигнала {kind!r}")
