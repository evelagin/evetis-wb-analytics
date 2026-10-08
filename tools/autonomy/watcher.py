"""Наблюдатель AE. Детерминированный: ни одного вызова модели.

Отвечает на вопрос «нужен ли инженер», а не «здоров ли конвейер». С AE-R1 здоровье данных
наблюдатель НЕ вычисляет: живой источник — канонические сигналы DRO-1 и журнал отказов прогонов
(`tools/autonomy/signals.py`). Каждый сигнал до создания задачи проходит детерминированный
классификатор (`tools/autonomy/classifier.py`); задачу создают только инженерные классы.

Классы канонических сигналов (AE-R1):
  HEALTHY                конвейер здоров
  NOT_ENGINEERING        данные отсутствуют/опаздывают, внешний сбой без дефекта кода, ручной или
                         бизнесовый вход (ФФ, план продаж) — только владельцу, задачи нет никогда
  UNCLASSIFIED           ни одно правило не сработало — fail closed: оповещение, задачи нет
  OBSERVING              инженерный класс по DRO, ещё не устойчив — ждём подтверждения
  ENGINEERING_CANDIDATE  инженерный класс (журнал отказов — сразу; DRO — при устойчивости) — диспатч
  INFRA_BLOCKED          доказательство не получено или детектор устарел — это НЕ здоровье

Классы старого пути наблюдений проверок (фикстуры и синтетические стенды):
  HEALTHY          проверка проходит
  KNOWN            падает, но привязана к открытому UBR или вынесена из ворот с доказательством
  OBSERVING        упала впервые — ждём подтверждения, модель не зовём
  FLAPPING         чередует PASS и FAIL — это дефект проверки или гонка, а не поломка кода;
                   оповестить, но не диспатчить: автономный «ремонт» мигающего сигнала опасен
  NEW_PERSISTENT   падает подряд не меньше persistence_threshold раз и не известна — диспатч ТОЛЬКО
                   на синтетическом стенде; в живом наблюдении без канонической классификации —
                   UNCLASSIFIED (AE-R1: задачи без классификатора не бывает)
  INFRA_BLOCKED    доказательство не получено (нет доступа, ошибка запроса) — это НЕ здоровье
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

from tools.autonomy import classifier
from tools.autonomy.envelope import (build_incident, build_signal_incident, known_checks, objective_from_incident,
                                     objective_from_signal_incident, suites_index)
from tools.autonomy.policy import load_policy
from tools.autonomy.redact import redact_tail, safe_dumps
from tools.autonomy.state import StateStore, now_iso

REPO = Path(__file__).resolve().parent.parent.parent
FAILING = {"FAIL", "EMPTY", "UNHEALTHY", "DRIFT"}


# ------------------------------------------------------------ источники ---
def fixture_source(path: Path) -> list[dict]:
    return json.loads(Path(path).read_text(encoding="utf-8"))["observations"]


def canonical_source(project: str, token_command: str) -> list[dict]:
    """AE-R1: канонические сигналы DRO-1 и журнала отказов. Только SELECT от sa-ae-reader."""
    from tools.autonomy.signals import canonical_signals, live_fetcher
    return canonical_signals(live_fetcher(project, token_command), load_policy())


def live_source(project: str, token_command: str, suites: list[str],
                include_health: bool = True) -> list[dict]:
    """УСТАРЕВШИЙ путь (до AE-R1): прогнать ворота данных и превратить отчёты в наблюдения.

    Задачи по этим наблюдениям наблюдатель больше не создаёт (они не проходят классификатор):
    устойчивое падение — UNCLASSIFIED с оповещением. Сохранён для диагностики владельцем."""
    obs: list[dict] = []
    for suite in suites:
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "report.json"
            r = subprocess.run([sys.executable, "tools/run_data_checks.py", "--suite", suite,
                                "--project", project, "--token-command", token_command,
                                "--output", str(out)], cwd=REPO, capture_output=True, text=True,
                               timeout=1800)
            if not out.exists():
                obs.append({"key": f"suite:{suite}", "kind": "suite", "check_id": suite, "suite": suite,
                            "status": "BLOCKED", "detail": redact_tail(r.stderr or r.stdout or "", 300)})
                continue
            rep = json.loads(out.read_text(encoding="utf-8"))
        for c in rep["checks"]:
            obs.append({
                "key": f"check:{c['check_id']}", "kind": "check", "check_id": c["check_id"],
                "suite": suite, "status": c["status"], "source": f"{c['source']}:{c.get('line')}",
                "failing_rows": int(c.get("failing_rows") or 0),
                "excluded": bool(c.get("override")) or not c.get("gate_relevant", True),
            })
    if include_health:
        sys.path.insert(0, str(REPO / "tools"))
        from verify_task import stage_health  # noqa: E402 — существующий источник истины
        h = stage_health(project, token_command, "BQ_TOKEN")
        if h["status"] in ("ERROR", "BLOCKED"):
            obs.append({"key": "health:detector", "kind": "health", "check_id": "health:detector",
                        "suite": "health", "status": "BLOCKED"})
        else:
            for u in h.get("unhealthy", []):
                obs.append({"key": f"health:{u['scope']}", "kind": "health", "check_id": f"health:{u['scope']}",
                            "suite": "health", "status": "UNHEALTHY", "severity": "HIGH",
                            "source": u.get("reason")})
    return obs


# --------------------------------------------------------- классификация ---
def classify(obs: dict, history: list[dict], known: dict[str, str], policy: dict) -> str:
    w = policy["watch"]
    st = obs["status"]
    if st in ("PASS", "HEALTHY"):
        return "HEALTHY"
    if st in ("BLOCKED", "ERROR"):
        return "INFRA_BLOCKED"
    if obs["kind"] == "health":
        return "HEALTH_DEGRADED"
    if obs["check_id"] in known or obs.get("excluded"):
        return "KNOWN"
    statuses = [h["status"] for h in history[-w["history_window"]:]]
    failing = [s in FAILING for s in statuses]
    transitions = sum(1 for a, b in zip(failing, failing[1:]) if a != b)
    if transitions >= w["flapping_min_transitions"]:
        return "FLAPPING"
    streak = 0
    for f in reversed(failing):
        if not f:
            break
        streak += 1
    return "NEW_PERSISTENT" if streak >= w["persistence_threshold"] else "OBSERVING"


CANONICAL_KINDS = ("dro_health", "run_failure", "infra_blocked")
SIGNAL_STATUS = {"run_failure": "RUN_FAILED", "dro_health": "DEGRADED"}


def classify_signal(sig: dict, history: list[dict], policy: dict) -> tuple[str, dict | None]:
    """Канонический сигнал → (класс наблюдения, решение классификатора)."""
    if sig["kind"] == "infra_blocked":
        return "INFRA_BLOCKED", None
    decision = classifier.classify(sig, policy["signals"]["recurrence_threshold_7d"])
    if sig["kind"] == "dro_health" and sig.get("serving_status") == "HEALTHY":
        return "HEALTHY", decision
    if decision["task_class"] == "NOT_ENGINEERING":
        return "NOT_ENGINEERING", decision
    if not decision["engineering"]:
        return "UNCLASSIFIED", decision
    if sig["kind"] == "run_failure":
        # Отказ прогона — уже свершившийся факт с кодом и сигнатурой: ждать повторения не нужно.
        return "ENGINEERING_CANDIDATE", decision
    streak = 0
    for h in reversed(history[-policy["watch"]["history_window"]:]):
        if h["status"] in ("HEALTHY", "PASS"):
            break
        streak += 1
    return ("ENGINEERING_CANDIDATE" if streak >= policy["watch"]["persistence_threshold"] else "OBSERVING"), decision


def _signal_row(sig: dict) -> dict:
    """Публичная строка итога: только идентификаторы и перечисления."""
    keep = ("pipeline_id", "loader_name", "error_code", "failure_signature", "reason_code", "serving_status",
            "message_fingerprint", "occurrences_7d", "recovery_status")
    return {k: sig[k] for k in keep if k in sig}


# ----------------------------------------------------------------- прогон ---
def watch(observations: list[dict], store: StateStore, repository_sha: str, out_dir: Path,
          synthetic: bool = False) -> dict:
    policy = load_policy()
    known = {} if synthetic else known_checks()
    hist_path = store.root / "watch" / "history.json"
    hist_path.parent.mkdir(parents=True, exist_ok=True)
    history = json.loads(hist_path.read_text(encoding="utf-8")) if hist_path.exists() else {}
    at = now_iso()
    out_dir.mkdir(parents=True, exist_ok=True)

    results, dispatch, notify = [], [], []
    for o in observations:
        canonical = o.get("kind") in CANONICAL_KINDS
        entry = history.setdefault(o["key"], {"observations": [], "last_class": None})
        if canonical:
            st = ("BLOCKED" if o["kind"] == "infra_blocked" else
                  "HEALTHY" if o.get("serving_status") == "HEALTHY" else SIGNAL_STATUS[o["kind"]])
            entry["observations"] = (entry["observations"] + [{"at": at, "status": st}])[-policy["watch"]["history_window"]:]
            cls, decision = classify_signal(o, entry["observations"], policy)
            row = {"key": o["key"], "check_id": o["key"], "suite": o["kind"], "status": st, "class": cls,
                   **({"task_class": decision["task_class"], "rule": decision["rule"]} if decision else {}),
                   **_signal_row(o)}
        else:
            entry["observations"] = (entry["observations"] + [
                {"at": at, "status": o["status"], **({"failing_rows": o["failing_rows"]} if o.get("failing_rows") else {})}
            ])[-policy["watch"]["history_window"]:]
            cls, decision = classify(o, entry["observations"], known, policy), None
            if cls in ("NEW_PERSISTENT", "HEALTH_DEGRADED") and not synthetic:
                # AE-R1: без канонической классификации задачи не бывает — fail closed.
                cls = "UNCLASSIFIED"
            row = {"key": o["key"], "check_id": o["check_id"], "suite": o["suite"], "status": o["status"],
                   "class": cls}
        changed = cls != entry["last_class"]
        entry["last_class"] = cls
        row["changed"] = changed
        if cls == "KNOWN" and o.get("check_id") in known:
            row["ubr"] = known[o["check_id"]]
        if cls in policy["watch"]["dispatch_on"]:
            active = store.active_run(o["key"])
            if active:
                row["dispatch"] = f"already_active:{active['run_id']}"
            elif store.in_cooldown(o["key"], policy["budgets"]["failed_cooldown_hours"]):
                row["dispatch"] = "cooldown"
            else:
                if canonical:
                    incident = build_signal_incident(o, decision, entry["observations"], repository_sha)
                    objective = objective_from_signal_incident(incident, policy)
                else:
                    incident = build_incident(o, entry["observations"], repository_sha, cls, synthetic)
                    objective = objective_from_incident(incident)
                p = out_dir / f"{objective['objective_id']}.json"
                p.write_text(safe_dumps(objective, indent=2), encoding="utf-8")
                row["dispatch"] = str(p)
                dispatch.append({"objective_path": str(p), "deduplication_key": o["key"],
                                 "incident_id": incident["incident_id"]})
        if cls in policy["watch"]["notify_on"] and changed:
            notify.append(row)
        results.append(row)

    hist_path.write_text(safe_dumps(history, indent=2, sort_keys=True), encoding="utf-8")
    classes = {r["class"] for r in results}
    if dispatch:
        status = "ACTION"
    elif "INFRA_BLOCKED" in classes:
        status = "INFRA_BLOCKED"
    elif classes & {"OBSERVING", "FLAPPING", "NEW_PERSISTENT", "HEALTH_DEGRADED", "UNCLASSIFIED",
                    "ENGINEERING_CANDIDATE"}:
        status = "ATTENTION"
    else:
        status = "HEALTHY"
    return {
        "schema_version": 1, "watched_at": at, "repository_sha": repository_sha, "status": status,
        "llm_invocations": 0,
        "counts": {c: sum(1 for r in results if r["class"] == c) for c in sorted(classes)},
        "dispatch": dispatch, "notify": notify,
        "results": results,
    }
