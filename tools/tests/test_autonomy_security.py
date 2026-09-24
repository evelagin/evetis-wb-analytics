"""Структурная безопасность AE v1: доказательства из конфигурации, а не из промпта.

«Агент сказал, что не будет менять production» — не граница безопасности. Здесь граница
проверяется там, где она живёт: права jobs, триггеры workflows, роли сервисных аккаунтов,
WIF-привязки, флаги запуска агента, окружение процесса агента.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

import ae_fixtures as F  # noqa: E402
from tools.autonomy import policy as P
from tools.autonomy.agents import ClaudeCliAdapter, scrubbed_env
from tools.autonomy.schema import load_schema

WF = F.REPO / ".github" / "workflows"
AE_WORKFLOWS = sorted(WF.glob("autonomy-*.yml"))
TF = F.REPO / "infra" / "terraform"
UNTRUSTED = {"autonomy-engineer.yml", "autonomy-test.yml", "autonomy-review.yml"}


def text(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def jobs(src: str) -> dict[str, str]:
    """Разрезать workflow на блоки jobs верхнего уровня (отступ 2) без YAML-парсера."""
    body = src.split("\njobs:\n", 1)[1]
    out, name, buf = {}, None, []
    for line in body.splitlines():
        m = re.match(r"^  ([A-Za-z0-9_-]+):\s*$", line)
        if m:
            if name:
                out[name] = "\n".join(buf)
            name, buf = m.group(1), []
        elif name:
            buf.append(line)
    if name:
        out[name] = "\n".join(buf)
    return out


def test_all_six_ae_workflows_exist():
    assert {p.name for p in AE_WORKFLOWS} == {"autonomy-watch.yml", "autonomy-run.yml", "autonomy-engineer.yml",
                                              "autonomy-test.yml", "autonomy-review.yml", "autonomy-gate.yml"}


@pytest.mark.parametrize("wf", AE_WORKFLOWS, ids=lambda p: p.name)
def test_default_permissions_are_empty(wf):
    assert re.search(r"^permissions: \{\}$", text(wf), re.M)


@pytest.mark.parametrize("wf", AE_WORKFLOWS, ids=lambda p: p.name)
def test_no_untrusted_triggers(wf):
    on = text(wf).split("\non:\n", 1)[1].split("\npermissions:", 1)[0]
    for bad in ("pull_request", "pull_request_target", "issue_comment", "issues:", "push:", "workflow_run",
                "repository_dispatch", "discussion"):
        assert bad not in on, f"{wf.name}: триггер {bad}"


@pytest.mark.parametrize("wf", AE_WORKFLOWS, ids=lambda p: p.name)
def test_no_secrets_and_no_static_anthropic_key(wf):
    src = text(wf)
    assert "secrets." not in src, f"{wf.name}: AE не использует секреты — федерация идентичности вместо ключей"
    assert "anthropic_api_key" not in src
    assert not re.search(r"^\s*ANTHROPIC_API_KEY\s*:", src, re.M), "ключ задан как переменная окружения job"


@pytest.mark.parametrize("wf", AE_WORKFLOWS, ids=lambda p: p.name)
def test_no_production_mutation_commands(wf):
    src = text(wf)
    for bad in ("terraform apply", "terraform destroy", "bq query", "bq rm", "gcloud run deploy",
                "gcloud run jobs update", "gcloud run jobs execute", "gcloud scheduler", "gcloud iam",
                "dangerously-skip-permissions", "bypassPermissions", "HEAD:refs/heads/main", "push origin main",
                "@anthropic-ai/claude-code@latest", "clasp push"):
        assert bad not in src, f"{wf.name}: {bad}"


@pytest.mark.parametrize("name", sorted(UNTRUSTED))
def test_untrusted_jobs_are_read_only(name):
    src = text(WF / name)
    for job, body in jobs(src).items():
        assert "contents: read" in body, f"{name}/{job}"
        for bad in ("contents: write", "pull-requests: write", "actions: write", "issues: write", "packages: write",
                    "statuses: write", "deployments: write"):
            assert bad not in body, f"{name}/{job}: {bad}"
    assert "persist-credentials: false" in src
    assert "state_push.sh" not in src, f"{name}: недоверенный job пишет авторитетное состояние"


def test_reviewer_has_no_cloud_credentials_and_restricted_tools():
    src = text(WF / "autonomy-review.yml")
    assert "google-github-actions/auth" not in src
    assert "--reviewer claude" in src and "--engineer none" in src


def test_trusted_step_never_runs_an_agent():
    src = text(WF / "autonomy-gate.yml")
    assert "--engineer claude" not in src and "--reviewer claude" not in src
    assert "claude-code" not in src and "refresh_anthropic_oidc" not in src
    assert re.findall(r"--engineer (\w+)", src) and set(re.findall(r"--engineer (\w+)", src)) <= {"none", "replay"}


def test_write_tokens_only_in_deterministic_jobs():
    allowed = {("autonomy-watch.yml", "watch"), ("autonomy-gate.yml", "step"),
               ("autonomy-run.yml", "publish"), ("autonomy-run.yml", "ci-verify"), ("autonomy-run.yml", "persist")}
    for wf in AE_WORKFLOWS:
        for job, body in jobs(text(wf)).items():
            if "contents: write" in body and "uses: ./" not in body:
                assert (wf.name, job) in allowed, f"{wf.name}/{job} получил contents: write"


def test_actions_write_only_in_trusted_dispatchers():
    """actions: write = право запустить ЛЮБОЙ workflow, включая deploy-prod и infra (environments
    на тарифе без правил защиты). Поэтому оно есть только у детерминированных job'ов без кода агента."""
    allowed = {("autonomy-watch.yml", "watch"), ("autonomy-run.yml", "publish"), ("autonomy-run.yml", "persist")}
    for wf in AE_WORKFLOWS:
        for job, body in jobs(text(wf)).items():
            if "actions: write" in body:
                assert (wf.name, job) in allowed, f"{wf.name}/{job} получил actions: write"


