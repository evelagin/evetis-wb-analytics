"""Песочница кандидата и сбор доказательств. Факты вычисляет система, а не агент.

Песочница — отдельный git worktree от базового коммита. Дифф кандидата берётся из git,
а не из отчёта агента: агент может заявить что угодно, но изменить вывод `git diff`
задним числом не может.

`RepoEvidenceRunner` запускает СУЩЕСТВУЮЩИЕ инструменты Platform Baseline внутри
песочницы. Без учётных данных чтения проверки данных возвращают BLOCKED — не PASS.

AE-R1:
  * тесты кандидата — по профилям `policy.json` (`test_profiles`): python, ozon, cloud (npm ci
    без lifecycle-скриптов, typecheck, lint, vitest). Статус теста = код выхода И JUnit-отчёт, путь
    которому задаёт харнесс main: `os._exit(0)` без отчёта или пустой отчёт — FAIL, а не PASS.
    Сам отчёт пишет процесс тестов, т.е. код кандидата может его подделать — это ограничение,
    а не гарантия (последние рубежи — ревьюер, обязательный CI и слияние человеком);
  * на время тестов сеть отключается (`unshare`), где это возможно; иначе в доказательстве
    честно записано `network_isolation = NOT_ENFORCED`;
  * `RetestRunner` — то же самое в job'е БЕЗ учётных данных, на базе и на кандидате (число тестов
    не может уменьшиться). `ReplayEvidenceRunner` сверяет его с недоверенным job'ом: тесты берутся
    из retest, расхождение — `evidence_disagreement` (UNSAFE у гейткипера), retest нет —
    `test_provenance = UNTRUSTED_ONLY` (не готово);
  * хвосты вывода в доказательствах — только идентификаторы упавших тестов и отпечаток
    (политика вывода B3).
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

import xml.etree.ElementTree as ET

from tools.autonomy.output_policy import mask_data, public_tail
from tools.autonomy.redact import redact_tail, redact_text


def _git(cwd: Path, *args: str, check: bool = True, input: str | None = None) -> str:
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, input=input)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {redact_text(r.stderr.strip()[:400])}")
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
        # --no-renames: переименование — это удаление старого пути плюс новый путь. Иначе
        # `.github/workflows/x.yml → docs/x.yml` выглядело бы как правка одного docs/x.yml.
        return _git(self.path, "diff", "--cached", "--binary", "--no-renames", self.base_sha)

    def changed_files(self) -> list[str]:
        assert self.path
        _git(self.path, "add", "-A")
        return [f for f in _git(self.path, "-c", "core.quotepath=false", "diff", "--cached", "--name-only",
                                "--no-renames", self.base_sha).splitlines() if f]

    def cleanup(self) -> None:
        if self.path and self.path.exists():
            _git(self.repo, "worktree", "remove", "--force", str(self.path), check=False)
            shutil.rmtree(self.path, ignore_errors=True)
        _git(self.repo, "worktree", "prune", check=False)
        self.path = None


class EvidenceRunner(Protocol):
    def collect(self, workdir: Path, changed_files: list[str], objective: dict) -> dict: ...

    def impact(self, workdir: Path, files: list[str]) -> dict: ...


# Переменные, которые инструментам доказательств не нужны и не должны достаться коду, который
# они исполняют (pytest исполняет conftest/тесты рабочего дерева): токены GitHub, выпуск
# OIDC-токенов, runtime-токен Actions (кэш и артефакты), идентификация Claude API.
EVIDENCE_ENV_DENY_PREFIXES = ("ACTIONS_", "GITHUB_TOKEN", "GH_", "ANTHROPIC_", "CLAUDE_")
EVIDENCE_ENV_DENY = {"GH_TOKEN", "GITHUB_TOKEN", "ACTIONS_ID_TOKEN_REQUEST_TOKEN", "ACTIONS_ID_TOKEN_REQUEST_URL",
                     "ACTIONS_RUNTIME_TOKEN", "ACTIONS_CACHE_URL", "ACTIONS_RESULTS_URL"}


def evidence_env(base: dict | None = None) -> dict:
    base = dict(os.environ if base is None else base)
    return {k: v for k, v in base.items()
            if k not in EVIDENCE_ENV_DENY and not k.startswith(EVIDENCE_ENV_DENY_PREFIXES)}


def _run(cmd: list[str], cwd: Path, timeout: int = 1800, env: dict | None = None,
         prefix: list[str] | None = None) -> dict:
    run_env = evidence_env(env)
    argv = list(cmd)
    if prefix:
        # Под sudo PATH сбрасывается secure_path: исполняемый файл — по абсолютному пути, окружение — явно.
        exe = shutil.which(argv[0], path=run_env.get("PATH")) or argv[0]
        argv = [*prefix, exe, *argv[1:]] if prefix[0] != "sudo" else \
            [*prefix, "/usr/bin/env", "-i", *[f"{k}={v}" for k, v in sorted(run_env.items())], exe, *argv[1:]]
    try:
        r = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=run_env)
    except subprocess.TimeoutExpired:
        return {"exit_code": 124, "tail": "timeout"}
    except FileNotFoundError as e:
        return {"exit_code": 127, "tail": str(e)}
    # Хвост вывода — недоверенный текст (repr stdout теста мог содержать токен, инцидент M6).
    return {"exit_code": r.returncode, "tail": redact_tail((r.stdout or "") + (r.stderr or ""), 1500)}


# ------------------------------------------------ тестовые профили (AE-R1) ---
_PROBE = ("import socket, sys\ns = socket.socket(); s.settimeout(3)\n"
          "try:\n    s.connect(('1.1.1.1', 53))\nexcept OSError:\n    sys.exit(0)\nsys.exit(1)\n")
# Вторая проба: внутри изоляции sudo обязан НЕ работать — иначе код кандидата вернул бы себе сеть
# (`sudo nsenter -t 1 -n`). no_new_privs и пустой bounding set делают setuid-бинарники бессильными.
_PROBE_NO_SUDO = ("import os, socket, subprocess, sys\n"
                  "try:\n    r = subprocess.run(['sudo', '-n', 'true'], capture_output=True, timeout=10)\n"
                  "    if r.returncode == 0:\n        sys.exit(1)\n"
                  "except (OSError, subprocess.SubprocessError):\n    pass\n"
                  # Unix-сокет Docker не привязан к сетевому пространству: доступ к нему = сеть и root снова.
                  "for path in ('/var/run/docker.sock', '/run/docker.sock'):\n"
                  "    if os.path.exists(path):\n"
                  "        s = socket.socket(socket.AF_UNIX)\n"
                  "        try:\n            s.connect(path); sys.exit(1)\n"
                  "        except OSError:\n            pass\n"
                  "sys.exit(0)\n")


def offline_prefix(env: dict | None = None) -> tuple[list[str], str]:
    """Префикс команды без сети и его вид. Отключение проверяется пробами, а не предполагается:
    (1) соединение наружу не устанавливается; (2) sudo внутри не работает; (3) сокет Docker недоступен
    (группы очищены). Изоляция только СЕТЕВАЯ и best-effort: файловая система раннера не изолирована."""
    if os.environ.get("AE_FORCE_NETWORK_ISOLATION") == "off":
        return [], "NOT_ENFORCED"
    # Только sudo + unshare: в непривилегированном `unshare -r` setgroups запрещён, и --clear-groups не работает.
    candidates = []
    if shutil.which("sudo") and shutil.which("setpriv"):
        candidates.append(("SUDO_UNSHARE", ["sudo", "-n", "unshare", "-n", "--", "setpriv",
                                            f"--reuid={os.getuid()}", f"--regid={os.getgid()}", "--clear-groups",
                                            "--no-new-privs", "--inh-caps=-all", "--bounding-set=-all", "--"]))
    for name, pfx in candidates:
        if not shutil.which(pfx[0]) or not shutil.which("setpriv"):
            continue
        net = _run([sys.executable, "-c", _PROBE], Path.cwd(), timeout=30, env=env, prefix=pfx)
        nosudo = _run([sys.executable, "-c", _PROBE_NO_SUDO], Path.cwd(), timeout=30, env=env, prefix=pfx)
        if net["exit_code"] == 0 and nosudo["exit_code"] == 0:
            return pfx, name
    return [], "NOT_ENFORCED"


def parse_junit(path: Path) -> dict | None:
    """Сумма по JUnit-отчёту (pytest --junitxml, vitest --reporter=junit). Нет файла/не разбирается — None."""
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        return None
    suites = [root] if root.tag == "testsuite" else root.findall(".//testsuite")
    if root.tag == "testsuites" and root.get("tests") is not None:
        suites = [root]
    tot = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    for su in suites:
        for k in tot:
            try:
                tot[k] += int(float(su.get(k) or (su.get("skips") if k == "skipped" else 0) or 0))
            except ValueError:
                return None
    return tot


def profiles_for(changed_files: list[str], allowed: list[str] | None, policy: dict) -> list[str]:
    from tools.autonomy.policy import glob_match
    out = []
    for name, prof in policy["test_profiles"].items():
        if name == "comment":
            continue
        if allowed is not None and name not in allowed and name != "python":
            continue
        if name == "python" or any(glob_match(f, g) for f in changed_files for g in prof["when_changed"]):
            out.append(name)
    return out


def _label(name: str, cmd: list[str]) -> str:
    keep = [a for a in cmd if a != "{python}" and "{junit}" not in a and not a.startswith("--junitxml")
            and not a.startswith("--reporter") and not a.startswith("--outputFile")]
    return f"{name}: {' '.join(keep)}"[:200]


def run_profile(name: str, prof: dict, workdir: Path, python: str, prefix: list[str],
                env: dict | None = None) -> list[dict]:
    """Тесты профиля. PASS только при коде 0 И валидном непустом JUnit (если команда его пишет)."""
    cwd = workdir / prof.get("cwd", ".")
    out: list[dict] = []
    if prof.get("install"):
        r = _run(prof["install"], cwd, timeout=1200, env=env)       # установка — с сетью, без скриптов
        if r["exit_code"] != 0:
            return [{"name": f"{name}: install", "status": "FAIL", "exit_code": r["exit_code"],
                     "tail": mask_data("; ".join(public_tail(r["tail"])["failed_tests"]) or "install failed")}]
    for cmd in prof["commands"]:
        with tempfile.TemporaryDirectory(prefix="ae-junit-") as td:
            junit = Path(td) / "junit.xml"
            argv = [a.replace("{python}", python).replace("{junit}", str(junit)) for a in cmd]
            r = _run(argv, cwd, timeout=3600, env=env, prefix=prefix)
            counts = parse_junit(junit) if any("{junit}" in a for a in cmd) else None
            wants_junit = any("{junit}" in a for a in cmd)
        ok = r["exit_code"] == 0
        if wants_junit:
            ok = ok and counts is not None and counts["tests"] > 0 and counts["failures"] == 0 and counts["errors"] == 0
        pt = public_tail(r["tail"])
        out.append({"name": _label(name, cmd), "status": "PASS" if ok else "FAIL", "exit_code": r["exit_code"],
                    **({"junit": counts} if wants_junit else {}),
                    **({"reason": "JUNIT_MISSING_OR_EMPTY"} if wants_junit and r["exit_code"] == 0 and not ok else {}),
                    "tail": "; ".join(pt["failed_tests"]) + (" " if pt["failed_tests"] else "") + pt["fingerprint"]})
    return out


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
        from tools.autonomy.policy import load_policy, task_scope
        policy = load_policy()
        scope = task_scope(objective, policy)
        prefix, isolation = offline_prefix()
        tests = []
        for name in profiles_for(changed_files, scope["test_profiles"] if scope else None, policy):
            tests += run_profile(name, policy["test_profiles"][name], workdir, self.python, prefix)
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
            "sql_validation": {"status": "PASS" if sv["exit_code"] == 0 else "FAIL", "tail": mask_data(sv["tail"][-400:], 400)},
            "runtime_access": runtime, "parity": parity, "data_suites": data,
            "objective_resolution": resolution,
            # Исполнитель этих доказательств сам себе не доверяет: в доверенном ingest это поле
            # перезаписывается результатом сверки с retest (ReplayEvidenceRunner).
            "test_provenance": "TRUSTED_LOCAL",
            "network_isolation": isolation,
            "isolation_scope": "network-only, best-effort; runner filesystem is not isolated",
        }


class RetestRunner:
    """Повторный прогон тестов кандидата в job'е БЕЗ учётных данных (AE-R1).

    Харнесс — код main; кандидат применён в отдельной песочнице. Те же профили и на базовом
    коммите: число тестов в JUnit кандидата не может быть меньше базы (исчезнувшие тесты — FAIL)."""

    def __init__(self, python: str | None = None):
        self.python = python or sys.executable

    def collect(self, base_ws: Path, cand_ws: Path, changed_files: list[str], objective: dict) -> dict:
        from tools.autonomy.policy import load_policy, task_scope
        policy = load_policy()
        scope = task_scope(objective, policy)
        prefix, isolation = offline_prefix()
        names = profiles_for(changed_files, scope["test_profiles"] if scope else None, policy)
        tests = []
        for name in names:
            prof = policy["test_profiles"][name]
            base = {t["name"]: t for t in run_profile(name, prof, base_ws, self.python, prefix)}
            for t in run_profile(name, prof, cand_ws, self.python, prefix):
                b = base.get(t["name"], {}).get("junit")
                ran = lambda j: j["tests"] - j.get("skipped", 0)  # noqa: E731 — пропущенный тест не исполнялся
                if b and t.get("junit") and ran(t["junit"]) < ran(b):
                    t = {**t, "status": "FAIL",
                         "reason": f"TESTS_DISAPPEARED: исполнено {ran(t['junit'])} < {ran(b)} на базе"}
                elif b and t.get("junit") and t["junit"].get("skipped", 0) > b.get("skipped", 0):
                    # «Пропустить один и добавить пустой» не меняет число исполненных — ловим рост пропусков.
                    t = {**t, "status": "FAIL",
                         "reason": f"TESTS_SKIPPED_GREW: пропущено {t['junit']['skipped']} > {b.get('skipped', 0)} на базе"}
                if b:
                    t["base_junit"] = b
                tests.append(t)
        return {"schema": "ae_retest/1", "changed_files": sorted(changed_files), "profiles": names,
                "tests": tests, "network_isolation": isolation,
                "isolation_scope": "network-only, best-effort; runner filesystem is not isolated"}


def reconcile(untrusted: dict, retest: dict | None) -> dict:
    """Тесты — из независимого retest; расхождение с недоверенным job'ом — evidence_disagreement."""
    ev = dict(untrusted)
    if retest is None:
        ev["test_provenance"] = "UNTRUSTED_ONLY"
        ev["evidence_disagreement"] = []
        return ev
    mine = {t["name"]: t["status"] for t in untrusted.get("tests", [])}
    theirs = {t["name"]: t["status"] for t in retest.get("tests", [])}
    disagreement = sorted(
        [f"{n}: недоверенный {mine.get(n, 'нет')} ≠ retest {theirs[n]}" for n in theirs if mine.get(n) != theirs[n]]
        + [f"{n}: есть только в недоверенном job'е" for n in mine if n not in theirs])
    ev["tests"] = retest.get("tests", [])
    ev["network_isolation"] = retest.get("network_isolation", "NOT_ENFORCED")
    ev["isolation_scope"] = "network-only, best-effort; runner filesystem is not isolated"
    ev["evidence_disagreement"] = disagreement
    ev["test_provenance"] = "RECONCILED" if not disagreement and ev["tests"] else "DISAGREEMENT"
    return ev


