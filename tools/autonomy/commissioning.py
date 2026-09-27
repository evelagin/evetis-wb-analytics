"""Вердикт ввода AE v1 в эксплуатацию (S9). Детерминированный, по фактам прогона.

Живой прогон синтетической цели (quality/autonomy/examples/objective.commissioning.json) считается
успешным, только если ВСЕ условия подтверждены данными, а не отчётом агента:

  real_engineer      — job инженера исполнялся ≥2 раз (план/реализация и исправление) и успешно;
                       авторитетное состояние воспроизвело ≥2 реализаций (usage replay)
  real_reviewer      — job ревьюера исполнялся ≥2 раз успешно, на ДРУГИХ раннерах, чем инженер
  iteration_proven   — был переход REVIEWING → FIXING по находке ревьюера, затем READY_FOR_PR
  deterministic_gate — gate.json вынесен гейткипером, вердикт READY_FOR_PR
  required_ci        — обязательные workflows прошли на опубликованном SHA (verification PASS)
  final_state        — READY_FOR_HUMAN_REVIEW
  zero_mutations     — production_mutations = 0 и аудит PASS (не BLOCKED)
  scope              — изменён ровно разрешённый тестовый файл, маркер итерации присутствует

jobs — объекты API Actions (`GET …/runs/{id}/jobs`) всех проходов autonomy-run по этому прогону.
"""
from __future__ import annotations

import re

from tools.autonomy.audit import zero_mutations_proven

ALLOWED_FILES = {"tools/tests/test_ae_commissioning_canary.py"}


def _jobs(jobs: list[dict], needle: str) -> list[dict]:
    return [j for j in jobs if needle in j.get("name", "")]


def assess(run: dict, gate: dict, patch: str, jobs: list[dict], marker: str) -> dict:
    eng = [j for j in _jobs(jobs, "engineer") if j.get("conclusion") == "success"]
    rev = [j for j in _jobs(jobs, "review") if j.get("conclusion") == "success"]
    replays = [u for u in run.get("usage", []) if u.get("role") == "engineer_implement"]
    trans = [(t["from"], t["to"]) for t in run.get("transitions", [])]
    files = sorted(set(re.findall(r"^diff --git a/(\S+) b/\S+$", patch, re.M)))
    checks = {
        "real_engineer": len(eng) >= 2 and len(replays) >= 2,
        "real_reviewer": len(rev) >= 2 and not ({j.get("runner_name") for j in rev}
                                                & {j.get("runner_name") for j in eng}),
        "iteration_proven": ("REVIEWING", "FIXING") in trans and ("REVIEWING", "READY_FOR_PR") in trans
                            and trans.index(("REVIEWING", "FIXING")) < trans.index(("REVIEWING", "READY_FOR_PR")),
        "deterministic_gate": gate.get("decided_by", "").startswith("gatekeeper") and gate.get("verdict") == "READY_FOR_PR",
        "required_ci": ((run.get("verification") or {}).get("result") or {}).get("status") == "PASS",
        "final_state": run.get("state") == "READY_FOR_HUMAN_REVIEW",
        "zero_mutations": zero_mutations_proven(run)[0],
        "scope": bool(files) and set(files) <= ALLOWED_FILES and marker in patch,
    }
    return {"status": "PASS" if all(checks.values()) else "FAIL", "checks": checks,
            "changed_files": files, "engineer_jobs": len(eng), "reviewer_jobs": len(rev)}