def test_no_scheduled_autonomy():
    """Фаза READY_FOR_CONTROLLED_CANARY: ни планового наблюдения, ни автоматического исправления."""
    for wf in AE_WORKFLOWS:
        on = text(wf).split("\non:\n", 1)[1].split("\npermissions:", 1)[0]
        assert "schedule" not in on and "cron" not in on, f"{wf.name}: расписание"
        assert "github.event_name == 'schedule'" not in text(wf)


@pytest.mark.parametrize("wf", AE_WORKFLOWS, ids=lambda p: p.name)
def test_third_party_actions_pinned_by_commit_sha(wf):
    for m in re.finditer(r"uses:\s*([^\s#]+)", text(wf)):
        ref = m.group(1)
        if ref.startswith("./"):
            continue
        assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", ref), f"{wf.name}: {ref} не закреплён по SHA"


@pytest.mark.parametrize("wf", AE_WORKFLOWS, ids=lambda p: p.name)
def test_no_expression_interpolation_inside_run_scripts(wf):
    """${{ }} в run: подставляется ДО bash — входы/outputs становятся кодом. Только через env."""
    lines = text(wf).splitlines()
    i = 0
    while i < len(lines):
        m = re.match(r"^(\s*)(- )?run: ?(.*)$", lines[i])
        if not m:
            i += 1
            continue
        ind = len(m.group(1)) + (2 if m.group(2) else 0)
        block, j = [m.group(3)], i + 1
        if m.group(3).strip() in ("|", ">", "|-", ">-"):
            while j < len(lines) and (not lines[j].strip() or len(lines[j]) - len(lines[j].lstrip()) > ind):
                block.append(lines[j]); j += 1
        assert not any("${{" in b for b in block), f"{wf.name}:{i + 1}: выражение внутри run"
        i = j


@pytest.mark.parametrize("name", ["autonomy-engineer.yml", "autonomy-review.yml"])
def test_static_anthropic_credentials_fail_closed_before_agent(name):
    src = text(WF / name)
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE", "CLAUDE_CODE_OAUTH_TOKEN"):
        assert "${" + var + ":-}" in src, f"{name}: нет проверки {var}"


def test_run_id_is_strictly_validated_in_every_ae_job_that_receives_it():
    rx = "^run-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$"
    for name in ("autonomy-gate.yml", "autonomy-engineer.yml", "autonomy-test.yml", "autonomy-review.yml"):
        assert rx in text(WF / name), name
    run = text(WF / "autonomy-run.yml")
    for job in ("publish", "ci-verify", "persist"):
        assert rx in jobs(run)[job], job


