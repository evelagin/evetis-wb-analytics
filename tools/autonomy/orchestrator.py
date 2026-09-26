"""Оркестратор AE v1: пошаговое исполнение машины состояний.

Каждый шаг читает то, что ему нужно, из долговременного хранилища и пишет туда результат.
Поэтому процесс можно убить на любом шаге: перезапуск прочитает запись прогона и
продолжит с последнего зафиксированного состояния, не полагаясь на память модели.

Бюджеты (итерации, циклы ревью, время, инфраструктурные повторы) исполняются здесь,
по `policy.json`. Семантический отказ не повторяется автоматически никогда.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from tools.autonomy import gatekeeper
from tools.autonomy.agents import AgentAdapter, AgentResult, ReplayAdapter, SCHEMA_OF_ROLE
from tools.autonomy.evidence import EvidenceRunner, Sandbox
from tools.autonomy.redact import RedactionError, diff_added_secrets, ensure_clean, redact_obj, redact_text, safe_dumps, safe_text
from tools.autonomy.policy import (detect_gate_weakening, forbidden_paths, load_policy, plan_requires_ack,
                                   tcb_globs, tcb_paths)
from tools.autonomy.schema import load_schema, require_valid
from tools.autonomy.state import PARKED, TERMINAL, StateStore, TransitionError

PROMPTS = Path(__file__).resolve().parent / "prompts"
PHASE_TEXT = {
    "PLAN": ("Исследуй задачу и составь план. Файлы НЕ правь (права правки в этой фазе нет). "
             "Установи первопричину доказательствами. В `intended_files` перечисли все файлы, "
             "которые собираешься изменить. Если исправление меняет бизнес-семантику или вывод "
             "требует решения владельца — `business_semantics_change`=true и статус NEEDS_HUMAN. "
             "Если план готов — статус PLAN_READY."),
    "IMPLEMENT": ("Реализуй утверждённый план в рабочем дереве. Добавь тест, который краснеет без "
                  "исправления. Прогони тесты сам. Статус CANDIDATE_READY, если кандидат готов; "
                  "NEEDS_HUMAN, если по ходу выяснилось, что нужен владелец; CANNOT_PROCEED — если "
                  "задачу нельзя решить в рамках ограничений."),
}


def _sha(doc) -> str:
    return hashlib.sha256(json.dumps(doc, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class Orchestrator:
    def __init__(self, store: StateStore, repo: Path, engineer: AgentAdapter, reviewer: AgentAdapter,
                 evidence: EvidenceRunner, sandbox_root: Path,
                 audit: Callable[[dict], dict | int] = lambda run: {"status": "NOT_APPLICABLE", "mutations": 0},
                 publisher=None, now: Callable[[], datetime] | None = None,
                 verifier: Callable[[dict], dict] | None = None, trusted_base_ref: str | None = None):
        if engineer is reviewer:
            # Один объект-адаптер на обе роли — это одна и та же «голова». Ревью не независимо.
            raise ValueError("инженер и ревьюер обязаны быть разными экземплярами адаптера")
        self.store, self.repo = store, Path(repo)
        self.engineer, self.reviewer, self.evidence = engineer, reviewer, evidence
        self.sandbox_root, self.audit, self.publisher = Path(sandbox_root), audit, publisher
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.policy = load_policy()
        # S8: решение по обязательным workflows опубликованного кандидата (verification.py).
        self.verifier = verifier
        # Базовый коммит прогона исполняется доверенным job'ом (доказательства базы). Поэтому он
        # обязан лежать в истории доверенной ветки: цель с чужим sha — это чужой код.
        self.trusted_base_ref = trusted_base_ref
        # Записи вызовов агента в ЭТОМ процессе (tools/autonomy/diagnostics.py) — для диагностики job'а.
        self.diagnostics: list[dict] = []

    # ------------------------------------------------------------ артефакты ---
    def _art(self, run: dict) -> Path:
        p = self.store.root / "artifacts" / run["run_id"]
        p.mkdir(parents=True, exist_ok=True)
        return p

    def _put(self, run: dict, name: str, doc) -> None:
        p = self._art(run) / name
        p.parent.mkdir(parents=True, exist_ok=True)
        if name.endswith(".patch"):
            # Дифф кандидата не редактируется (это изменило бы код): секретоподобное в ДОБАВЛЕННЫХ
            # строках отсеивается раньше (_implement → UNSAFE); здесь — последний рубеж, fail closed.
            if diff_added_secrets(doc):
                raise RedactionError("секретоподобное в добавленных строках диффа — запись отменена")
            p.write_text(doc, encoding="utf-8")
            return
        p.write_text(safe_text(doc) if isinstance(doc, str) else safe_dumps(doc, indent=2), encoding="utf-8")

    def _get(self, run: dict, name: str, default=None):
        p = self._art(run) / name
        if not p.exists():
            return default
        text = p.read_text(encoding="utf-8")
        return text if name.endswith((".patch", ".md")) else json.loads(text)

    # --------------------------------------------------------------- приём ---
    def submit(self, objective: dict) -> tuple[dict, bool]:
        require_valid(objective, "objective")
        self._require_trusted_base(objective["repository_sha"])
        if not objective["execute"]:
            raise TransitionError(f"{objective['objective_id']}: execute=false — цель подготовлена, "
                                  "но её запуск не разрешён")
        run, created = self.store.open_or_get(objective, objective["repository_sha"],
                                              self.policy["budgets"]["failed_cooldown_hours"])
        if created:
            self._put(run, "objective.json", objective)
        return run, created

    def _require_trusted_base(self, sha: str) -> None:
        if not self.trusted_base_ref:
            return
        import subprocess
        r = subprocess.run(["git", "merge-base", "--is-ancestor", sha, self.trusted_base_ref], cwd=self.repo,
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise TransitionError(f"repository_sha {sha[:12]} не входит в историю {self.trusted_base_ref}: "
                                  "доверенный job не исполняет код вне доверенной ветки")

    # -------------------------------------------------------------- бюджеты ---
    def _over_time(self, run: dict) -> bool:
        started = datetime.fromisoformat(run["created_at"].replace("Z", "+00:00"))
        return (self.now() - started).total_seconds() / 60 > self.policy["budgets"]["max_runtime_minutes"]

    def _agent(self, run: dict, adapter: AgentAdapter, role: str, prompt: str, workdir: Path
               ) -> tuple[dict, AgentResult | None]:
        """Вызов агента с инфраструктурными повторами. Семантический отказ не повторяется."""
        b = self.policy["budgets"]
        while True:
            if self._over_time(run):
                return run, None
            if isinstance(adapter, ReplayAdapter):
                adapter.bind_run(run["run_id"])        # диагностика чужого прогона отвергается при проверке
            res = adapter.run(role, prompt, workdir, load_schema(SCHEMA_OF_ROLE[role]))
            run = self._record_agent(run, adapter, role, res)
            a = self.audit(run)
            a = {"status": "PASS" if a == 0 else "FAIL", "mutations": a} if isinstance(a, int) else a
            # BLOCKED — доказательства нет; это НЕ ноль мутаций, гейткипер даст INCONCLUSIVE.
            run = {**run, "audit_status": a["status"]}
            if a.get("mutations"):
                run = {**run, "production_mutations": run["production_mutations"] + int(a["mutations"])}
            self.store.save(run)
            if res.structured is not None or not res.transient:
                return run, res
            if run["infra_retries"] >= b["max_infra_retries"]:
                return run, res
            run = {**run, "infra_retries": run["infra_retries"] + 1}
            self.store.save(run)

    def _record_agent(self, run: dict, adapter: AgentAdapter, role: str, res: AgentResult) -> dict:
        """Запись вызова в usage и отредактированная диагностика в artifacts/<run>/diagnostics/.

        Диагностика недоверенного job'а (ReplayAdapter) приходит уже проверенной (sha256, схема,
        секреты); здесь дополнительно сверяется run_id. Своя запись вызова — оборачивается в документ."""
        from tools.autonomy import diagnostics as D
        entry = {"role": role, "adapter": adapter.name, **res.usage}
        doc, d = None, res.diagnostics
        job = D.JOB_OF_ROLE[role]
        if d is None and not isinstance(adapter, ReplayAdapter):
            # Адаптер без своей диагностики (сценарный, будущий): минимальная запись по результату.
            d = D.new_invocation(role)
            if res.structured is None:
                d.update(failure_stage="OUTPUT_NOT_JSON" if res.transient else "NO_STRUCTURED_OUTPUT",
                         exit_code=max(-255, min(255, res.exit_code)), summary=redact_text(res.error or "")[:D.SUMMARY])
            D.classify(d, self.policy["retry"]["transient_error_markers"])
            res.diagnostics = d
        if d is not None and "invocations" in d:
            if d["run_id"] != run["run_id"]:
                res.diagnostics = None
                res.error = f"{res.error or ''}; диагностика отвергнута: run_id чужого прогона".lstrip("; ")
                entry["diagnostics"] = {"rejected": "run_id чужого прогона"}
            else:
                doc = d
        elif d is not None:
            inv = {**d, "attempt": min(1 + sum(1 for x in self.diagnostics if x["role"] == role), 12)}
            self.diagnostics.append(inv)
            env = adapter.environment() if hasattr(adapter, "environment") else {}
            doc = D.bundle(job, run["run_id"], [inv], env=env, scratch_state=run["state"])
        rejected = adapter.diagnostics(job)[1] if isinstance(adapter, ReplayAdapter) else None
        if rejected:
            entry["diagnostics"] = {"rejected": redact_text(rejected)[:200]}
        if doc is not None:
            text, doc = D.finalize(doc)
            seq = 1 + sum(1 for u in run["usage"] if u.get("role") == role)
            name = f"diagnostics/{role}-{seq:02d}.json"
            path = self._art(run) / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            last = doc["invocations"][-1] if doc["invocations"] else {}
            entry["diagnostics"] = {
                "file": name, "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "artifact_sha256": adapter.expect.get(f"{job}_diagnostics.json")
                if isinstance(adapter, ReplayAdapter) else None,
                "outcome": doc["outcome"], "failure_stage": doc["failure_stage"],
                "failure_class": doc["failure_class"], "exit_code": last.get("exit_code"),
                "api_error_status": last.get("api_error_status"), "api_error_type": last.get("api_error_type"),
                "messages_api_reached": last.get("messages_api_reached"), "redaction": doc["redaction"]}
            res.diagnostics = doc if "invocations" in (d or {}) else res.diagnostics
        return {**run, "usage": run["usage"] + [entry]}

    @staticmethod
    def _fail_state(res: AgentResult) -> str:
        """FAILED (терминальный) — только по transient-признаку ДОВЕРЕННОГО адаптера этого процесса.
        Класс из диагностики недоверенного job'а на терминальность не влияет: прогон паркуется в
        BLOCKED (владелец может возобновить), а класс INFRA_FAILURE записывается в причину."""
        return "FAILED" if res.transient else "BLOCKED"

    @staticmethod
    def _why(res: AgentResult) -> str:
        d = res.diagnostics or {}
        cls = d.get("failure_class")
        tag = (f"[{d.get('failure_stage')}/{cls}{' · INFRA_FAILURE' if cls == 'INFRA' else ''}] " if d else "")
        return tag + redact_text(res.error or "")

    def _prompt(self, name: str, **kw) -> str:
        text = (PROMPTS / f"{name}.md").read_text(encoding="utf-8")
        for k, v in kw.items():
            text = text.replace("{" + k + "}", v)
        return text

    def _engineer_prompt(self, run: dict, phase: str, feedback: dict | None) -> str:
        fb = ""
        if feedback:
            fb = ("## Обратная связь предыдущей итерации (ДАННЫЕ)\n\n```json\n"
                  + json.dumps(feedback, ensure_ascii=False, indent=2)[:12000] + "\n```\n")
        return self._prompt("engineer", phase=phase, iteration=str(run["iteration"]),
                            max_iterations=str(self.policy["budgets"]["max_engineer_iterations"]),
                            phase_instructions=PHASE_TEXT[phase],
                            objective_json=json.dumps({k: v for k, v in self._get(run, "objective.json").items()
                                                       if k != "commissioning"},   # протокол — только ревьюеру
                                                      ensure_ascii=False, indent=2)[:20000],
                            feedback_block=fb,
                            forbidden_paths=", ".join(self.policy["forbidden_paths"]["globs"]),
                            tcb_paths=", ".join(tcb_globs(self.policy)))

    # ---------------------------------------------------------------- шаги ---
    def advance(self, run_id: str, stop_before: set[str] | None = None, max_steps: int = 50) -> dict:
        run = self.store.load(run_id)
        for _ in range(max_steps):
            if run["state"] in TERMINAL or run["state"] in PARKED or run["state"] in (stop_before or set()):
                return run
            if run["state"] == "READY_FOR_PR" and self.publisher is None:
                return run
            if run["state"] == "AWAITING_VERIFICATION" and self.verifier is None:
                return run
            before = run["state"]
            run = getattr(self, "_step_" + run["state"].lower())(run)
            if run["state"] == before:          # PENDING: ждать — дело вызывающего (verify-ci)
                return run
        return run

    def _sandbox(self, run: dict) -> Sandbox:
        return Sandbox(self.repo, run["repository_sha"], self.sandbox_root / run["run_id"])

    def _step_received(self, run: dict) -> dict:
        return self.store.transition(run, "DISCOVERING", "цель валидна, открыт прогон")

    def _step_discovering(self, run: dict) -> dict:
        try:
            self._require_trusted_base(run["repository_sha"])
        except TransitionError as e:
            return self.store.transition(run, "BLOCKED", str(e)[:500])
        sb = self._sandbox(run)
        try:
            ws = sb.create()
            baseline = self.evidence.collect(ws, [], self._get(run, "objective.json"))
        finally:
            sb.cleanup()
        self._put(run, "baseline.json", baseline)
        return self.store.transition(run, "PLANNING", "собраны доказательства базового коммита")

    def _step_planning(self, run: dict) -> dict:
        objective = self._get(run, "objective.json")
        sb = self._sandbox(run)
        try:
            ws = sb.create()
            run, res = self._agent(run, self.engineer, "engineer_plan",
                                   self._engineer_prompt(run, "PLAN", None), ws)
            leaked = sb.changed_files()
        finally:
            sb.cleanup()
        if res is None:
            return self.store.transition(run, "BLOCKED", "исчерпан бюджет времени до плана")
        if res.structured is None:
            return self.store.transition(run, self._fail_state(res), f"план не получен: {self._why(res)}")
        # Хеш плана (для ACK владельца) — по той же, отредактированной форме, что сохраняется на диск.
        plan = redact_obj(res.structured)
        self._put(run, "plan.json", plan)
        if leaked:
            return self.store.transition(run, "BLOCKED", f"фаза плана изменила файлы, хотя права правки нет: {leaked[:5]}")
        if plan["status"] == "NEEDS_HUMAN":
            return self.store.transition(run, "WAITING_FOR_HUMAN", "инженер: нужен владелец — " + plan["summary"][:500])
        if plan["status"] == "CANNOT_PROCEED":
            return self.store.transition(run, "BLOCKED", "инженер: задачу нельзя решить — " + plan["summary"][:500])
        plan_sha = _sha(plan)
        tcb = tcb_paths(plan["intended_files"])
        if tcb:
            return self._human_tcb(run, tcb, "план затрагивает доверенную базу", plan_sha256=plan_sha)
        imp = self.evidence.impact(self.repo, plan["intended_files"])
        need, why = plan_requires_ack(plan["intended_files"], imp.get("risk_tier"),
                                      plan["business_semantics_change"], objective, plan_sha)
        if need:
            return self.store.transition(run, "WAITING_FOR_HUMAN",
                                         f"нужен ACK плана до реализации ({why}); для продолжения "
                                         f"владелец задаёт objective.owner_ack.plan_sha256={plan_sha}",
                                         plan_sha256=plan_sha)
        return self.store.transition(run, "IMPLEMENTING", f"план принят без ACK: {why}", plan_sha256=plan_sha)

    def _implement(self, run: dict, fixing: bool) -> dict:
        b = self.policy["budgets"]
        if run["iteration"] >= b["max_engineer_iterations"]:
            return self.store.transition(run, "BLOCKED", f"исчерпан бюджет итераций инженера ({b['max_engineer_iterations']})")
        run = {**run, "iteration": run["iteration"] + 1}
        self.store.save(run)
        objective = self._get(run, "objective.json")
        sb = self._sandbox(run)
        try:
            ws = sb.create(self._get(run, "candidate.patch") if fixing else None)
            run, res = self._agent(run, self.engineer, "engineer_implement",
                                   self._engineer_prompt(run, "IMPLEMENT",
                                                         self._get(run, "feedback.json") if fixing else None), ws)
            patch, files = sb.diff(), sb.changed_files()
        finally:
            sb.cleanup()
        if res is None:
            return self.store.transition(run, "BLOCKED", "исчерпан бюджет времени")
        if run["production_mutations"]:
            return self._unsafe(run, [], "зафиксированы production-мутации во время работы инженера")
        if res.structured is None:
            return self.store.transition(run, self._fail_state(res), f"кандидат не получен: {self._why(res)}")
        report = res.structured
        leaked_secrets = diff_added_secrets(patch)
        if leaked_secrets:
            # Секретоподобный материал в диффе — не решение человека, а инцидент; дифф не сохраняется.
            return self._unsafe(run, files, f"секретоподобный материал в диффе кандидата: {leaked_secrets}")
        self._put(run, "engineer_report.json", report)
        self._put(run, "candidate.patch", patch)
        bad = forbidden_paths(files)
        if bad:
            return self._unsafe(run, files, f"кандидат затронул секретный материал: {bad[:5]}")
        # TCB проверяется ДО ACK плана: owner_ack.plan_sha256 разрешает реализацию плана, но не
        # делает изменение доверенной базы самоодобряемым.
        tcb = tcb_paths(files)
        if tcb:
            return self._human_tcb(run, tcb, "кандидат меняет доверенную базу")
        if report["status"] == "NEEDS_HUMAN":
            return self.store.transition(run, "WAITING_FOR_HUMAN", "инженер: нужен владелец — " + report["summary"][:500])
        if report["status"] == "CANNOT_PROCEED":
            return self.store.transition(run, "BLOCKED", "инженер: нельзя решить — " + report["summary"][:500])
        imp = self.evidence.impact(self.repo, files)
        need, why = plan_requires_ack(files, imp.get("risk_tier"), report["business_semantics_change"],
                                      objective, run["plan_sha256"] or "")
        if need:
            return self.store.transition(run, "WAITING_FOR_HUMAN", f"фактический дифф требует ACK: {why}")
        return self.store.transition(run, "TESTING", f"кандидат: {len(files)} файл(ов), итерация {run['iteration']}")

    def _unsafe(self, run: dict, files: list[str], reason: str) -> dict:
        gate = gatekeeper.evaluate({}, {}, None, {"forbidden_paths": forbidden_paths(files),
                                                  "production_mutations": run["production_mutations"]})
        gate["verdict"] = "UNSAFE"
        gate.setdefault("reasons", {}).setdefault("UNSAFE", []).append(reason)
        self._put(run, "gate.json", gate)
        return self.store.transition(run, "BLOCKED", "UNSAFE: " + reason, last_gate={"verdict": "UNSAFE",
                                                                                      "reason": reason[:500]})

    def _human_tcb(self, run: dict, tcb: list[str], reason: str, **extra) -> dict:
        gate = gatekeeper.evaluate({}, {}, None, {"tcb_paths": tcb})
        gate["verdict"] = "HUMAN_DECISION_REQUIRED"   # минимум; секреты/мутации уже отсеяны выше
        self._put(run, "gate.json", gate)
        return self.store.transition(run, "WAITING_FOR_HUMAN", f"TCB_MODIFICATION: {reason}: {tcb[:5]}",
                                     last_gate={"verdict": "HUMAN_DECISION_REQUIRED",
                                                "reason": f"TCB_MODIFICATION {tcb[:5]}"[:500]}, **extra)

    def _step_implementing(self, run: dict) -> dict:
        return self._implement(run, fixing=False)

    def _step_fixing(self, run: dict) -> dict:
        return self._implement(run, fixing=True)

    def _step_testing(self, run: dict) -> dict:
        objective, patch = self._get(run, "objective.json"), self._get(run, "candidate.patch") or ""
        sb = self._sandbox(run)
        try:
            ws = sb.create(patch)
            files = sb.changed_files()
            ev = self.evidence.collect(ws, files, objective)
        finally:
            sb.cleanup()
        ev["changed_files"] = files
        ev["gate_weakening"] = detect_gate_weakening(patch, files)
        self._put(run, "evidence.json", ev)
        baseline = self._get(run, "baseline.json", {})
        failing = [t for t in ev["tests"] if t["status"] in ("FAIL", "ERROR")]
        if ev.get("sql_validation", {}).get("status") == "FAIL":
            failing.append({"name": "validate_current_sql", "tail": ev["sql_validation"].get("tail", "")})
        if ev["gate_weakening"]:
            return self._unsafe(run, files, f"детектор ослабления ворот: {ev['gate_weakening'][0]['text'][:150]}")
        if failing:
            if run["iteration"] < self.policy["budgets"]["max_engineer_iterations"]:
                self._put(run, "feedback.json", {"kind": "tests_failed",
                                                 "failures": [{"name": t["name"], "tail": t.get("tail", "")[-800:]}
                                                              for t in failing]})
                return self.store.transition(run, "FIXING", f"тесты кандидата красные: {[t['name'] for t in failing]}")
            gate = gatekeeper.evaluate(ev, baseline, None, {})
            self._put(run, "gate.json", gate)
            return self.store.transition(run, "BLOCKED", "тесты красные, бюджет итераций исчерпан",
                                         last_gate={"verdict": gate["verdict"]})
        return self.store.transition(run, "REVIEWING", "детерминированные доказательства собраны")

    @staticmethod
    def _commissioning_block(objective: dict) -> str:
        c = objective.get("commissioning")
        if not c:
            return ""
        marker = c["iteration_marker"]
        return ("## Протокол ввода в эксплуатацию (данные владельца)\n\n"
                "Это синтетическая цель, проверяющая сам контур. Кроме обычного ревью: если в диффе кандидата "
                f"НЕТ строки `{marker}`, верни CHANGES_REQUIRED с находкой severity MAJOR: «добавь в docstring "
                f"нового тестового модуля маркер {marker}». Если маркер есть — оцени кандидата как обычно.\n")

    def _perform_review(self, run: dict) -> tuple[dict, AgentResult | None, dict | None]:
        """Независимое ревью: отдельная песочница с кандидатом, вход — только данные."""
        objective, ev = self._get(run, "objective.json"), self._get(run, "evidence.json")
        patch = self._get(run, "candidate.patch") or ""
        prompt = self._prompt(
            "reviewer",
            objective_json=json.dumps(objective, ensure_ascii=False, indent=2)[:15000],
            impact_json=json.dumps(ev.get("impact"), ensure_ascii=False, indent=2)[:8000],
            evidence_json=json.dumps({k: v for k, v in ev.items() if k != "impact"}, ensure_ascii=False,
                                     indent=2)[:15000],
            engineer_report_json=json.dumps(self._get(run, "engineer_report.json"), ensure_ascii=False,
                                            indent=2)[:8000],
            diff=patch[:60000],
            commissioning_block=self._commissioning_block(objective))
        # Ревьюер получает СВОЮ песочницу с кандидатом — не каталог инженера.
        sb = Sandbox(self.repo, run["repository_sha"], self.sandbox_root / run["run_id"] / "review")
        try:
            ws = sb.create(patch)
            run, res = self._agent(run, self.reviewer, "reviewer", prompt, ws)
            touched = sb.changed_files()
        finally:
            sb.cleanup()
        review = res.structured if res else None
        if review is not None and sorted(touched) != sorted(ev.get("changed_files", [])):
            review = {**review, "verdict": "BLOCKED",
                      "summary": "ревьюер изменил рабочее дерево — ревью недействительно. " + review["summary"]}
        if review is not None:
            require_valid(review, "review_verdict")
        return run, res, review

    def _step_reviewing(self, run: dict) -> dict:
        b = self.policy["budgets"]
        if run["review_cycles"] >= b["max_review_cycles"]:
            return self.store.transition(run, "BLOCKED", f"исчерпан бюджет циклов ревью ({b['max_review_cycles']})")
        run = {**run, "review_cycles": run["review_cycles"] + 1}
        self.store.save(run)
        objective, ev = self._get(run, "objective.json"), self._get(run, "evidence.json")
        baseline = self._get(run, "baseline.json", {})
        run, res, review = self._perform_review(run)
        if res is None:
            return self.store.transition(run, "BLOCKED", "исчерпан бюджет времени на ревью")
        if review is None and res.structured is None and res.diagnostics is not None:
            # Отказ ревьюера с диагностикой — явная классификация, а не «ревью не проведено» у гейткипера.
            return self.store.transition(run, self._fail_state(res), f"ревью не получено: {self._why(res)}")
        if review is not None:
            self._put(run, "review.json", review)
        weakening = ev.get("gate_weakening", [])
        objective_ubr = set(objective.get("known_ubr_links", []))
        context = {
            "forbidden_paths": forbidden_paths(ev.get("changed_files", [])),
            "tcb_paths": tcb_paths(ev.get("changed_files", [])),
            "gate_weakening": weakening,
            "production_mutations": run["production_mutations"],
            "audit_status": run.get("audit_status", "NOT_APPLICABLE"),
            "objective_resolution": ev.get("objective_resolution", "NOT_APPLICABLE"),
            "no_change": not ev.get("changed_files"),
            "touches_open_ubr": sorted(objective_ubr) if objective_ubr and objective["kind"] == "INCIDENT" else None,
        }
        gate = gatekeeper.evaluate(ev, baseline, review, context)
        self._put(run, "gate.json", gate)
        summary = {"verdict": gate["verdict"], "cycle": run["review_cycles"],
                   "review": review["verdict"] if review else None}
        run = {**run, "last_gate": summary, "last_review": ({"verdict": review["verdict"],
                                                              "findings": len(review["findings"])} if review else None)}
        v = gate["verdict"]
        if v == "READY_FOR_PR":
            return self.store.transition(run, "READY_FOR_PR", "гейткипер: READY_FOR_PR")
        if v == "UNSAFE":
            return self.store.transition(run, "BLOCKED", "гейткипер: UNSAFE — " + "; ".join(gate["reasons"]["UNSAFE"])[:500])
        budget_left = (run["iteration"] < b["max_engineer_iterations"]
                       and run["review_cycles"] < b["max_review_cycles"])
        fixable = gate["review_requests_changes"] or v in ("BLOCKED_BY_TEST", "BLOCKED_BY_DATA",
                                                           "BLOCKED_BY_RUNTIME_ACCESS")
        if v == "HUMAN_DECISION_REQUIRED" and not gate["review_requests_changes"]:
            return self.store.transition(run, "WAITING_FOR_HUMAN", "гейткипер: " + "; ".join(
                gate["reasons"].get("HUMAN_DECISION_REQUIRED", []))[:500])
        if fixable and budget_left:
            self._put(run, "feedback.json", {"kind": "review_and_gate",
                                             "gate": gate, "review_findings": (review or {}).get("findings", [])})
            return self.store.transition(run, "FIXING", f"возврат инженеру: гейт {v}, ревью "
                                                        f"{review['verdict'] if review else '—'}")
        return self.store.transition(run, "BLOCKED", f"гейткипер: {v}; бюджет доработки "
                                                     f"{'исчерпан' if fixable else 'не применим'}")

    def _step_ready_for_pr(self, run: dict) -> dict:
        gate = self._get(run, "gate.json")
        if not gate or gate["verdict"] != "READY_FOR_PR":
            raise TransitionError("публикация без READY_FOR_PR невозможна")
        pub = self.publisher.publish(run, self._get(run, "candidate.patch") or "", self._art(run),
                                     self.policy["required_verification"]["workflows"])
        return self.store.transition(
            run, "AWAITING_VERIFICATION",
            f"опубликован draft PR {pub['url']}; обязательные workflows запущены на {pub['head_sha'][:12]}",
            pr_url=pub["url"], verification={"head_sha": pub["head_sha"], "dispatched_at": pub["dispatched_at"],
                                             "workflows": pub["workflows"], "result": None})

    def _step_awaiting_verification(self, run: dict) -> dict:
        """S8: READY_FOR_HUMAN_REVIEW — только по факту исполнения обязательных workflows."""
        from tools.autonomy.verification import timed_out
        rv = self.policy["required_verification"]
        result = self.verifier(run)
        self._put(run, "verification.json", result)
        run = {**run, "verification": {**run["verification"], "result": result}}
        if result["status"] == "PASS":
            return self.store.transition(run, "READY_FOR_HUMAN_REVIEW",
                                         "обязательные workflows прошли на опубликованном SHA: "
                                         + ", ".join(r["workflow"] for r in result["runs"]),
                                         last_gate={**(run.get("last_gate") or {}), "required_verification": "PASS"})
        if result["status"] == "FAIL":
            failed = [r["workflow"] for r in result["runs"] if r["status"] == "FAIL"]
            return self.store.transition(run, "BLOCKED", f"обязательные workflows упали: {failed}",
                                         last_gate={"verdict": "BLOCKED_BY_TEST",
                                                    "reason": f"required verification FAIL {failed}"})
        if timed_out(run, rv["timeout_minutes"], self.now()):
            return self.store.transition(run, "BLOCKED", f"обязательные workflows не завершились за "
                                                         f"{rv['timeout_minutes']} мин",
                                         last_gate={"verdict": "INCONCLUSIVE", "reason": "required verification timeout"})
        self.store.save(run)
        return run


def review_only(orch: Orchestrator, run_id: str, out_dir: Path | None = None,
                scope_guard: dict | None = None) -> dict[str, str]:
    """Для job'а ревьюера в CI: выполнить ревью и сохранить вердикт, НЕ меняя состояние.
    Переход делает job гейткипера на другой машине, воспроизводя вердикт (ReplayAdapter).
    При ЛЮБОМ исходе пишется reviewer_diagnostics.json; вердикт — только если он валиден."""
    from tools.autonomy import diagnostics as D
    from tools.autonomy.agents import sha256_file
    run = orch.store.load(run_id)
    if run["state"] != "REVIEWING":
        raise TransitionError(f"{run_id}: ревью возможно только в состоянии REVIEWING, а не {run['state']}")
    target = Path(out_dir) if out_dir else orch._art(run)
    target.mkdir(parents=True, exist_ok=True)
    review, res, exc = None, None, None
    try:
        run, res, review = orch._perform_review(run)
    except Exception as e:           # noqa: BLE001 — отказ фиксируется диагностикой, не теряется
        exc = type(e).__name__
    if review is not None:
        (target / "reviewer.json").write_text(safe_dumps(review, indent=2), encoding="utf-8")
    reason = "" if review is not None else (
        f"ревьюер не вернул валидный вердикт: {res.error}" if res is not None and res.error else
        ("исчерпан бюджет времени на ревью" if exc is None and res is None else ""))
    env = orch.reviewer.environment() if hasattr(orch.reviewer, "environment") else {}
    text, _ = D.finalize(D.bundle("reviewer", run_id, list(orch.diagnostics), scratch_state=run["state"],
                                  scratch_reason=reason, scope_guard=scope_guard, expected=["reviewer.json"],
                                  created=["reviewer.json"] if review is not None else [], env=env,
                                  wrapper_exception=exc))
    (target / "reviewer_diagnostics.json").write_text(text, encoding="utf-8")
    return {n: sha256_file(target / n) for n in ("reviewer.json", "reviewer_diagnostics.json")
            if (target / n).exists() and (n != "reviewer.json" or review is not None)}


def next_pass(store: StateStore, run_id: str) -> str:
    """Решение job'а persist после прохода autonomy-run. Детерминированное и тестируемое.

      dispatch       — состояние FIXING и потолок проходов не достигнут
      report         — прогон припаркован или завершён: итог владельцу
      infra_failure  — прогон остался в рабочем состоянии (в т.ч. READY_FOR_PR — публикация
                       не состоялась, AWAITING_VERIFICATION — ci-verify не дождался), значит job упал;
                       повторного диспатча НЕТ, иначе падающий job зациклил бы систему
      exhausted      — потолок проходов достигнут
    """
    run = store.load(run_id)
    limit = load_policy()["budgets"]["max_workflow_passes"]
    passes = run.get("passes", 0) + 1
    run = {**run, "passes": passes}
    store.save(run)
    if run["state"] in TERMINAL or run["state"] in PARKED:
        return "report"
    if run["state"] != "FIXING":
        return "infra_failure"
    return "dispatch" if passes < limit else "exhausted"


def agent_run(make_orchestrator: Callable[[StateStore], Orchestrator], store: StateStore, run_id: str,
              out_dir: Path, scope_guard: dict | None = None) -> dict[str, str]:
    """Недоверенный job инженера: исполнить агента на ЧЕРНОВОЙ копии состояния.

    Авторитетное хранилище не меняется. Наружу выходят только ответы агента (`<role>.json`,
    для реализации — полный патч кандидата относительно базы) и их sha256. Доверенный job
    воспроизводит их через ReplayAdapter, пересчитывая дифф и счётчики сам.

    engineer_diagnostics.json пишется при ЛЮБОМ исходе (в т.ч. исключении) и всегда входит в хеши:
    доверенный ingest классифицирует отказ, а не видит «пустой вывод» (инцидент M6 Phase 1)."""
    import shutil
    import tempfile
    from tools.autonomy import diagnostics as D
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    invs, env, scratch_state, reason, exc, start_state = [], {}, None, "", None, None
    try:
        start_state = store.load(run_id)["state"]
        with tempfile.TemporaryDirectory(prefix="ae-scratch-") as td:
            scratch = StateStore(Path(td) / "state")
            shutil.copytree(store.root, scratch.root, dirs_exist_ok=True)
            orch = make_orchestrator(scratch)
            try:
                before = len(scratch.load(run_id)["usage"])
                run = orch.advance(run_id, stop_before={"TESTING", "REVIEWING"})
                roles = [u["role"] for u in run["usage"][before:]]
                art = orch._art(run)
                if "engineer_plan" in roles and (art / "plan.json").exists():
                    shutil.copy(art / "plan.json", out_dir / "engineer_plan.json")
                if "engineer_implement" in roles and (art / "engineer_report.json").exists():
                    shutil.copy(art / "engineer_report.json", out_dir / "engineer_implement.json")
                    (out_dir / "engineer_implement.patch").write_text(orch._get(run, "candidate.patch") or "",
                                                                      encoding="utf-8")
                (out_dir / "usage.json").write_text(safe_dumps(run["usage"][before:]), encoding="utf-8")
                (out_dir / "scratch_state.json").write_text(json.dumps({"state": run["state"], "roles": roles},
                                                                       ensure_ascii=False), encoding="utf-8")
                scratch_state = run["state"]
                reason = run["transitions"][-1]["reason"] if run.get("transitions") else ""
            finally:
                invs = list(orch.diagnostics)
                env = orch.engineer.environment() if hasattr(orch.engineer, "environment") else {}
    except Exception as e:           # noqa: BLE001 — отказ фиксируется диагностикой, не теряется
        exc = type(e).__name__
    produced = sorted(p.name for p in out_dir.iterdir()
                      if p.name in ("engineer_plan.json", "engineer_implement.json", "engineer_implement.patch"))
    expected = sorted({a for i in invs for a in D.ARTIFACTS[i["role"]]}) or D.expected_for_state(start_state)
    text, _ = D.finalize(D.bundle("engineer", run_id, invs, scratch_state=scratch_state, scratch_reason=reason,
                                  scope_guard=scope_guard, expected=expected, created=produced, env=env,
                                  wrapper_exception=exc))
    (out_dir / "engineer_diagnostics.json").write_text(text, encoding="utf-8")
    from tools.autonomy.agents import sha256_file
    return {p.name: sha256_file(p) for p in sorted(out_dir.iterdir())
            if p.name.startswith("engineer_") or p.name == "reviewer.json"}


def collect_candidate_evidence(orch: Orchestrator, run_id: str, out_file: Path) -> str:
    """Недоверенный job тестов: исполнить ворота на коде кандидата. Состояние не меняется.
    Этот job исполняет код кандидата, поэтому у него нет ни токена записи, ни решений."""
    from tools.autonomy.agents import sha256_file
    run = orch.store.load(run_id)
    if run["state"] != "TESTING":
        raise TransitionError(f"{run_id}: доказательства собираются только в TESTING, а не {run['state']}")
    sb = orch._sandbox(run)
    try:
        ws = sb.create(orch._get(run, "candidate.patch") or "")
        files = sb.changed_files()
        ev = orch.evidence.collect(ws, files, orch._get(run, "objective.json"))
    finally:
        sb.cleanup()
    ev["changed_files"] = files
    Path(out_file).parent.mkdir(parents=True, exist_ok=True)
    Path(out_file).write_text(safe_dumps(ev, indent=2), encoding="utf-8")
    return sha256_file(Path(out_file))
