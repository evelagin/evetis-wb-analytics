"""Адаптеры агентного рантайма. Граница миграции.

Оркестратор, гейткипер и машина состояний знают только протокол `AgentAdapter`:
«роль + промпт + рабочий каталог + схема ответа → структурированный результат».
Замена Claude Code CLI на claude-code-action, OpenHands Agent Server или другой ACP-агент —
это новый адаптер, а не переписывание слоя доказательств.

Вывод агента — всегда ЗАЯВЛЕНИЕ. Он валидируется по схеме (fail-closed), а факты
(дифф, результаты тестов) оркестратор вычисляет сам.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

from tools.autonomy.policy import load_policy
from tools.autonomy.redact import redact_tail, redact_text
from tools.autonomy.schema import validate

ROLES = {"engineer_plan", "engineer_implement", "reviewer"}
SCHEMA_OF_ROLE = {"engineer_plan": "engineer_report", "engineer_implement": "engineer_report",
                  "reviewer": "review_verdict"}
# Переменные, которые НИКОГДА не передаются агенту: права записи и чужие секреты.
# Имена с «_» на конце — префиксы. CLOUDSDK_AUTH_ покрывает CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE,
# которую экспортирует google-github-actions/auth и которую gcloud читает НЕЗАВИСИМО от CLOUDSDK_CONFIG
# (инцидент M6 2026-09-25: без неё «очищенное» окружение в CI сохраняло токен GCP).
SCRUB_ENV = ("GITHUB_TOKEN", "GH_TOKEN", "GOOGLE_APPLICATION_CREDENTIALS", "CLOUDSDK_AUTH_",
             "CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE", "GOOGLE_GHA_CREDS_PATH", "GOOGLE_CREDENTIALS",
             "GOOGLE_CLOUD_KEYFILE_JSON", "GOOGLE_OAUTH_ACCESS_TOKEN", "CLOUDSDK_AUTH_ACCESS_TOKEN_FILE",
             "BQ_TOKEN", "WB_", "OZON_", "EVETIS_", "TF_VAR_", "ACTIONS_RUNTIME_TOKEN",
             "ACTIONS_ID_TOKEN_REQUEST_TOKEN", "ACTIONS_ID_TOKEN_REQUEST_URL", "SSH_AUTH_SOCK",
             "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_IDENTITY_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN",
             # Файлы команд Actions: агенту незачем менять окружение, PATH, outputs и итог следующих шагов.
             "GITHUB_ENV", "GITHUB_PATH", "GITHUB_OUTPUT", "GITHUB_STATE", "GITHUB_STEP_SUMMARY")


@dataclass
class AgentResult:
    role: str
    structured: dict | None
    exit_code: int
    error: str | None = None
    transient: bool = False
    usage: dict = field(default_factory=dict)
    raw_tail: str = ""
    # Отредактированная диагностика вызова (tools/autonomy/diagnostics.py): у ClaudeCliAdapter — запись
    # одного вызова, у ReplayAdapter — проверенный документ job'а. У сценарных адаптеров — None.
    diagnostics: dict | None = None


class AgentAdapter(Protocol):
    name: str

    def run(self, role: str, prompt: str, workdir: Path, schema: dict) -> AgentResult: ...


def scrubbed_env(base: dict | None = None, extra: dict | None = None, keep: tuple[str, ...] = ()) -> dict:
    """Окружение агента без учётных данных записи.

    gcloud без каталога конфигурации, git без глобальной и системной конфигурации (а значит
    без credential helper), gh без каталога конфигурации: любая попытка bq/gcloud/git push/gh
    падает на аутентификации, а не зависит от того, послушается ли модель.

    ОГРАНИЧЕНИЕ, названное прямо: на машине оператора это снижает, но не устраняет риск —
    агент, исполняющий произвольный код через тест, может прочитать файлы под $HOME по
    абсолютному пути. Структурная гарантия «ноль мутаций» определена для эфемерного
    раннера GitHub, где учётных данных оператора нет физически (AE_V1_SECURITY.md §3).
    `keep` — явный перечень переменных, которые CI передаёт намеренно (read-only SA)."""
    env = dict(base if base is not None else os.environ)
    for k in list(env):
        if k in keep:
            continue
        if any(k == s or (s.endswith("_") and k.startswith(s)) for s in SCRUB_ENV):
            del env[k]
    empty = tempfile.mkdtemp(prefix="ae-empty-")
    empty_cfg = Path(empty) / "gitconfig"
    empty_cfg.write_text("")
    if "CLOUDSDK_CONFIG" not in keep:
        env["CLOUDSDK_CONFIG"] = tempfile.mkdtemp(prefix="ae-no-gcloud-")
    env.update({"GIT_CONFIG_GLOBAL": str(empty_cfg), "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
                "GH_CONFIG_DIR": tempfile.mkdtemp(prefix="ae-no-gh-"), "GIT_ASKPASS": "/bin/false"})
    env.update(extra or {})
    return env


def _check(role: str, structured, schema: dict) -> str | None:
    if structured is None:
        return "агент не вернул структурированный вывод"
    errs = validate(structured, schema)
    return ("вывод агента нарушает схему: " + "; ".join(errs[:5])) if errs else None


# Окружение КАЖДОГО процесса Claude Code (инженер и ревьюер — один адаптер, значит одно значение).
# 2.1.280 без него шлёт телеметрию напрямую на api.anthropic.com в обход ANTHROPIC_BASE_URL и грузит
# plugin telemetry@builtin; CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC — штатный и самый широкий
# выключатель (телеметрия, отчёты об ошибках, автообновление, feature flags, bootstrap). Обмен токена
# федерации, Messages API, инструменты и проверку домена WebFetch не трогает (офлайн-стенд, 2026-09-27).
# Применяется ПОСЛЕ окружения job'а и env_extra: снять его входом workflow или вызывающим кодом нельзя.
CLAUDE_RUNTIME_ENV = {"CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}


class ClaudeCliAdapter:
    """Claude Code в headless-режиме. Каждый вызов — новый процесс без сохранения сессии:
    независимость ревьюера обеспечена тем, что у процесса нет доступа к контексту автора."""

    name = "claude-code-cli"

    def __init__(self, binary: str = "claude", env_extra: dict | None = None, timeout_s: int = 3600,
                 keep_env: tuple[str, ...] = (), pre_invoke: list[str] | None = None):
        """keep_env — переменные, которые CI передаёт агенту намеренно (read-only SA, WIF Anthropic).
        pre_invoke — команда перед КАЖДЫМ вызовом: в CI обновляет одноразовый OIDC-токен
        Anthropic, иначе второй процесс получит jti_reused."""
        self.binary, self.env_extra, self.timeout_s = binary, env_extra or {}, timeout_s
        self.keep_env, self.pre_invoke = keep_env, pre_invoke

    @staticmethod
    def cli_schema(schema: dict) -> dict:
        """Схема для флага --json-schema: без аннотаций $schema/$id. Валидатор CLI не знает URI
        черновика 2020-12 и отвергает схему целиком. Смысл проверки аннотации не несут, а полная
        схема всё равно валидирует ответ на нашей стороне (_check)."""
        return {k: v for k, v in schema.items() if k not in ("$schema", "$id")}

    def command(self, role: str, schema: dict) -> list[str]:
        p = load_policy()
        b, m = p["budgets"], p["model"]
        cmd = [self.binary, "-p", "--output-format", "json", "--json-schema", json.dumps(self.cli_schema(schema)),
               "--no-session-persistence", "--restricted", "--strict-mcp-config",
               "--permission-mode", "dontAsk"]
        if role == "reviewer":
            cmd += ["--tools", ",".join(p["reviewer_tools"]["tools"]),
                    "--model", m["reviewer"], "--max-turns", str(b["reviewer_max_turns"]),
                    "--max-budget-usd", str(b["reviewer_max_budget_usd"])]
        else:
            tools = list(p["engineer_tools"]["tools"])
            if role == "engineer_plan":           # план — без права правки файлов
                tools = [t for t in tools if t not in ("Edit", "Write")]
            cmd += ["--tools", ",".join(tools),
                    "--allowedTools", " ".join(f"Bash({x})" for x in p["engineer_tools"]["allowed_bash"])
                    + (" Edit Write" if role == "engineer_implement" else "") + " Read Grep Glob WebSearch WebFetch",
                    "--disallowedTools", " ".join(f"Bash({x})" for x in p["engineer_tools"]["denied_bash"]),
                    "--model", m["engineer"], "--max-turns", str(b["engineer_max_turns"]),
                    "--max-budget-usd", str(b["engineer_max_budget_usd"])]
        return cmd

    def environment(self) -> dict:
        """Версии рантайма для диагностики (один раз на адаптер). Не вызов модели: только --version."""
        if getattr(self, "_env", None) is None:
            def probe(cmd, rx):
                try:
                    r = subprocess.run(cmd, capture_output=True, text=True, timeout=15, stdin=subprocess.DEVNULL,
                                       env={**os.environ, **CLAUDE_RUNTIME_ENV})
                    m = re.search(rx, r.stdout or "")
                    return m.group(0) if m else None
                except (OSError, subprocess.SubprocessError):
                    return None
            self._env = {"claude_code_version": probe([self.binary, "--version"], r"\b\d+\.\d+\.\d+\b")
                         if shutil.which(self.binary) else None,
                         "node_version": probe(["node", "--version"], r"v\d+\.\d+\.\d+") if shutil.which("node") else None}
        return self._env

    def run(self, role: str, prompt: str, workdir: Path, schema: dict) -> AgentResult:
        """Любой исход — AgentResult с диагностикой; исключение обёртки/подпроцесса не пробрасывается."""
        from tools.autonomy import diagnostics as D
        assert role in ROLES, role
        inv, t0 = D.new_invocation(role), time.monotonic()
        markers = load_policy()["retry"]["transient_error_markers"]
        try:
            res = self._invoke(role, prompt, workdir, schema, inv, markers)
        except Exception as e:           # noqa: BLE001 — сбой обёртки фиксируется, а не роняет job без следа
            inv.update(failure_stage="PROCESS_SPAWN" if not inv["process_started"] and isinstance(e, OSError)
                       else "WRAPPER_EXCEPTION", exception_type=type(e).__name__,
                       summary=redact_text(str(e))[:D.SUMMARY])
            res = AgentResult(role, None, 1, f"сбой обёртки агента: {type(e).__name__}", transient=False)
        inv["elapsed_ms"] = int((time.monotonic() - t0) * 1000)
        D.classify(inv, markers)
        res.diagnostics = inv
        return res

    def _invoke(self, role, prompt, workdir, schema, inv, markers) -> AgentResult:
        from tools.autonomy import diagnostics as D
        if not shutil.which(self.binary):
            inv.update(failure_stage="BINARY_MISSING", summary=f"нет исполняемого файла {self.binary}"[:D.SUMMARY])
            return AgentResult(role, None, 127, f"нет исполняемого файла {self.binary}", transient=False)
        if self.pre_invoke:
            pre = subprocess.run(self.pre_invoke, capture_output=True, text=True)
            inv["pre_invoke_succeeded"] = pre.returncode == 0
            if pre.returncode != 0:
                inv.update(failure_stage="PRE_INVOKE", exit_code=pre.returncode,
                           summary="не обновлён токен идентичности агента")
                D.set_tails(inv, "", pre.stderr)
                return AgentResult(role, None, pre.returncode, "не обновлён токен идентичности агента", transient=True)
        try:
            inv["process_started"] = True
            r = subprocess.run(self.command(role, schema), input=prompt, cwd=workdir, capture_output=True,
                               text=True, timeout=self.timeout_s,
                               env=scrubbed_env(extra={**self.env_extra, **CLAUDE_RUNTIME_ENV}, keep=self.keep_env))
        except subprocess.TimeoutExpired as e:
            out = e.stdout.decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
            err = e.stderr.decode(errors="replace") if isinstance(e.stderr, bytes) else (e.stderr or "")
            D.set_tails(inv, out, err)
            inv.update(failure_stage="TIMEOUT", summary=f"превышено время агента ({self.timeout_s} с)")
            return AgentResult(role, None, 124, "превышено время агента", transient=True)
        except OSError:
            inv["process_started"] = False
            raise
        inv["exit_code"] = max(-255, min(255, r.returncode))
        D.set_tails(inv, r.stdout, r.stderr)
        tail = redact_tail(r.stdout or "", 2000) + redact_tail(r.stderr or "", 1000)
        if not (r.stdout or "").strip():
            inv.update(output_format="empty", failure_stage="PROCESS_EXIT_NONZERO" if r.returncode else "OUTPUT_NOT_JSON",
                       summary="агент не вывел ничего")
            return AgentResult(role, None, r.returncode, "вывод агента пуст",
                               transient=any(m in tail for m in markers), raw_tail=tail)
        try:
            doc = json.loads(r.stdout)
            if not isinstance(doc, dict):
                raise json.JSONDecodeError("не объект", r.stdout, 0)
        except json.JSONDecodeError:
            inv.update(output_format="non_json", failure_stage="OUTPUT_NOT_JSON", summary="вывод агента не JSON")
            return AgentResult(role, None, r.returncode, "вывод агента не JSON",
                               transient=any(m in tail for m in markers), raw_tail=tail)
        inv["output_format"] = "json"
        D.parse_cli_json(inv, doc)
        usage = {k: doc.get(k) for k in ("total_cost_usd", "num_turns", "duration_ms", "usage", "session_id")
                 if k in doc}
        structured = doc.get("structured_output")
        if structured is None and isinstance(doc.get("result"), str):
            try:
                structured = json.loads(doc["result"])
            except json.JSONDecodeError:
                structured = None
        # Текст ошибки рантайма — в причину: «не JSON» или «ошибка» без текста не диагностируемы.
        if doc.get("is_error"):
            err = f"агент завершился с ошибкой: {redact_text(str(doc.get('result')))[:300]}"
            inv["failure_stage"] = "CLI_REPORTED_ERROR"
        else:
            err = _check(role, structured, schema)
            if err:
                inv.update(failure_stage="NO_STRUCTURED_OUTPUT" if structured is None else "SCHEMA_INVALID",
                           summary=redact_text(err)[:D.SUMMARY])
        return AgentResult(role, structured if err is None else None, r.returncode, err, usage=usage,
                           raw_tail=tail)


class ScriptedAdapter:
    """Детерминированный адаптер для приёмочных тестов оркестратора.

    Скрипт — список шагов на роль: {"respond": dict, "edit": callable(workdir)} или
    {"error": str, "transient": bool}. Проверяет не «ум» агента, а поведение системы вокруг
    него: изоляцию, бюджеты, возвраты на доработку, отказ ворот."""

    name = "scripted"

    def __init__(self, script: dict[str, list[dict]]):
        self.script = {k: list(v) for k, v in script.items()}
        self.calls: list[dict] = []

    def run(self, role: str, prompt: str, workdir: Path, schema: dict) -> AgentResult:
        self.calls.append({"role": role, "workdir": str(workdir), "prompt_chars": len(prompt),
                           "prompt": prompt})
        steps = self.script.get(role) or []
        if not steps:
            return AgentResult(role, None, 1, f"скрипт исчерпан для роли {role}")
        step = steps.pop(0) if len(steps) > 1 else steps[0]
        if "error" in step:
            return AgentResult(role, None, 1, step["error"], transient=step.get("transient", False))
        if "edit" in step:
            step["edit"](Path(workdir))
        structured = step["respond"](Path(workdir)) if callable(step["respond"]) else step["respond"]
        err = _check(role, structured, schema)
        return AgentResult(role, structured if err is None else None, 0 if err is None else 1, err,
                           usage={"total_cost_usd": 0.0, "num_turns": 1, "adapter": self.name})


AdapterFactory = Callable[[str], AgentAdapter]


class AgentNotPermitted(RuntimeError):
    """Детерминированный job попытался вызвать агента. Это дефект конфигурации, а не отказ."""


class NoAgentAdapter:
    """Ставится в детерминированных jobs (test, gate). Любой вызов — исключение до перехода
    состояния: прогон не портится, job падает громко."""

    name = "no-agent"

    def run(self, role, prompt, workdir, schema):
        raise AgentNotPermitted(f"роль {role} не может исполняться в детерминированном job")


def sha256_file(path: Path) -> str:
    import hashlib
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class IntegrityError(RuntimeError):
    """Недоверенный артефакт не совпал с хешем, объявленным job'ом-производителем."""