def test_every_git_push_targets_only_autonomy_state():
    scripts = [F.REPO / "tools/autonomy/ci/state_push.sh"] + AE_WORKFLOWS
    for p in scripts:
        for m in re.finditer(r"git push[^\n]*", text(p)):
            assert "HEAD:refs/heads/autonomy-state" in m.group(0), f"{p.name}: {m.group(0)}"


def test_kill_switch_guards_dispatch_and_runs():
    assert "vars.AE_ENABLED == 'true'" in text(WF / "autonomy-run.yml")
    assert "vars.AE_ENABLED == 'true'" in text(WF / "autonomy-watch.yml")


def test_agent_version_is_pinned():
    for name in ("autonomy-engineer.yml", "autonomy-review.yml"):
        assert re.search(r"@anthropic-ai/claude-code@\d+\.\d+\.\d+", text(WF / name))


# ------------------------------------------------------------------ Terraform ---
def test_ae_reader_has_only_read_roles():
    src = text(TF / "autonomy.tf")
    roles = set(re.findall(r'"(roles/[a-zA-Z.]+)"', src))
    assert roles == {"roles/bigquery.resourceViewer", "roles/logging.viewer",
                     "roles/bigquery.dataViewer", "roles/iam.workloadIdentityUser"}
    # jobUser запрещён: в нём создание ресурсов Dataform и чат Gemini (откат M3 2026-09-24).
    assert '"roles/bigquery.jobUser"' not in src
    custom = re.search(r'"ae_job_runner" \{.*?permissions = \[(.*?)\]', src, re.S).group(1)
    assert sorted(re.findall(r'"([a-z.]+)"', custom)) == ["bigquery.config.get", "bigquery.jobs.create"]
    for bad in ("dataEditor", "dataOwner", "admin", "roles/editor", "roles/owner", "run.developer", "secretAccessor"):
        assert bad not in src


def test_wif_bindings_are_proven_semantically_not_by_string_match():
    """Строковые проверки привязок заменены решением GCP на точных claims GitHub
    (tools/tests/test_autonomy_wif.py, случаи A–E). Здесь — только что модель видит ВСЕ
    привязки workloadIdentityUser из Terraform, а не часть."""
    from tools.autonomy.wif_check import load_terraform
    tf = "\n".join(p.read_text() for p in TF.glob("*.tf"))
    declared = tf.count('role               = "roles/iam.workloadIdentityUser"')
    model = load_terraform()
    assert declared == 4    # deployer, terraform_plan, terraform_apply, ae_reader (for_each внутри)
    assert set(model.bindings) == {"sa-deployer", "sa-terraform-plan", "sa-terraform-apply", "sa-ae-reader"}


# ------------------------------------------------------------------- агент ---
@pytest.mark.parametrize("role", ["engineer_plan", "engineer_implement", "reviewer"])
def test_agent_command_is_restricted(role):
    cmd = ClaudeCliAdapter().command(role, load_schema("review_verdict" if role == "reviewer" else "engineer_report"))
    joined = " ".join(cmd)
    for must in ("--restricted", "--no-session-persistence", "--strict-mcp-config", "--json-schema"):
        assert must in cmd, must
    assert cmd[cmd.index("--permission-mode") + 1] == "dontAsk"
    assert "--dangerously-skip-permissions" not in cmd and "bypassPermissions" not in joined
    tools = cmd[cmd.index("--tools") + 1].split(",")
    if role == "reviewer":
        assert tools == ["Read", "Grep", "Glob"]
    else:
        denied = cmd[cmd.index("--disallowedTools") + 1]
        for d in ("git push", "gcloud", "bq:", "terraform", "gh:", "curl"):
            assert d in denied, d
        assert ("Edit" in tools) == (role == "engineer_implement")
        allowed = cmd[cmd.index("--allowedTools") + 1]
        for bypass in ("Bash(cat", "Bash(head", "Bash(ls", "Bash(env", "Bash(printenv"):
            assert bypass not in allowed, bypass


