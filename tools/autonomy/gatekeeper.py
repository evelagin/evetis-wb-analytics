"""Гейткипер AE v1. Детерминированный. Ни одна модель не может отменить его вердикт.

Вход — доказательства, вычисленные системой (не агентом), и вердикт ревьюера.
Правило симметрии: вердикт модели может только УЖЕСТОЧИТЬ итог. PASS ревьюера не
снимает ни одного детерминированного запрета; находка ревьюера снимает готовность.

Неизвестность не превращается в успех: BLOCKED/ERROR там, где доказательство обязательно,
даёт INCONCLUSIVE, а не READY_FOR_PR.
"""
from __future__ import annotations

PRIORITY = ["UNSAFE", "BLOCKED_BY_TEST", "BLOCKED_BY_RUNTIME_ACCESS", "BLOCKED_BY_DATA",
            "HUMAN_DECISION_REQUIRED", "INCONCLUSIVE", "READY_FOR_PR"]
FINAL_STATES = set(PRIORITY)
BAD = {"FAIL", "EMPTY", "ERROR"}


def _regressions(candidate: dict, baseline: dict) -> tuple[list[str], list[str], list[str]]:
    """(новые падения, нет доказательства, унаследованные падения) по проверкам наборов.

    Проверка, падающая и на базовой ветке, — не вина кандидата: она сообщается, но не
    блокирует. Проверка без доказательства на кандидате — не успех."""
    new, unknown, inherited = [], [], []
    for suite, rep in candidate.items():
        base_checks = (baseline.get(suite) or {}).get("checks", {})
        for check, status in rep.get("checks", {}).items():
            base = base_checks.get(check)
            tag = f"{suite}/{check}"
            if status in ("BLOCKED", "UNPROVEN") or (status == "ERROR" and base != "ERROR"):
                unknown.append(tag)
            elif status in BAD and base in BAD:
                inherited.append(tag)
            elif status in BAD:
                new.append(tag)
        if rep.get("verdict") in ("BLOCKED", "ERROR") and not rep.get("checks"):
            unknown.append(f"{suite}/*")
    return new, unknown, inherited