class ReplayEvidenceRunner:
    """Доказательства кандидата, собранные в ОТДЕЛЬНОМ job, исполнявшем код кандидата.

    Анализ влияния (impact) — доверенный: считается инструментом репозитория main по списку
    файлов. Доказательства кандидата — недоверенный артефакт с проверкой sha256.

    ОГРАНИЧЕНИЕ, названное прямо: доказательства, полученные исполнением кода кандидата,
    корректны против ошибающегося кандидата, но не против злонамеренного — код теста может
    подделать собственный результат. Последний рубеж против злонамеренного кандидата —
    независимое ревью диффа и слияние только человеком (AE_V1_SECURITY.md §4)."""

    REQUIRED = ("tests", "sql_validation", "runtime_access", "parity", "data_suites", "objective_resolution")

    def __init__(self, pending_file: Path, expect_sha256: str, impact_runner, trusted_repo: Path,
                 retest_file: Path | None = None, retest_sha256: str | None = None):
        from tools.autonomy.agents import IntegrityError
        if not expect_sha256:
            raise IntegrityError("ReplayEvidenceRunner без sha256 производителя: доказательства не с чем сверить")
        self.file, self.expect, self.impact_runner = Path(pending_file), expect_sha256, impact_runner
        # retest без объявленного хеша не читается вовсе (тогда UNTRUSTED_ONLY — не готово).
        self.retest_file = Path(retest_file) if retest_file and retest_sha256 else None
        self.retest_sha = retest_sha256
        # Анализ влияния — инструмент репозитория, т.е. КОД. В доверенном job'е он исполняется
        # только из доверенного checkout (main), НИКОГДА из песочницы с кандидатом: иначе
        # кандидат подменил бы tools/impact_analysis.py (или положил tools/json.py) и исполнил
        # свой код рядом с токеном записи.
        self.trusted_repo = Path(trusted_repo)

    def impact(self, workdir: Path, files: list[str]) -> dict:
        return self.impact_runner.impact(self.trusted_repo, files)

    def collect(self, workdir: Path, changed_files: list[str], objective: dict) -> dict:
        from tools.autonomy.agents import IntegrityError, sha256_file
        if not self.file.exists():
            raise IntegrityError(f"нет недоверенных доказательств {self.file}")
        if sha256_file(self.file) != self.expect:
            raise IntegrityError("доказательства кандидата: sha256 не совпал с объявленным производителем")
        raw = json.loads(self.file.read_text(encoding="utf-8"))
        missing = [k for k in self.REQUIRED if k not in raw]
        if missing:
            raise IntegrityError(f"доказательства кандидата неполны: нет {missing}")
        declared = sorted(raw.get("changed_files", changed_files))
        if declared != sorted(changed_files):
            raise IntegrityError(f"доказательства собраны не по этому кандидату: {declared} != {sorted(changed_files)}")
        # Недоверенный документ — только известные ключи и маскированные значения (B3): лишние поля,
        # подброшенные кодом кандидата, в доверенное и публичное состояние не попадают.
        from tools.autonomy.output_policy import public_obj
        ev = {k: public_obj(raw[k], 400) for k in self.REQUIRED + ("changed_files", "network_isolation") if k in raw}
        ev["impact"] = {k: v for k, v in self.impact(workdir, changed_files).items()
                        if k in ("risk_tier", "affected_total", "required_contracts", "assets_without_contract",
                                 "flags", "downstream_assets")}
        retest = None
        if self.retest_file is not None:
            if not self.retest_file.exists() or sha256_file(self.retest_file) != self.retest_sha:
                raise IntegrityError("retest: файл отсутствует или sha256 не совпал с объявленным производителем")
            retest = json.loads(self.retest_file.read_text(encoding="utf-8"))
            if retest.get("schema") != "ae_retest/1" or sorted(retest.get("changed_files", [])) != sorted(changed_files):
                raise IntegrityError("retest собран не по этому кандидату или не той схемой")
            from tools.autonomy.output_policy import public_obj
            retest = {"schema": retest["schema"], "changed_files": retest["changed_files"],
                      "tests": public_obj(retest.get("tests", []), 400),
                      "network_isolation": str(retest.get("network_isolation", "NOT_ENFORCED"))[:40]}
        return reconcile(ev, retest)
