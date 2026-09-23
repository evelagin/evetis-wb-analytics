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
    policy = policy or load_policy()
    globs = policy["forbidden_paths"]["globs"]
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
