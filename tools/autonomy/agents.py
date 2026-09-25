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
import shutil
import subprocess
import tempfile
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

    def run(self, role: str, prompt: str, workdir: Path, schema: dict) -> AgentResult:
        assert role in ROLES, role
        if not shutil.which(self.binary):
            return AgentResult(role, None, 127, f"нет исполняемого файла {self.binary}", transient=False)
        if self.pre_invoke:
            pre = subprocess.run(self.pre_invoke, capture_output=True, text=True)
            if pre.returncode != 0:
                return AgentResult(role, None, pre.returncode, "не обновлён токен идентичности агента", transient=True)
        try:
            r = subprocess.run(self.command(role, schema), input=prompt, cwd=workdir, capture_output=True,
                               text=True, timeout=self.timeout_s,
                               env=scrubbed_env(extra=self.env_extra, keep=self.keep_env))
        except subprocess.TimeoutExpired:
            return AgentResult(role, None, 124, "превышено время агента", transient=True)
        tail = redact_tail(r.stdout or "", 2000) + redact_tail(r.stderr or "", 1000)
        try:
            doc = json.loads(r.stdout)
        except json.JSONDecodeError:
            markers = load_policy()["retry"]["transient_error_markers"]
            return AgentResult(role, None, r.returncode, "вывод агента не JSON",
                               transient=any(m in tail for m in markers), raw_tail=tail)
        usage = {k: doc.get(k) for k in ("total_cost_usd", "num_turns", "duration_ms", "usage", "session_id")
                 if k in doc}
        structured = doc.get("structured_output")
        if structured is None and isinstance(doc.get("result"), str):
            try:
                structured = json.loads(doc["result"])
            except json.JSONDecodeError:
                structured = None
        # Текст ошибки рантайма — в причину: «не JSON» или «ошибка» без текста не диагностируемы.
        err = (f"агент завершился с ошибкой: {redact_text(str(doc.get('result')))[:300]}" if doc.get("is_error")
               else _check(role, structured, schema))
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

    def _verified(self, name: str) -> Path:
        path = self.dir / name
        if not path.exists():
            raise AgentNotPermitted(f"нет недоверенного вывода {name} в {self.dir}")
        want = self.expect.get(name)
        if not want or want != sha256_file(path):
            raise IntegrityError(f"{name}: sha256 не совпал с объявленным производителем")
        return path

    def run(self, role, prompt, workdir, schema):
        try:
            structured = json.loads(self._verified(f"{role}.json").read_text(encoding="utf-8"))
            patch = self._verified(f"{role}.patch").read_text(encoding="utf-8") if role == "engineer_implement" else ""
        except AgentNotPermitted as e:
            # Недоверенный job не отдал вывод (например, сам отверг дифф с секретом): управляемый
            # отказ → BLOCKED, а не падение доверенного шага. Подмена (IntegrityError) — по-прежнему исключение.
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
                           usage={"adapter": self.name})
