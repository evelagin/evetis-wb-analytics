"""Гейткипер AE v1. Детерминированный. Ни одна модель не может отменить его вердикт.

Вход — доказательства, вычисленные системой (не агентом), и вердикт ревьюера.
Правило симметрии: вердикт модели может только УЖЕСТОЧИТЬ итог. PASS ревьюера не
снимает ни одного детерминированного запрета; находка ревьюера снимает готовность.

Неизвестность не превращается в успех: BLOCKED/ERROR там, где доказательство обязательно,
даёт INCONCLUSIVE, а не READY_FOR_PR.

AE-R1:
  * область класса задачи (allowlist путей) и лимит размера диффа — кандидат вне области или больше
    лимита не становится READY_FOR_PR; это исправимо инженером (`scope_fixable`);
  * доказательства тестов обязаны быть подтверждены НЕЗАВИСИМЫМ повторным прогоном (retest без учётных
    данных): `test_provenance` RECONCILED или TRUSTED_LOCAL; расхождение двух исполнений — UNSAFE;
  * вердикты ревьюера APPROVE / APPROVE_WITH_NITS / CHANGES_REQUIRED / BLOCK / UNPROVEN
    (+ HUMAN_DECISION_REQUIRED); устаревшие PASS → APPROVE, BLOCKED → UNPROVEN. Одобрение без
    подтверждённой ревьюером проверки тестов (`test_verification`) — не одобрение.
"""
from __future__ import annotations

PRIORITY = ["UNSAFE", "BLOCKED_BY_REVIEW", "BLOCKED_BY_TEST", "BLOCKED_BY_RUNTIME_ACCESS", "BLOCKED_BY_DATA",
            "HUMAN_DECISION_REQUIRED", "INCONCLUSIVE", "READY_FOR_PR"]
TRUSTED_TEST_PROVENANCE = {"RECONCILED", "TRUSTED_LOCAL"}
LEGACY_VERDICTS = {"PASS": "APPROVE", "BLOCKED": "UNPROVEN"}
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
    if context.get("evidence_disagreement"):
        d = context["evidence_disagreement"]
        hits["UNSAFE"].append(f"EVIDENCE_DISAGREEMENT: недоверенный job и независимый retest разошлись: {d[:5]}")

    # --- область класса задачи и размер диффа (AE-R1) ---------------------------
    scope_fixable = False
    if "scope_violations" in context:
        for v in context["scope_violations"]:
            hits["HUMAN_DECISION_REQUIRED"].append(v)
        # Вне allowlist / слишком большой дифф — исправимо инженером; отсутствие класса — нет.
        scope_fixable = bool(context["scope_violations"]) and context.get("scope_present", True)
    if "test_provenance" in context and context["test_provenance"] not in TRUSTED_TEST_PROVENANCE:
        hits["INCONCLUSIVE"].append(f"тесты не подтверждены независимым повторным прогоном "
                                    f"(provenance {context['test_provenance']})")

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
    rv = LEGACY_VERDICTS.get((review or {}).get("verdict"), (review or {}).get("verdict"))
    severities = {f["severity"] for f in (review or {}).get("findings", [])}
    if review is None:
        hits["INCONCLUSIVE"].append("независимое ревью не проведено")
    elif rv == "CHANGES_REQUIRED":
        requests_changes = True
        hits["INCONCLUSIVE"].append(f"ревьюер требует изменений: {len(review.get('findings', []))} находок")
    elif rv == "HUMAN_DECISION_REQUIRED":
        hits["HUMAN_DECISION_REQUIRED"].append(f"ревьюер: {review.get('human_decision_reason') or 'решение человека'}")
    elif rv == "BLOCK":
        hits["BLOCKED_BY_REVIEW"].append("ревьюер: BLOCK — кандидат не должен продвигаться "
                                         f"({len(review.get('findings', []))} находок)")
    elif rv == "UNPROVEN":
        hits["INCONCLUSIVE"].append("ревьюер: UNPROVEN — доказательств недостаточно для вывода")
    elif rv in ("APPROVE", "APPROVE_WITH_NITS"):
        tv = review.get("test_verification") or {}
        if severities & {"BLOCKER", "MAJOR"}:
            requests_changes = True
            hits["INCONCLUSIVE"].append(f"ревьюер вернул {rv} с находками BLOCKER/MAJOR — противоречие трактуется строго")
        if tv.get("status") == "INSUFFICIENT" or not tv:
            requests_changes = True
            hits["INCONCLUSIVE"].append("ревьюер не подтвердил, что тесты покрывают изменение (test_verification)")
        elif tv.get("status") == "NOT_APPLICABLE" and context.get("code_changed", True):
            requests_changes = True
            hits["INCONCLUSIVE"].append("test_verification NOT_APPLICABLE при изменении кода — противоречие")
        elif tv.get("status") == "VERIFIED":
            unknown = [t for t in tv.get("relevant_tests", []) if context.get("known_test_paths") is not None
                       and t.split("::")[0] not in context["known_test_paths"]]
            if not tv.get("relevant_tests") or unknown:
                requests_changes = True
                hits["INCONCLUSIVE"].append(f"ревьюер сослался на тесты, которых нет в кандидате/репозитории: "
                                            f"{unknown[:5] or 'пустой список'}")
        if rv == "APPROVE" and severities == {"MINOR"}:
            info.append("ревьюер: APPROVE с замечаниями MINOR — трактуется как APPROVE_WITH_NITS")
        if rv == "APPROVE_WITH_NITS" or severities == {"MINOR"}:
            info.append(f"ревьюер: несущественные замечания ({len(review.get('findings', []))})")
    else:
        hits["INCONCLUSIVE"].append(f"неизвестный вердикт ревьюера {rv!r}")

    verdict = next(s for s in PRIORITY if s == "READY_FOR_PR" or hits[s])
    return {
        "verdict": verdict,
        "reasons": {s: v for s, v in hits.items() if v},
        "informational": info,
        "review_requests_changes": requests_changes,
        "scope_fixable": scope_fixable,
        "review_verdict": rv,
        "decided_by": "gatekeeper (deterministic)",
    }
