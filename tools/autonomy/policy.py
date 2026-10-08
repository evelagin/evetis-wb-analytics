"""Детерминированная политика AE v1: что автономному кандидату запрещено.

Всё, что здесь решается, решается кодом по файлу `quality/autonomy/policy.json`.
Вердикт ревьюера-модели не может отменить ни один из этих запретов.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
POLICY = REPO / "quality" / "autonomy" / "policy.json"


@lru_cache(maxsize=1)
def load_policy() -> dict:
    p = json.loads(POLICY.read_text(encoding="utf-8"))
    if p.get("production_mutation_authority") != "NONE":
        # Политика, выдающая агенту право менять production, для AE v1 недопустима как таковая.
        raise RuntimeError("policy.json: production_mutation_authority обязан быть NONE")
    return p


def _glob_regex(pattern: str) -> re.Pattern:
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out += "(?:.*/)?"; i += 3
        elif pattern.startswith("**", i):
            out += ".*"; i += 2
        elif pattern[i] == "*":
            out += "[^/]*"; i += 1
        elif pattern[i] == "?":
            out += "[^/]"; i += 1
        else:
            out += re.escape(pattern[i]); i += 1
    return re.compile(f"^{out}$")


def glob_match(path: str, pattern: str) -> bool:
    # Только префикс "./". НЕ lstrip("./"): тот снимает ведущую точку и превращает
    # ".github/..." в "github/...", после чего запрет на workflow-файлы молча не срабатывает.
    return bool(_glob_regex(pattern).match(path.removeprefix("./")))


def forbidden_paths(files: list[str], policy: dict | None = None) -> list[str]:
    """Секретный материал в диффе → UNSAFE."""
    policy = policy or load_policy()
    globs = policy["forbidden_paths"]["globs"]
    return sorted(f for f in files if any(glob_match(f, g) for g in globs))


def tcb_globs(policy: dict | None = None) -> list[str]:
    policy = policy or load_policy()
    return [g for globs in policy["trusted_computing_base"]["classes"].values() for g in globs]


def tcb_paths(files: list[str], policy: dict | None = None) -> list[str]:
    """Пути доверенной вычислительной базы → минимум HUMAN_DECISION_REQUIRED, публикации нет.

    Проверка идёт по ОБОИМ именам переименования и по любому файлу диффа: переименование
    `.github/workflows/x.yml → docs/x.yml` тоже меняет TCB."""
    globs = tcb_globs(policy)
    return sorted(f for f in files if any(glob_match(f, g) for g in globs))


def detect_gate_weakening(diff_text: str, files: list[str], policy: dict | None = None) -> list[dict]:
    """Признаки ослабления ворот в унифицированном диффе.

    Это не эвристика «на всякий случай», а список конкретных механизмов, которыми
    ворота выключаются: удалить `@check`, пометить тест skip, объявить проверку
    known_failing, сделать набор не-воротами. Каждое совпадение — находка с номером строки.
    """
    policy = policy or load_policy()
    gw = policy["gate_weakening"]
    removed = [re.compile(p) for p in gw["removed_line_patterns"]]
    added = [re.compile(p) for p in gw["added_line_patterns"]]
    findings: list[dict] = []
    current = None
    for n, line in enumerate(diff_text.splitlines(), 1):
        if line.startswith("+++ "):
            current = line[4:].removeprefix("b/")
            continue
        if line.startswith("--- "):
            continue
        for rx in removed:
            if line.startswith("-") and rx.search(line):
                findings.append({"file": current, "line": n, "kind": "removed", "pattern": rx.pattern,
                                 "text": line[:200]})
        for rx in added:
            if line.startswith("+") and rx.search(line):
                findings.append({"file": current, "line": n, "kind": "added", "pattern": rx.pattern,
                                 "text": line[:200]})
    for f in files:
        if f in gw["protected_files"]:
            findings.append({"file": f, "line": 0, "kind": "protected_file", "pattern": "protected_files",
                             "text": "правка файла, определяющего сами ворота"})
    return findings


def plan_requires_ack(intended_files: list[str], risk_tier: str | None, declares_semantics: bool,
                      objective: dict, plan_sha256: str, policy: dict | None = None) -> tuple[bool, str]:
    """AI_ENGINEERING.md §3: ACK до реализации для экономически значимых изменений.

    ACK считается данным только если объект цели несёт sha256 ИМЕННО этого плана —
    устное «одобряю» не проверяемо, а хеш проверяем."""
    policy = policy or load_policy()
    pa = policy["plan_ack"]
    reasons = []
    if risk_tier in pa["requires_ack_risk_tiers"]:
        reasons.append(f"риск-тир {risk_tier}")
    hits = [f for f in intended_files if any(glob_match(f, g) for g in pa["requires_ack_path_globs"])]
    if hits:
        reasons.append(f"план правит экономически значимые пути: {hits[:5]}")
    if declares_semantics and pa["requires_ack_if_plan_declares_business_semantics"]:
        reasons.append("план заявляет изменение бизнес-семантики")
    if not reasons:
        return False, "ACK не требуется"
    ack = objective.get("owner_ack")
    if ack and ack.get("plan_sha256") == plan_sha256:
        return False, f"ACK владельца совпал с sha256 плана ({'; '.join(reasons)})"
    return True, "; ".join(reasons)


def branch_allowed(branch: str, policy: dict | None = None) -> bool:
    policy = policy or load_policy()
    b = policy["branches"]
    return bool(re.fullmatch(b["slug_pattern"], branch)) and branch not in b["protected"]


# ------------------------------------------------- классы задач (AE-R1) ---
def task_class_of(objective: dict) -> str | None:
    """Класс задачи цели. Явный `task_class`; иначе выводится только для двух известных форм:
    цель ввода в эксплуатацию (commissioning) и синтетический стенд. Иначе — None (области нет)."""
    if objective.get("task_class"):
        return objective["task_class"]
    # Синтетический стенд — первым: его область (synthetic/**) в настоящем репозитории пуста.
    if ((objective.get("incident") or {}).get("source")) == "synthetic":
        return "SYNTHETIC_FIXTURE"
    if objective.get("commissioning"):
        return "COMMISSIONING_CANARY"
    return None


def task_scope(objective: dict, policy: dict | None = None) -> dict | None:
    """Область кандидата по классу задачи из policy.json — НЕ из цели: автор цели не расширяет allowlist.

    None — класса нет, класс не инженерный или синтетический класс вне синтетического стенда.
    Fail closed: без области кандидат не может стать READY_FOR_PR."""
    policy = policy or load_policy()
    tc = task_class_of(objective)
    cls = policy["task_classes"]["classes"].get(tc or "")
    if not cls or not cls.get("engineering") or not cls.get("allowed_paths"):
        return None
    if tc == "SYNTHETIC_FIXTURE" and ((objective.get("incident") or {}).get("source")) != "synthetic":
        return None
    lim = policy["diff_limits"]
    return {"task_class": tc, "allowed_paths": list(cls["allowed_paths"]),
            "max_changed_lines": min(int(cls["max_changed_lines"]), int(lim["max_changed_lines"])),
            "max_files": min(int(cls["max_files"]), int(lim["max_files"])),
            "test_profiles": list(cls.get("test_profiles", []))}


def out_of_scope(files: list[str], scope: dict | None) -> list[str]:
    if scope is None:
        return sorted(files)
    return sorted(f for f in files if not any(glob_match(f, g) for g in scope["allowed_paths"]))


def diff_size(patch: str) -> dict:
    """Изменённые строки (+ и −) ТОЛЬКО внутри ханков `@@` и файлы унифицированного диффа.

    Заголовки `---`/`+++` бывают лишь до первого `@@` файла: удалённая строка-комментарий SQL
    («-- …» → «--- …» в диффе) внутри ханка считается, а не принимается за заголовок."""
    lines = files = binary = 0
    in_hunk = False
    for line in (patch or "").splitlines():
        if line.startswith("diff --git "):
            files += 1
            in_hunk = False
        elif line.startswith("@@"):
            in_hunk = True
        elif line.startswith(("GIT binary patch", "Binary files ")):
            binary += 1
        elif in_hunk and line.startswith(("+", "-")):
            lines += 1
    return {"changed_lines": lines, "files": files, "binary": binary}


SAFE_PATH = re.compile(r"^[A-Za-z0-9._/-]+$")
UNPARSED = "<unparsed-path>"


def _added_by_file(patch: str) -> dict[str, list[str]]:
    """Добавленные строки по файлам. Заголовок, который не разбирается (пробел, кавычки, не-ASCII в
    пути), — НЕ повод пропустить строки: они попадают под `<unparsed-path>` и проверяются строже всего."""
    out: dict[str, list[str]] = {}
    current, in_hunk = None, False
    for line in (patch or "").splitlines():
        if line.startswith("diff --git "):
            m = re.match(r"^diff --git a/(\S+) b/(\S+)$", line)
            current, in_hunk = (m.group(2) if m else UNPARSED), False
        elif line.startswith("@@"):
            in_hunk = True
        elif in_hunk and current and line.startswith("+"):
            out.setdefault(current, []).append(line[1:])
    return out


def patch_data_findings(patch: str, policy: dict | None = None) -> list[str]:
    """Политика вывода B3 для КОДА кандидата: данные в добавленных строках и бинарные изменения."""
    from tools.autonomy.output_policy import DATA_PATTERNS
    policy = policy or load_policy()
    scan = policy["output_policy"]["patch_data_scan"]
    findings: list[str] = []
    if diff_size(patch)["binary"]:
        findings.append("BINARY_CHANGE: бинарные изменения кандидату запрещены")
    rx = dict(DATA_PATTERNS)
    for f, added in _added_by_file(patch).items():
        if f == UNPARSED:
            findings.append("PATCH_PATH_UNPARSEABLE: путь файла в диффе не разбирается — допустимы только [A-Za-z0-9._/-]")
        data_file = f == UNPARSED or any(glob_match(f, g) for g in scan["data_file_globs"])
        kinds = scan["data_file_kinds"] if data_file else scan["all_files_kinds"]
        text = "\n".join(added)
        hit = sorted(k for k in kinds if rx[k].search(text))
        if hit:
            findings.append(f"PATCH_DATA ({f}): {hit}")
    return findings


def scope_violations(files: list[str], patch: str, scope: dict | None) -> list[str]:
    """Нарушения области: класс отсутствует, файлы вне allowlist, дифф больше лимита."""
    if scope is None:
        return ["класс задачи не определён или не инженерный — allowlist отсутствует (fail closed)"]
    out = []
    odd = [f for f in files if not SAFE_PATH.match(f)]
    if odd:
        out.append(f"PATH_NOT_ALLOWED: пути вне [A-Za-z0-9._/-] {odd[:5]}")
    oos = out_of_scope(files, scope)
    if oos:
        out.append(f"SCOPE_OUT_OF_ALLOWLIST ({scope['task_class']}): {oos[:10]}")
    size = diff_size(patch)
    if size["changed_lines"] > scope["max_changed_lines"]:
        out.append(f"DIFF_TOO_LARGE: {size['changed_lines']} изменённых строк > {scope['max_changed_lines']}")
    if max(size["files"], len(files)) > scope["max_files"]:
        out.append(f"DIFF_TOO_LARGE: {max(size['files'], len(files))} файлов > {scope['max_files']}")
    if size["binary"]:
        out.append(f"BINARY_CHANGE: {size['binary']} бинарных изменений — вне области любого класса")
    return out


def normalize_verdict(verdict: str, policy: dict | None = None) -> str:
    policy = policy or load_policy()
    return policy["reviewer_verdicts"]["legacy_aliases"].get(verdict, verdict)
