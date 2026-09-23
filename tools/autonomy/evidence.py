"""Песочница кандидата и сбор доказательств. Факты вычисляет система, а не агент.

Песочница — отдельный git worktree от базового коммита. Дифф кандидата берётся из git,
а не из отчёта агента: агент может заявить что угодно, но изменить вывод `git diff`
задним числом не может.

`RepoEvidenceRunner` запускает СУЩЕСТВУЮЩИЕ инструменты Platform Baseline внутри
песочницы. Без учётных данных чтения проверки данных возвращают BLOCKED — не PASS.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Protocol


def _git(cwd: Path, *args: str, check: bool = True, input: str | None = None) -> str:
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, input=input)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()[:400]}")
    return r.stdout


class Sandbox:
    """Изолированное рабочее дерево от базового коммита. Не видит рабочую копию оператора."""

    def __init__(self, repo: Path, base_sha: str, root: Path):
        self.repo, self.base_sha = Path(repo), base_sha
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path: Path | None = None

    def create(self, patch: str | None = None) -> Path:
        self.cleanup()
        self.path = Path(tempfile.mkdtemp(prefix="ws-", dir=self.root))
        self.path.rmdir()
        _git(self.repo, "worktree", "add", "--detach", str(self.path), self.base_sha)
        if patch:
            _git(self.path, "apply", "--whitespace=nowarn", "-", input=patch)
        return self.path

    def diff(self) -> str:
        assert self.path
        _git(self.path, "add", "-A")
        return _git(self.path, "diff", "--cached", "--binary", self.base_sha)

    def changed_files(self) -> list[str]:
        assert self.path
        _git(self.path, "add", "-A")
        return [f for f in _git(self.path, "diff", "--cached", "--name-only", self.base_sha).splitlines() if f]

    def cleanup(self) -> None:
        if self.path and self.path.exists():
            _git(self.repo, "worktree", "remove", "--force", str(self.path), check=False)
            shutil.rmtree(self.path, ignore_errors=True)
        _git(self.repo, "worktree", "prune", check=False)
        self.path = None


class EvidenceRunner(Protocol):
    def collect(self, workdir: Path, changed_files: list[str], objective: dict) -> dict: ...

    def impact(self, workdir: Path, files: list[str]) -> dict: ...


def _run(cmd: list[str], cwd: Path, timeout: int = 1800, env: dict | None = None) -> dict:
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return {"exit_code": 124, "tail": "timeout"}
    except FileNotFoundError as e:
        return {"exit_code": 127, "tail": str(e)}
    return {"exit_code": r.returncode, "tail": ((r.stdout or "") + (r.stderr or ""))[-1500:]}


class RepoEvidenceRunner:
    """Доказательства по репозиторию EVETIS через существующие инструменты.

    project/token_command отсутствуют → проверки данных, паритет и runtime-доступ
    возвращают BLOCKED. Это честное «доказательства нет», а не успех."""

    def __init__(self, project: str | None = None, token_command: str | None = None,
                 python: str | None = None):
        self.project, self.token_command = project, token_command
        self.python = python or sys.executable

    def impact(self, workdir: Path, files: list[str]) -> dict:
        if not files:
            return {"risk_tier": None, "required_contracts": {}, "affected_total": 0}
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "impact.json"
            r = _run([self.python, "tools/impact_analysis.py", "--files", *files, "--output", str(out)], workdir)
            if not out.exists():
                return {"risk_tier": "TIER0", "error": r["tail"][-300:], "required_contracts": {}}
            return json.loads(out.read_text(encoding="utf-8"))

    def _suite(self, workdir: Path, suite: str) -> dict:
        if not (self.project and self.token_command):
            return {"verdict": "BLOCKED", "checks": {}, "reason": "нет учётных данных чтения"}
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "r.json"
            _run([self.python, "tools/run_data_checks.py", "--suite", suite, "--project", self.project,
                  "--token-command", self.token_command, "--output", str(out)], workdir)
            if not out.exists():
                return {"verdict": "ERROR", "checks": {}}
            rep = json.loads(out.read_text(encoding="utf-8"))
        return {"verdict": rep["verdict"],
                "checks": {c["check_id"]: c["status"] for c in rep["checks"] if c.get("gate_relevant", True)}}

    def collect(self, workdir: Path, changed_files: list[str], objective: dict) -> dict:
        imp = self.impact(workdir, changed_files)
        tests = []
        t = _run([self.python, "-m", "pytest", "-q", "tools/tests"], workdir, timeout=3600)
        tests.append({"name": "pytest tools/tests", "status": "PASS" if t["exit_code"] == 0 else "FAIL",
                      "exit_code": t["exit_code"], "tail": t["tail"][-600:]})
        if any(f.startswith("pipelines/ozon/") for f in changed_files):
            t = _run([self.python, "-m", "pytest", "-q", "pipelines/ozon/tests"], workdir, timeout=1800)
            tests.append({"name": "pytest pipelines/ozon/tests", "status": "PASS" if t["exit_code"] == 0 else "FAIL",
                          "exit_code": t["exit_code"], "tail": t["tail"][-600:]})
        if any(f.startswith("cloud/") for f in changed_files):
            tests.append({"name": "cloud typecheck/lint/test", "status": "BLOCKED",
                          "tail": "cloud/ проверяется job'ом ci.yml; в AE v1 гейткипер его не исполняет"})
        sv = _run([self.python, "tools/validate_current_sql.py"], workdir)
        creds = bool(self.project and self.token_command)
        sql_files = [f for f in changed_files if f.endswith(".sql")]
        if sql_files:
            ra = (_run([self.python, "tools/check_runtime_access.py", "--project", self.project,
                        "--token-command", self.token_command, "--files", *sql_files], workdir)
                  if creds else {"exit_code": None})
            runtime = {"status": {0: "PASS", 1: "FAIL", None: "BLOCKED"}.get(ra["exit_code"], "ERROR")}
        else:
            runtime = {"status": "NOT_APPLICABLE"}
        if any(f.startswith("sql/current/") for f in changed_files):
            if creds:
                p = _run([self.python, "tools/verify_current_sql_live.py", "--project", self.project,
                          "--token-command", self.token_command], workdir)
                # PENDING (код 2) для изменённого канонического объекта ожидаем: он не развёрнут.
                parity = {"status": {0: "PASS", 2: "PASS", 1: "FAIL"}.get(p["exit_code"], "ERROR")}
            else:
                parity = {"status": "BLOCKED"}
        else:
            parity = {"status": "NOT_APPLICABLE"}
        suites = sorted(set(imp.get("required_contracts", {})) | set(objective.get("contract_suites", [])))
        data = {s: self._suite(workdir, s) for s in suites}
        trig = ((objective.get("incident") or {}).get("triggering_checks") or [{}])[0]
        resolution = "NOT_APPLICABLE"
        if trig.get("check_id"):
            st = data.get(trig["suite"], {}).get("checks", {}).get(trig["check_id"])
            resolution = "RESOLVED" if st == "PASS" else "NOT_DEMONSTRATED"
        return {
            "impact": {k: imp.get(k) for k in ("risk_tier", "affected_total", "required_contracts",
                                                "assets_without_contract", "flags", "downstream_assets")},
            "tests": tests,
            "sql_validation": {"status": "PASS" if sv["exit_code"] == 0 else "FAIL", "tail": sv["tail"][-400:]},
            "runtime_access": runtime, "parity": parity, "data_suites": data,
            "objective_resolution": resolution,
        }


class ReplayEvidenceRunner:
    """Доказательства кандидата, собранные в ОТДЕЛЬНОМ job, исполнявшем код кандидата.

    Анализ влияния (impact) — доверенный: считается инструментом репозитория main по списку
    файлов. Доказательства кандидата — недоверенный артефакт с проверкой sha256.

    ОГРАНИЧЕНИЕ, названное прямо: доказательства, полученные исполнением кода кандидата,
    корректны против ошибающегося кандидата, но не против злонамеренного — код теста может
    подделать собственный результат. Последний рубеж против злонамеренного кандидата —
    независимое ревью диффа и слияние только человеком (AE_V1_SECURITY.md §4)."""

    REQUIRED = ("tests", "sql_validation", "runtime_access", "parity", "data_suites", "objective_resolution")

    def __init__(self, pending_file: Path, expect_sha256: str | None, impact_runner):
        self.file, self.expect, self.impact_runner = Path(pending_file), expect_sha256, impact_runner

    def impact(self, workdir: Path, files: list[str]) -> dict:
        return self.impact_runner.impact(workdir, files)

    def collect(self, workdir: Path, changed_files: list[str], objective: dict) -> dict:
        from tools.autonomy.agents import IntegrityError, sha256_file
        if not self.file.exists():
            raise IntegrityError(f"нет недоверенных доказательств {self.file}")
        if self.expect is not None and sha256_file(self.file) != self.expect:
            raise IntegrityError("доказательства кандидата: sha256 не совпал с объявленным производителем")
        ev = json.loads(self.file.read_text(encoding="utf-8"))
        missing = [k for k in self.REQUIRED if k not in ev]
        if missing:
            raise IntegrityError(f"доказательства кандидата неполны: нет {missing}")
        declared = sorted(ev.get("changed_files", changed_files))
        if declared != sorted(changed_files):
            raise IntegrityError(f"доказательства собраны не по этому кандидату: {declared} != {sorted(changed_files)}")
        ev["impact"] = {k: v for k, v in self.impact_runner.impact(workdir, changed_files).items()
                        if k in ("risk_tier", "affected_total", "required_contracts", "assets_without_contract",
                                 "flags", "downstream_assets")}
        return ev