def evaluate(evidence: dict, baseline: dict, review: dict | None, context: dict) -> dict:
    """Вернуть {"verdict", "reasons", "informational", "review_requests_changes"}.

    context: forbidden_paths, tcb_paths, gate_weakening, production_mutations, plan_ack_required,
    touches_open_ubr, objective_resolution ("RESOLVED" | "NOT_DEMONSTRATED" | "NOT_APPLICABLE"),
    no_change (bool)."""
    hits: dict[str, list[str]] = {s: [] for s in PRIORITY}
    info: list[str] = []

    # --- UNSAFE: то, что нельзя разрешить никаким доказательством -------------
    if context.get("production_mutations", 0) > 0:
        hits["UNSAFE"].append(f"production-мутаций: {context['production_mutations']} (обязано быть 0)")
    if context.get("audit_status") == "BLOCKED":
        hits["INCONCLUSIVE"].append("аудит production-мутаций не получен (BLOCKED) — ноль не доказан")
    if context.get("forbidden_paths"):
        hits["UNSAFE"].append(f"затронуты запрещённые пути: {context['forbidden_paths'][:10]}")
    if context.get("gate_weakening"):
        g = context["gate_weakening"]
        hits["UNSAFE"].append(f"ослабление ворот в диффе: {len(g)} признак(ов), первый — "
                              f"{g[0]['file']}: {g[0]['text'][:120]}")
    if review and review.get("gate_weakening_detected"):
        hits["UNSAFE"].append("ревьюер обнаружил ослабление ворот")

    # --- тесты и статическая проверка ------------------------------------------
    # Тесты репозитория — строго: красный тест блокирует ВСЕГДА, даже если он красный и на
    # базовой ветке. Кандидат, оставивший тест красным, мог «не заметить» именно то, что
    # должен был починить. (Для проверок данных ниже логика иная: они отражают состояние
    # production, которое кандидат до развёртывания изменить не может.)
    base_tests = {b["name"]: b["status"] for b in baseline.get("tests", [])}
    for t in evidence.get("tests", []):
        if t["status"] in ("FAIL", "ERROR"):
            inherited = " (красный и на базовой ветке)" if base_tests.get(t["name"]) in ("FAIL", "ERROR") else ""
            hits["BLOCKED_BY_TEST"].append(f"тест {t['name']}: {t['status']}{inherited}")
        elif t["status"] != "PASS":
            hits["INCONCLUSIVE"].append(f"тест {t['name']}: {t['status']} (нет доказательства)")
    if not evidence.get("tests"):
        hits["INCONCLUSIVE"].append("ни один тест не выполнен")
    sv = evidence.get("sql_validation", {}).get("status", "NOT_APPLICABLE")
    if sv == "FAIL":
        hits["BLOCKED_BY_TEST"].append("validate_current_sql: FAIL")
    elif sv not in ("PASS", "NOT_APPLICABLE"):
        hits["INCONCLUSIVE"].append(f"validate_current_sql: {sv}")

    # --- runtime-доступ --------------------------------------------------------
    ra = evidence.get("runtime_access", {}).get("status", "NOT_APPLICABLE")
    if ra == "FAIL":
        hits["BLOCKED_BY_RUNTIME_ACCESS"].append("check_runtime_access: исполнитель не прочитает зависимость")
    elif ra not in ("PASS", "NOT_APPLICABLE"):
        hits["INCONCLUSIVE"].append(f"check_runtime_access: {ra}")

    # --- данные ----------------------------------------------------------------
    new, unknown, inherited = _regressions(evidence.get("data_suites", {}), baseline.get("data_suites", {}))
    if new:
        hits["BLOCKED_BY_DATA"].append(f"новые падения проверок данных: {new[:10]}")
    if unknown:
        hits["INCONCLUSIVE"].append(f"проверки данных без доказательства: {unknown[:10]}")
    if inherited:
        info.append(f"унаследованные падения (есть и на базовой ветке): {inherited[:10]}")
    par = evidence.get("parity", {}).get("status", "NOT_APPLICABLE")
    if par == "FAIL":
        hits["BLOCKED_BY_DATA"].append("Git↔production parity: неожиданный дрейф")
    elif par not in ("PASS", "NOT_APPLICABLE"):
        hits["INCONCLUSIVE"].append(f"Git↔production parity: {par}")

    # --- решения, принадлежащие человеку --------------------------------------
    # TCB: ворота, доказательства, CI, IAM, развёртывание. Автономный контур не одобряет
    # изменения собственной доверенной базы — ни ревьюер, ни ACK плана этого не снимают.
    if context.get("tcb_paths"):
        hits["HUMAN_DECISION_REQUIRED"].append(
            f"TCB_MODIFICATION: кандидат меняет доверенную базу {context['tcb_paths'][:10]} — "
            "AE такие изменения не публикует; решение и PR — только человек")
    if context.get("plan_ack_required"):
        hits["HUMAN_DECISION_REQUIRED"].append(f"нужен ACK плана: {context['plan_ack_required']}")
    if context.get("touches_open_ubr"):
        hits["HUMAN_DECISION_REQUIRED"].append(
            f"изменение касается открытого нерешённого правила {context['touches_open_ubr']}")
    res = context.get("objective_resolution", "NOT_APPLICABLE")
    if res == "NOT_DEMONSTRATED":
        hits["HUMAN_DECISION_REQUIRED"].append("устранение исходного сигнала не доказано доказательствами")
    if context.get("no_change"):
        hits["HUMAN_DECISION_REQUIRED"].append("кандидат не содержит изменений: вывод без PR требует решения человека")

    # --- ревьюер: только ужесточает ------------------------------------------
    requests_changes = False
    if review is None:
        hits["INCONCLUSIVE"].append("независимое ревью не проведено")
    elif review["verdict"] == "CHANGES_REQUIRED":
        requests_changes = True
        hits["INCONCLUSIVE"].append(f"ревьюер требует изменений: {len(review.get('findings', []))} находок")
    elif review["verdict"] == "HUMAN_DECISION_REQUIRED":
        hits["HUMAN_DECISION_REQUIRED"].append(f"ревьюер: {review.get('human_decision_reason') or 'решение человека'}")
    elif review["verdict"] == "BLOCKED":
        hits["INCONCLUSIVE"].append("ревьюер не смог провести ревью")
    elif review["verdict"] == "PASS" and any(f["severity"] in ("BLOCKER", "MAJOR")
                                             for f in review.get("findings", [])):
        requests_changes = True
        hits["INCONCLUSIVE"].append("ревьюер вернул PASS с находками BLOCKER/MAJOR — противоречие трактуется строго")

    verdict = next(s for s in PRIORITY if s == "READY_FOR_PR" or hits[s])
    return {
        "verdict": verdict,
        "reasons": {s: v for s, v in hits.items() if v},
        "informational": info,
        "review_requests_changes": requests_changes,
        "decided_by": "gatekeeper (deterministic)",
    }
