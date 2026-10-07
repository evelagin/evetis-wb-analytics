"""Публичный репозиторий: структурные инварианты workflows и отсутствие учётных данных в дереве.

Репозиторий публичный, поэтому граница безопасности — не «никто не увидит», а конфигурация:
  * ни один workflow, который может запустить чужой PR, не получает OIDC/облачные права/запись;
  * привилегированные SA появляются только в файлах, которые им разрешает WIF (wif.tf);
  * все внешние actions закреплены полным SHA коммита (тег можно переписать, SHA — нет);
  * входы dispatch не подставляются ${{ }} в текст shell-скриптов (только через env);
  * checkout не оставляет токен в .git/config там, где job ничего не пушит;
  * в отслеживаемых файлах нет учётных данных известных форматов.

Без YAML-парсера (в CI его нет): разбор по отступам, как в test_autonomy_security.py.
Живые настройки GitHub/GCP здесь не проверяются — это docs/security/PUBLIC_REPOSITORY_POLICY.md.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
WF_DIR = REPO / ".github" / "workflows"
WORKFLOWS = sorted(WF_DIR.glob("*.yml"))

# Файлы, которым WIF (infra/terraform/wif.tf, живые привязки) разрешает привилегированные SA.
PRIVILEGED_SA_FILES = {
    "DEPLOYER_SA": {"deploy-prod.yml", "deploy-shadow.yml"},
    "TERRAFORM_APPLY_SA": {"infra.yml", "scheduler-control.yml"},
    "TERRAFORM_PLAN_SA": {"infra.yml"},
    "sa-tenant-provisioner@": {"tenant-infra.yml"},
    # Те же идентичности литералом e-mail (в обход переменных репозитория).
    "sa-deployer@": {"deploy-prod.yml", "deploy-shadow.yml"},
    "sa-terraform-apply@": {"infra.yml", "scheduler-control.yml"},
    "sa-terraform-plan@": {"infra.yml"},
}
PRIVILEGED_WORKFLOWS = set().union(*PRIVILEGED_SA_FILES.values())
# Единственный checkout, которому нужен токен в .git/config: публикатор AE пушит ветки ae/*.
CHECKOUT_MAY_PERSIST = {("autonomy-run.yml", "publish")}
# Разрешённые события (allowlist): всё прочее — issues, issue_comment, discussion*, fork, watch,
# pull_request_target, workflow_run, repository_dispatch — в публичном репозитории запускает посторонний.
ALLOWED_TRIGGERS = {"push", "pull_request", "workflow_dispatch", "workflow_call", "schedule", "merge_group"}


def text(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def strip_comments(src: str) -> str:
    return "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))


def on_block(src: str) -> str:
    m = re.search(r"^on:\s*\n(.*?)(?=^\S)", src, re.M | re.S)
    return m.group(1) if m else ""


def triggers(src: str) -> set[str]:
    return set(re.findall(r"^  ([a-z_]+):", on_block(src), re.M))


def jobs(src: str) -> dict[str, str]:
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


def run_scripts(src: str) -> list[str]:
    """Тексты всех `run:` и `script:` (github-script), однострочных и блочных `|`/`>`."""
    lines, out, i = src.splitlines(), [], 0
    while i < len(lines):
        m = re.match(r"^(\s*)(?:- )?(?:run|script):\s*(.*)$", lines[i])
        if not m:
            i += 1
            continue
        indent, rest = len(m.group(1)), m.group(2).strip()
        if rest and rest[0] not in "|>":
            out.append(rest)
            i += 1
            continue
        buf, i = [], i + 1
        while i < len(lines) and (not lines[i].strip() or len(lines[i]) - len(lines[i].lstrip()) > indent):
            buf.append(lines[i])
            i += 1
        out.append("\n".join(buf))
    return out


def test_workflows_found():
    names = {p.name for p in WORKFLOWS}
    assert PRIVILEGED_WORKFLOWS <= names
    assert {"ci.yml", "sql-current.yml"} <= names


# ── поставка: внешние actions только по полному SHA ─────────────────────────────────────
@pytest.mark.parametrize("wf", WORKFLOWS, ids=lambda p: p.name)
def test_external_actions_pinned_to_full_sha_with_version_comment(wf):
    for line in strip_comments(text(wf)).splitlines():
        m = re.match(r"^\s*(?:- )?uses:\s*(\S+)(.*)$", line)
        if not m or m.group(1).startswith("./"):
            continue
        ref = m.group(1)
        assert re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_./-]+@[0-9a-f]{40}", ref), f"{wf.name}: плавающая ссылка {ref}"
        assert re.match(r"\s+# v\d", m.group(2)), f"{wf.name}: у {ref} нет комментария с версией"


def test_same_action_same_sha_everywhere():
    seen: dict[str, set[str]] = {}
    for wf in WORKFLOWS:
        for action, sha in re.findall(r"uses:\s*([A-Za-z0-9_.-]+/[A-Za-z0-9_./-]+)@([0-9a-f]{40})", text(wf)):
            seen.setdefault(action, set()).add(sha)
    drift = {a: s for a, s in seen.items() if len(s) > 1}
    assert not drift, f"одна и та же action закреплена разными SHA: {drift}"


# ── триггеры и права ────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("wf", WORKFLOWS, ids=lambda p: p.name)
def test_on_is_block_style(wf):
    # Проверки ниже читают блок `on:`; `on: [pull_request]` или `on: push` прошли бы их вхолостую.
    assert re.search(r"^on:\s*$", text(wf), re.M), f"{wf.name}: только блочная форма `on:`"


@pytest.mark.parametrize("wf", WORKFLOWS, ids=lambda p: p.name)
def test_only_allowlisted_triggers(wf):
    extra = triggers(text(wf)) - ALLOWED_TRIGGERS
    assert not extra, f"{wf.name}: триггеры {extra} не разрешены в публичном репозитории"


@pytest.mark.parametrize("wf", WORKFLOWS, ids=lambda p: p.name)
def test_explicit_top_level_permissions_and_no_blanket_grants(wf):
    src = strip_comments(text(wf))
    assert re.search(r"^permissions:", src, re.M), f"{wf.name}: нет явных permissions верхнего уровня"
    assert not re.search(r"permissions:\s*(write-all|read-all)", src), f"{wf.name}: write-all/read-all"


def _pr_reachable(wf: Path) -> bool:
    on = on_block(text(wf))
    if re.search(r"^\s+pull_request:", on, re.M):
        return True
    push = re.search(r"^  push:\s*\n((?:    .*\n?)*)", on, re.M)
    return bool(push and "branches-ignore" in push.group(1))


PR_WORKFLOWS = [wf for wf in WORKFLOWS if _pr_reachable(wf)]


def test_pr_reachable_set_is_known():
    # Новый PR-триггер — повод заново пройти этот файл, а не молча расширить поверхность.
    assert {wf.name for wf in PR_WORKFLOWS} == {"ci.yml", "sql-current.yml"}


@pytest.mark.parametrize("wf", PR_WORKFLOWS, ids=lambda p: p.name)
def test_pr_workflows_have_no_cloud_identity_or_write(wf):
    src = strip_comments(text(wf))
    assert "id-token" not in src, f"{wf.name}: PR-workflow не должен запрашивать OIDC"
    assert not re.search(r":\s*write\b", src), f"{wf.name}: PR-workflow с правом записи"
    assert "google-github-actions/auth" not in src
    assert not re.search(r"\bsecrets\.|secrets:\s*inherit", src), f"{wf.name}: секреты в PR-workflow"
    assert not re.search(r"vars\.[A-Z_]*_SA\b|vars\.WIF_PROVIDER", src), f"{wf.name}: WIF/SA в PR-workflow"


@pytest.mark.parametrize("marker,allowed", sorted(PRIVILEGED_SA_FILES.items()))
def test_privileged_identity_only_in_wif_bound_files(marker, allowed):
    users = {wf.name for wf in WORKFLOWS if marker in strip_comments(text(wf))}
    assert users <= allowed, f"{marker} используется вне разрешённых WIF файлов: {users - allowed}"


@pytest.mark.parametrize("name", sorted(PRIVILEGED_WORKFLOWS))
def test_privileged_workflows_only_dispatch_or_main_push(name):
    on = on_block(text(WF_DIR / name))
    found = triggers(text(WF_DIR / name))
    assert found and found <= {"workflow_dispatch", "push"}, f"{name}: {found}"
    if "push" in found:
        assert re.search(r"^  push:\s*\n    branches:\s*\[\"main\"\]", on, re.M), f"{name}: push не только в main"


@pytest.mark.parametrize("name,job,env", [
    ("deploy-prod.yml", "promote-prod", "production"),
    ("infra.yml", "apply", "infra"),
    ("scheduler-control.yml", "control", "production"),
])
def test_privileged_jobs_bound_to_environment(name, job, env):
    assert re.search(rf"^\s+environment:.*\b{env}\b", jobs(text(WF_DIR / name))[job], re.M)


# ── инъекция выражений в shell ──────────────────────────────────────────────────────────
# Любое выражение, которое упоминает входы, данные события, head_ref или env (двойная подстановка):
# ловит и `${{ format('{0}', inputs.x) }}`, `${{ inputs['x'] }}`, `${{ toJSON(github.event) }}`.
UNTRUSTED_EXPR = re.compile(r"\$\{\{(?:(?!\}\}).)*?\b(inputs|github\.event|github\.head_ref|env)\b")


@pytest.mark.parametrize("expr", ["${{ inputs.x }}", "${{ format('{0}', inputs.x) }}", "${{ inputs['x'] }}",
                                  "${{ toJSON(github.event) }}", "${{ github.head_ref }}", "${{ env.DIGEST }}"])
def test_untrusted_expression_pattern_catches_variants(expr):
    assert UNTRUSTED_EXPR.search(expr)


@pytest.mark.parametrize("expr", ["${{ vars.GCP_REGION }}", "${{ steps.push.outputs.digest }}", "${{ runner.temp }}"])
def test_untrusted_expression_pattern_allows_trusted(expr):
    assert not UNTRUSTED_EXPR.search(expr)


@pytest.mark.parametrize("wf", WORKFLOWS, ids=lambda p: p.name)
def test_no_inputs_or_event_data_interpolated_into_shell(wf):
    for script in run_scripts(text(wf)):  # без strip_comments: `#` внутри скрипта GitHub всё равно подставляет
        hit = UNTRUSTED_EXPR.search(script)
        assert not hit, f"{wf.name}: вход подставлен прямо в скрипт ({script[hit.start():hit.start() + 40]!r}); передайте через env"


def test_run_script_parser_sees_blocks():
    scripts = run_scripts(text(WF_DIR / "deploy-prod.yml"))
    assert any("gcloud run jobs update wb-funnel-prod" in s for s in scripts)


# ── checkout без сохранённого токена ────────────────────────────────────────────────────
@pytest.mark.parametrize("wf", WORKFLOWS, ids=lambda p: p.name)
def test_checkout_does_not_persist_token_unless_job_pushes(wf):
    for job, body in jobs(text(wf)).items():
        lines = body.splitlines()
        for i, line in enumerate(lines):
            if "uses: actions/checkout@" not in line:
                continue
            step = []
            for nxt in lines[i + 1:]:
                if re.match(r"^\s*- ", nxt):
                    break
                step.append(nxt)
            persists = not re.search(r"persist-credentials:\s*false", strip_comments("\n".join(step)))
            if (wf.name, job) in CHECKOUT_MAY_PERSIST:
                continue
            assert not persists, f"{wf.name}/{job}: checkout без persist-credentials: false"


# ── учётные данные в дереве ─────────────────────────────────────────────────────────────
# Шаблоны собраны из частей, чтобы этот файл сам не совпадал с ними.
_B = "-----BEGIN "
CREDENTIAL_PATTERNS = {
    "PEM private key with body": _B + r"(?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----\s*[A-Za-z0-9+/=\s]{64,}",
    "GCP service-account key JSON": r'"private_key"\s*:\s*"' + _B,
    "GitHub token": r"\b(?:gh[pousr]" + r"_[A-Za-z0-9]{36,}|github_pat" + r"_[A-Za-z0-9_]{60,})",
    "Google API key": r"\bAI" + r"za[0-9A-Za-z_\-]{35}\b",
    "Google OAuth client secret": r"\bGOC" + r"SPX-[0-9A-Za-z_\-]{20,}",
    "Google OAuth refresh token": r"\b1//0[0-9A-Za-z_\-]{40,}",
    "Anthropic key": r"\bsk-" + r"ant-(?:api|admin)\d{2}-[A-Za-z0-9_\-]{20,}",
    "OpenAI key": r"\bsk-" + r"(?:proj|svcacct|admin)-[A-Za-z0-9_\-]{32,}",
    "Slack token": r"\bxox" + r"[abposr]-[0-9A-Za-z\-]{10,}",
    "Telegram bot token": r"\b\d{8,10}:A" + r"A[0-9A-Za-z_\-]{33}\b",
    "AWS access key": r"\b(?:AK" + r"IA|AS" + r"IA)[0-9A-Z]{16}\b",
    "long JWT (WB API token format)": r"\beyJ[A-Za-z0-9_\-]{20,}\.eyJ[A-Za-z0-9_\-]{100,}\.[A-Za-z0-9_\-]{20,}",
    "DSN with password": r"\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp)://[^\s:/@]+:[^\s@/$]{4,}@",
}
SKIP_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".pdf", ".xlsx", ".gz", ".zip", ".woff", ".woff2")


def tracked_files() -> list[Path]:
    out = subprocess.run(["git", "-C", str(REPO), "ls-files", "-z"], capture_output=True, check=True).stdout
    return [REPO / p for p in out.decode().split("\0") if p and not p.endswith(SKIP_SUFFIXES)]


def credential_findings() -> list[str]:
    """Только метаданные (путь, вид, длина). Значение не покидает функцию: при падении теста
    `pytest -l/--showlocals` печатает локальные переменные теста, а не этой функции."""
    compiled = {k: re.compile(v) for k, v in CREDENTIAL_PATTERNS.items()}
    found = []
    for path in tracked_files():
        try:
            data = path.read_text(encoding="utf-8", errors="ignore")
        except (FileNotFoundError, IsADirectoryError):
            continue
        for kind, rx in compiled.items():
            m = rx.search(data)
            if m:
                found.append(f"{path.relative_to(REPO)}: {kind} (len {len(m.group(0))})")
    return found


def test_no_credentials_in_tracked_files():
    found = credential_findings()
    assert not found, "похоже на учётные данные (ротация ДО удаления из истории):\n" + "\n".join(found)


@pytest.mark.parametrize("sample,kind", [
    (_B + "PRIVATE KEY-----\n" + "A" * 80, "PEM private key with body"),
    ('{"private_key": "' + _B + 'PRIVATE KEY-----"}', "GCP service-account key JSON"),
    ("gh" + "p_" + "a" * 36, "GitHub token"),
    ("AI" + "za" + "b" * 35, "Google API key"),
    ("123456789:A" + "A" + "c" * 33, "Telegram bot token"),
    ("eyJ" + "a" * 30 + ".eyJ" + "b" * 120 + "." + "c" * 30, "long JWT (WB API token format)"),
])
def test_credential_patterns_detect_samples(sample, kind):
    assert re.search(CREDENTIAL_PATTERNS[kind], sample)


@pytest.mark.parametrize("sample", [
    _B + "PRIVATE KEY-----",                 # заголовок без тела — фикстура редактора
    "ya29.TEST-TOKEN",                        # синтетика тестов verify_current_sql_live
    "https://x-access-token:${GH_TOKEN}@github.com/o/r.git",
])
def test_credential_patterns_ignore_known_fixtures(sample):
    assert not any(re.search(rx, sample) for rx in CREDENTIAL_PATTERNS.values())


@pytest.mark.parametrize("pattern", [".env", "*.pem", "*.p12", "*.key", "gha-creds-*.json",
                                     "*service-account*.json", "*.tfstate", ".security-private/"])
def test_gitignore_blocks_credential_and_private_files(pattern):
    assert pattern in text(REPO / ".gitignore").splitlines()


def test_codeowners_covers_security_sensitive_paths():
    co = text(REPO / ".github" / "CODEOWNERS")
    for path in ("/.github/", "/infra/", "/tools/autonomy/", "/tools/tenancy/", "/tenants/", "/sql/current/"):
        assert re.search(rf"^{re.escape(path)}\S*\s+@evelagin\b", co, re.M), path