def test_scrubbed_env_removes_write_credentials():
    base = {"PATH": "/usr/bin:/bin", "HOME": "/tmp", "GITHUB_TOKEN": "x", "GH_TOKEN": "x",
            "GOOGLE_APPLICATION_CREDENTIALS": "/c.json", "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "x",
            "ACTIONS_ID_TOKEN_REQUEST_URL": "x", "WB_TOKEN_CONTENT": "x", "OZON_API_KEY": "x", "SSH_AUTH_SOCK": "/s"}
    env = scrubbed_env(base)
    for gone in ("GITHUB_TOKEN", "GH_TOKEN", "GOOGLE_APPLICATION_CREDENTIALS", "ACTIONS_ID_TOKEN_REQUEST_TOKEN",
                 "ACTIONS_ID_TOKEN_REQUEST_URL", "WB_TOKEN_CONTENT", "OZON_API_KEY", "SSH_AUTH_SOCK"):
        assert gone not in env, gone
    assert env["GIT_CONFIG_NOSYSTEM"] == "1" and Path(env["GIT_CONFIG_GLOBAL"]).read_text() == ""
    assert not any(Path(env["CLOUDSDK_CONFIG"]).iterdir()) and not any(Path(env["GH_CONFIG_DIR"]).iterdir())


def test_ci_keep_list_passes_only_read_identity():
    from tools.autonomy.cli import CI_KEEP_ENV
    assert not any(k.startswith(("GITHUB", "GH_", "ACTIONS_", "WB_", "OZON_")) for k in CI_KEEP_ENV)


@pytest.mark.skipif(not shutil.which("gcloud"), reason="gcloud не установлен")
def test_gcloud_is_unauthenticated_inside_agent_env():
    r = subprocess.run(["gcloud", "auth", "print-access-token"], env=scrubbed_env(), capture_output=True, text=True,
                       timeout=60)
    assert r.returncode != 0, "агент получил бы токен GCP"


def test_git_has_no_credential_helper_inside_agent_env():
    r = subprocess.run(["git", "config", "--get-all", "credential.helper"], env=scrubbed_env(),
                       capture_output=True, text=True, cwd="/")
    assert r.stdout.strip() == ""


@pytest.mark.skipif(not shutil.which("gh"), reason="gh не установлен")
def test_gh_is_unauthenticated_inside_agent_env():
    r = subprocess.run(["gh", "auth", "status"], env=scrubbed_env(), capture_output=True, text=True, timeout=60)
    assert r.returncode != 0, "агент получил бы токен GitHub"


def test_readonly_bigquery_client_refuses_mutation_before_any_request():
    import sys
    sys.path.insert(0, str(F.REPO / "tools"))
    from lib.bq_readonly import ReadOnlyBigQuery, ReadOnlyViolation

    class Boom:
        def open(self, *a, **k):
            raise AssertionError("запрос ушёл в сеть")

    bq = ReadOnlyBigQuery(project="p", token="t", _opener=Boom())
    for sql in ("CREATE OR REPLACE VIEW `p.d.v` AS SELECT 1", "DELETE FROM `p.d.t` WHERE TRUE",
                "INSERT INTO `p.d.t` VALUES (1)", "DROP TABLE `p.d.t`", "CALL `p.d.sp`()",
                "MERGE `p.d.t` USING s ON TRUE WHEN MATCHED THEN DELETE"):
        with pytest.raises(ReadOnlyViolation):
            bq.query(sql)


def test_policy_granting_production_authority_is_rejected(tmp_path, monkeypatch):
    bad = json.loads(P.POLICY.read_text())
    bad["production_mutation_authority"] = "LIMITED"
    f = tmp_path / "policy.json"
    f.write_text(json.dumps(bad))
    monkeypatch.setattr(P, "POLICY", f)
    P.load_policy.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="NONE"):
            P.load_policy()
    finally:
        P.load_policy.cache_clear()


def test_deterministic_components_cannot_call_a_model():
    for mod in ("watcher.py", "gatekeeper.py", "policy.py", "state.py", "publisher.py", "audit.py"):
        src = text(F.REPO / "tools" / "autonomy" / mod)
        assert "agents import" not in src and "ClaudeCliAdapter" not in src, mod


@pytest.mark.parametrize("role", ["engineer_plan", "engineer_implement", "reviewer"])
def test_cli_schema_has_no_draft_uri_the_cli_validator_rejects(role):
    """Регресс 2026-09-24: CLI отвергал --json-schema целиком из-за URI черновика 2020-12 в $schema,
    агент не запускался, прогон уходил в BLOCKED с «вывод агента не JSON»."""
    cmd = ClaudeCliAdapter().command(role, load_schema("review_verdict" if role == "reviewer" else "engineer_report"))
    passed = json.loads(cmd[cmd.index("--json-schema") + 1])
    assert "$schema" not in passed and "$id" not in passed
    assert passed["required"] and passed["properties"]      # сама схема передана целиком