class ReplayAdapter:
    """Воспроизводит ответ агента, вычисленный в ОТДЕЛЬНОМ недоверенном job.

    Доверенный job не исполняет агента: он читает его вывод (`<role>.json`, для реализации —
    ещё `<role>.patch`), сверяет sha256 с тем, что объявил job-производитель через свои
    outputs (их другой job подделать не может), и только потом передаёт оркестратору.
    Патч применяется в песочницу доверенного job'а — дифф снова вычисляет git, а не агент."""

    name = "replay"

    def __init__(self, pending_dir: Path, expect: dict[str, str]):
        # Без объявленных хешей воспроизведение не проверяемо — это не «мягкий режим», а отказ.
        if expect is None:
            raise IntegrityError("ReplayAdapter без --expect: вывод недоверенного job'а не с чем сверить")
        self.dir, self.expect = Path(pending_dir), dict(expect)
        self._diag: dict[str, tuple[dict | None, str | None]] = {}
        self._run_id: str | None = None

    def bind_run(self, run_id: str) -> None:
        if self._run_id != run_id:
            self._run_id, self._diag = run_id, {}

    def diagnostics(self, job: str) -> tuple[dict | None, str | None]:
        """(проверенная диагностика job'а | None, причина отказа | None). Объявлена, но подменена или
        отсутствует — IntegrityError (как и любой вывод с объявленным хешем)."""
        from tools.autonomy import diagnostics as D
        if job not in self._diag:
            name = f"{job}_diagnostics.json"
            if name not in self.expect:
                self._diag[job] = (None, None)
            else:
                path = self.dir / name
                if not path.is_file():
                    raise IntegrityError(f"{name}: объявлен производителем, но отсутствует")
                if path.stat().st_size > D.MAX_BYTES:          # до хеширования: без чтения гигабайтов в память
                    self._diag[job] = (None, f"диагностика больше {D.MAX_BYTES} байт")
                    return self._diag[job]
                data = path.read_bytes()                       # одно чтение: хеш и разбор — одних и тех же байт
                import hashlib
                if hashlib.sha256(data).hexdigest() != self.expect.get(name):
                    raise IntegrityError(f"{name}: sha256 не совпал с объявленным производителем")
                try:
                    self._diag[job] = (D.verify_untrusted(data, job, run_id=self._run_id), None)
                except D.DiagnosticsRejected as e:
                    self._diag[job] = (None, str(e))
        return self._diag[job]

    def _verified(self, name: str) -> Path:
        path = self.dir / name
        if not path.exists():
            raise AgentNotPermitted(f"нет недоверенного вывода {name} в {self.dir}")
        want = self.expect.get(name)
        if not want or want != sha256_file(path):
            raise IntegrityError(f"{name}: sha256 не совпал с объявленным производителем")
        return path

    def run(self, role, prompt, workdir, schema):
        diag, rejected = self.diagnostics("reviewer" if role == "reviewer" else "engineer")
        try:
            structured = json.loads(self._verified(f"{role}.json").read_text(encoding="utf-8"))
            patch = self._verified(f"{role}.patch").read_text(encoding="utf-8") if role == "engineer_implement" else ""
        except AgentNotPermitted as e:
            # Недоверенный job не отдал вывод (например, сам отверг дифф с секретом): управляемый
            # отказ → BLOCKED/FAILED, а не падение доверенного шага. Подмена (IntegrityError) — исключение.
            if diag is not None:
                why = f"{diag['failure_stage']}/{diag['failure_class']}"
                detail = diag["scratch_reason"] or next((i["summary"] for i in reversed(diag["invocations"])
                                                         if i["role"] == role and i["summary"]), "")
                return AgentResult(role, None, 1, f"недоверенный вывод отсутствует; диагностика {why}: {detail}"[:600],
                                   diagnostics=diag)
            if rejected:
                return AgentResult(role, None, 1, f"недоверенный вывод отсутствует; диагностика отвергнута: {rejected}")
            return AgentResult(role, None, 1, f"недоверенный вывод отсутствует: {e}")
        if role == "engineer_implement":
            # Патч — ПОЛНЫЙ кандидат относительно базового коммита, поэтому песочница сначала
            # возвращается к базе (при FIXING в ней лежит прошлый кандидат).
            subprocess.run(["git", "reset", "-q", "--hard", "HEAD"], cwd=workdir, capture_output=True)
            subprocess.run(["git", "clean", "-qfdx"], cwd=workdir, capture_output=True)
            if patch.strip():
                r = subprocess.run(["git", "apply", "--whitespace=nowarn", "-"], cwd=workdir, input=patch,
                                   text=True, capture_output=True)
                if r.returncode != 0:
                    return AgentResult(role, None, 1, "патч недоверенного вывода не применяется: "
                                       + redact_tail(r.stderr, 300))
        err = _check(role, structured, schema)
        return AgentResult(role, structured if err is None else None, 0 if err is None else 1, err,
                           usage={"adapter": self.name}, diagnostics=diag)
