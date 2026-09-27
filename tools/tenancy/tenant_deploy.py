"""Tenancy T4 — применение ЗАМОРОЖЕННОГО плана арендатора (tenant-infra.yml, operation=apply).

Путь доверия не меняется: GitHub Actions → WIF (тот же провайдер, тот же workflow-файл,
только main, только workflow_dispatch) → sa-tenant-provisioner → проект арендатора.

Две стороны:

  package-plan <tenant_id> <work_dir>   — в job'е plan, после tenant_infra.py plan:
      пересчитывает сканер по plan.json, считает дельту, хеши файлов, пишет
      deploy-metadata.json. Артефакт выгружает закреплённый upload-artifact.

  fetch <tenant_id> <out_dir>           — в job'е verify, где id-token НЕТ (нечем получить WIF):
      провенанс прогона-источника через API GitHub (тот же репозиторий по числовому id,
      этот workflow-файл, workflow_dispatch, main, success, коммит = ожидаемый = текущий
      HEAD main), ровно один артефакт с ожидаемым именем, не истёк, не старше
      MAX_ARTIFACT_AGE, zip совпадает с дайджестом GitHub, в zip ровно четыре файла,
      метаданные сходятся с API, входами оператора, файлами, свежим контрактом реестра,
      lock-файлом провайдеров и раннером; секретоподобного содержимого нет. Проверенные файлы
      передаются job'у apply артефактом этого же прогона.

  apply <tenant_id> <verified_dir>      — в job'е apply (needs: verify), после аутентификации WIF:
      повторная сверка хешей, init того же backend (-lockfile=readonly), show -json из
      БИНАРНОГО плана = plan.json артефакта байт в байт, сканер 0, дельта = ожидаемая,
      хеш бинарного плана ещё раз — и `terraform apply <tenant.tfplan>`.

`terraform plan` в пути apply не вызывается никогда: применяется ровно проверенный файл, а
устаревший план Terraform отвергает сам («Saved plan is stale»). Любая проверка → DeployError,
код выхода 1; молчаливого восстановления нет.

Одобрение — это сам workflow_dispatch с точными входами (tenant_id, прогон-источник, коммит,
sha256 плана и контракта, дельта), и запустить apply может только владелец репозитория
(GITHUB_ACTOR_ID) и только первой попыткой прогона: «Re-run» чужого или прежнего одобрения
отвергается. Второго человека GitHub Free (приватный репозиторий) не обеспечивает: environments
с обязательными ревьюерами недоступны, main не защищённая ветка, и мы этого не имитируем.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tools.tenancy import platform as PL  # noqa: E402

SCHEMA = "vts.tenant-plan-artifact.v1"
TFPLAN, PLAN_JSON, CONTRACT, METADATA = "tenant.tfplan", "plan.json", "contract.auto.tfvars.json", "deploy-metadata.json"
ARTIFACT_FILES = (TFPLAN, PLAN_JSON, CONTRACT, METADATA)
ARTIFACT_RETENTION_DAYS = 3
MAX_ARTIFACT_AGE = timedelta(hours=72)
MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_ZIP_BYTES = 64 * 1024 * 1024
LOCK_FILE = REPO / "infra" / "tenant" / ".terraform.lock.hcl"
TENANT_ROOT = REPO / "infra" / "tenant"
SOURCE_WORKFLOW_REF = f"{PL.GITHUB_REPOSITORY}/{PL.TENANT_INFRA_WORKFLOW}@refs/heads/main"

TENANT_RE = re.compile(r"[a-z][a-z0-9_]{2,30}")
RUN_ID_RE = re.compile(r"[1-9][0-9]{0,19}")
COMMIT_RE = re.compile(r"[0-9a-f]{40}")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
DELTA_RE = re.compile(r"([0-9]{1,4})/([0-9]{1,4})/([0-9]{1,4})")

SECRET_PATTERNS = {
    "private key": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH |ENCRYPTED )?PRIVATE KEY-----"),
    "service account key": re.compile(rb'"private_key(?:_id)?"\s*:'),
    "google oauth token": re.compile(rb"ya29\.[0-9A-Za-z_\-]{20,}"),
    "google api key": re.compile(rb"AIza[0-9A-Za-z_\-]{35}"),
    "github token": re.compile(rb"(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{40,})"),
    "external account credentials": re.compile(rb'"type"\s*:\s*"external_account"'),
    "access token field": re.compile(rb'"access_token"\s*:\s*"[^"]{8,}'),
    "client secret field": re.compile(rb'"client_secret"\s*:\s*"[^"]{8,}'),
}


class DeployError(RuntimeError):
    """Проверка не пройдена: применения не будет."""


def _fail(msg: str):
    raise DeployError(msg)


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_file(p: Path) -> str:
    return sha256_bytes(p.read_bytes())


def _json(text: str | bytes):
    from tools.tenancy.validation import parse_tenant_json   # единый строгий разборщик JSON
    return parse_tenant_json(text.decode("utf-8") if isinstance(text, bytes) else text)


# ═══════════════════════════════════════ входы оператора
@dataclass(frozen=True)
class ApplyInputs:
    tenant_id: str
    source_run_id: str
    expected_source_commit: str
    expected_tfplan_sha256: str
    expected_contract_sha256: str
    expected_delta: str

    def delta(self) -> dict:
        m = DELTA_RE.fullmatch(self.expected_delta)
        return {"add": int(m.group(1)), "change": int(m.group(2)), "destroy": int(m.group(3))}


def parse_inputs(env: dict) -> ApplyInputs:
    """Входы apply — только из окружения, строго по форме, без пробелов и переводов строк."""
    def need(name: str, rx: re.Pattern) -> str:
        v = env.get(name, "")
        if not isinstance(v, str) or not rx.fullmatch(v):
            _fail(f"вход {name} отсутствует или имеет недопустимую форму")
        return v
    return ApplyInputs(
        tenant_id=need("TENANT_ID", TENANT_RE),
        source_run_id=need("SOURCE_RUN_ID", RUN_ID_RE),
        expected_source_commit=need("EXPECTED_SOURCE_COMMIT", COMMIT_RE),
        expected_tfplan_sha256=need("EXPECTED_TFPLAN_SHA256", SHA256_RE),
        expected_contract_sha256=need("EXPECTED_CONTRACT_SHA256", SHA256_RE),
        expected_delta=need("EXPECTED_DELTA", DELTA_RE),
    )


def check_invocation(env: dict, inputs: ApplyInputs) -> None:
    """Прогон apply: этот репозиторий, main, workflow_dispatch, этот workflow-файл; коммит
    источника обязан быть ровно текущим HEAD main (правило для первого развёртывания и далее:
    если main ушёл вперёд — новый план)."""
    if env.get("GITHUB_REPOSITORY") != PL.GITHUB_REPOSITORY:
        _fail("чужой репозиторий")
    if env.get("GITHUB_REPOSITORY_ID") != PL.GITHUB_REPOSITORY_ID:
        _fail("числовой id репозитория не совпадает")
    if env.get("GITHUB_REF") != "refs/heads/main" or env.get("GITHUB_EVENT_NAME") != "workflow_dispatch":
        _fail("apply только из refs/heads/main и только по workflow_dispatch")
    if env.get("GITHUB_WORKFLOW_REF") != SOURCE_WORKFLOW_REF:
        _fail("apply только из tenant-infra.yml@refs/heads/main")
    if env.get("GITHUB_SHA") != inputs.expected_source_commit:
        _fail("коммит плана не равен текущему HEAD main: main ушёл вперёд — нужен новый план")
    if env.get("GITHUB_RUN_ATTEMPT") != "1":
        _fail("apply только первой попыткой прогона: повторный запуск не переносит одобрение")
    if env.get("GITHUB_ACTOR_ID") != PL.GITHUB_OWNER_ID:
        _fail("apply запускает только владелец репозитория")


# ═══════════════════════════════════════ план → метаданные (сторона plan)
def plan_delta(plan: dict) -> dict:
    """Счёт как у Terraform (replace = add + destroy) и запрещённые действия отдельно."""
    d = {"add": 0, "change": 0, "destroy": 0, "forbidden": []}
    for rc in plan.get("resource_changes") or []:
        acts = list(rc.get("change", {}).get("actions") or [])
        addr = rc.get("address", "?")
        if acts in (["no-op"], ["read"]):
            continue
        if acts == ["create"]:
            d["add"] += 1
        elif acts == ["update"]:
            d["change"] += 1
        elif acts == ["delete"]:
            d["destroy"] += 1
            d["forbidden"].append(f"{addr}: delete")
        elif sorted(acts) == ["create", "delete"]:
            d["add"] += 1
            d["destroy"] += 1
            d["forbidden"].append(f"{addr}: replace")
        else:
            d["forbidden"].append(f"{addr}: {acts}")
    for rc in plan.get("resource_changes") or []:
        if rc.get("change", {}).get("importing") or rc.get("previous_address"):
            d["forbidden"].append(f"{rc.get('address')}: import/move")
        ch = rc.get("change", {})
        if _any_true(ch.get("before_sensitive")) or _any_true(ch.get("after_sensitive")):
            d["forbidden"].append(f"{rc.get('address')}: чувствительные значения в плане")
    for k in ("action_invocations", "deferred_changes", "deferred_action_invocations"):
        if plan.get(k):
            d["forbidden"].append(f"{k}: не пусто")
    if plan.get("errored") is True or plan.get("complete") is False:
        d["forbidden"].append("план с ошибкой или неполный")
    for name, oc in (plan.get("output_changes") or {}).items():
        if _any_true(oc.get("after_sensitive")) or _any_true(oc.get("before_sensitive")):
            d["forbidden"].append(f"output {name}: чувствительный")
    def walk(mod):
        for r in mod.get("resources") or []:
            if _any_true(r.get("sensitive_values")):
                d["forbidden"].append(f"state {r.get('address')}: чувствительные значения")
        for c in mod.get("child_modules") or []:
            walk(c)
    walk(((plan.get("prior_state") or {}).get("values") or {}).get("root_module") or {})
    return d


def _any_true(x) -> bool:
    if x is True:
        return True
    if isinstance(x, dict):
        return any(_any_true(v) for v in x.values())
    if isinstance(x, list):
        return any(_any_true(v) for v in x)
    return False


def _git_tree(sha_ref: str = "HEAD") -> str:
    return subprocess.run(["git", "-C", str(REPO), "rev-parse", f"{sha_ref}^{{tree}}"],
                          capture_output=True, text=True, check=True).stdout.strip()


def _tf_version() -> dict:
    out = subprocess.run(["terraform", "version", "-json"], capture_output=True, text=True, check=True).stdout
    v = _json(out)
    return {"terraform_version": v["terraform_version"], "platform": v["platform"]}


def build_metadata(tenant_id: str, work_dir: Path, env: dict, contract: dict, *,
                   tree_sha: str, tf: dict, now: datetime) -> dict:
    from tools.tenancy.plan_scan import scan_plan
    plan = _json((work_dir / PLAN_JSON).read_bytes())
    findings = scan_plan(plan, contract)
    delta = plan_delta(plan)
    if findings:
        _fail(f"сканер плана: нарушений {len(findings)} — артефакт не создаётся")
    if delta["forbidden"]:
        _fail(f"запрещённые действия в плане: {delta['forbidden']}")
    for name in ("GITHUB_REPOSITORY", "GITHUB_REPOSITORY_ID", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT",
                 "GITHUB_SHA", "GITHUB_WORKFLOW_REF", "RUNNER_OS", "RUNNER_ARCH"):
        if not env.get(name):
            _fail(f"нет {name}: метаданные артефакта создаются только в GitHub Actions")
    return {
        "schema": SCHEMA,
        "tenant_id": tenant_id,
        "project_id": contract["project_id"],
        "repository": env["GITHUB_REPOSITORY"],
        "repository_id": env["GITHUB_REPOSITORY_ID"],
        "source_workflow_ref": env["GITHUB_WORKFLOW_REF"],
        "source_run_id": env["GITHUB_RUN_ID"],
        "source_run_attempt": env["GITHUB_RUN_ATTEMPT"],
        "source_commit_sha": env["GITHUB_SHA"],
        "tree_sha": tree_sha,
        "contract_sha256": sha256_file(work_dir / CONTRACT),
        "tfplan_sha256": sha256_file(work_dir / TFPLAN),
        "plan_json_sha256": sha256_file(work_dir / PLAN_JSON),
        "terraform_version": tf["terraform_version"],
        "terraform_platform": tf["platform"],
        "provider_lock_sha256": sha256_file(LOCK_FILE),
        "runner": {"os": env["RUNNER_OS"], "arch": env["RUNNER_ARCH"], "image": env.get("ImageOS", "")},
        "scanner": {"violations": 0},
        "delta": {k: delta[k] for k in ("add", "change", "destroy")},
        "created_at": now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def artifact_name(tenant_id: str, run_id: str, attempt: str) -> str:
    return f"tenant-plan-{tenant_id}-{run_id}-{attempt}"


def package_plan(tenant_id: str, work_dir: Path, env: dict | None = None) -> dict:
    from tools.tenancy.tenant_infra import contract_for
    env = dict(os.environ if env is None else env)
    contract = contract_for(tenant_id)
    for f in (TFPLAN, PLAN_JSON, CONTRACT):
        if not (work_dir / f).is_file():
            _fail(f"нет {f} в {work_dir}")
    meta = build_metadata(tenant_id, work_dir, env, contract, tree_sha=_git_tree(), tf=_tf_version(),
                          now=datetime.now(timezone.utc))
    blob = (json.dumps(meta, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    (work_dir / METADATA).write_bytes(blob)
    scan_artifact_for_secrets({f: (work_dir / f).read_bytes() for f in ARTIFACT_FILES})
    print(f"artifact:              {artifact_name(tenant_id, meta['source_run_id'], meta['source_run_attempt'])}")
    for k in ("source_run_id", "source_run_attempt", "source_commit_sha", "tree_sha", "contract_sha256",
              "tfplan_sha256", "plan_json_sha256", "provider_lock_sha256", "terraform_version"):
        print(f"{k:22} {meta[k]}")
    print(f"{'metadata_sha256':22} {sha256_bytes(blob)}")
    print(f"{'delta':22} {meta['delta']['add']}/{meta['delta']['change']}/{meta['delta']['destroy']}")
    if env.get("GITHUB_OUTPUT"):
        with open(env["GITHUB_OUTPUT"], "a", encoding="utf-8") as fh:
            fh.write(f"artifact_name={artifact_name(tenant_id, meta['source_run_id'], meta['source_run_attempt'])}\n")
    return meta


# ═══════════════════════════════════════ проверки артефакта (сторона apply)
def scan_artifact_for_secrets(files: dict[str, bytes]) -> None:
    """Секретоподобное содержимое в любом файле артефакта, включая члены zip плана."""
    blobs = dict(files)
    if TFPLAN in files:
        try:
            with zipfile.ZipFile(io.BytesIO(files[TFPLAN])) as z:
                for n in z.namelist():
                    blobs[f"{TFPLAN}!{n}"] = z.read(n)
        except zipfile.BadZipFile:
            _fail("tenant.tfplan не является файлом плана Terraform (zip)")
    for name, data in blobs.items():
        for kind, rx in SECRET_PATTERNS.items():
            if rx.search(data):
                _fail(f"секретоподобное содержимое ({kind}) в {name}")


def check_source_run(run: dict, inputs: ApplyInputs) -> str:
    """Провенанс прогона-источника по API GitHub. Возвращает номер попытки."""
    if str(run.get("id")) != inputs.source_run_id:
        _fail("API вернул другой прогон")
    if str((run.get("repository") or {}).get("id")) != PL.GITHUB_REPOSITORY_ID:
        _fail("прогон-источник из чужого репозитория")
    if run.get("path") != PL.TENANT_INFRA_WORKFLOW:
        _fail(f"прогон-источник не из {PL.TENANT_INFRA_WORKFLOW}")
    if run.get("event") != "workflow_dispatch" or run.get("head_branch") != "main":
        _fail("прогон-источник не workflow_dispatch из main")
    if run.get("head_sha") != inputs.expected_source_commit:
        _fail("коммит прогона-источника не равен ожидаемому")
    if run.get("status") != "completed" or run.get("conclusion") != "success":
        _fail("прогон-источник не завершён успешно")
    attempt = str(run.get("run_attempt", ""))
    if not RUN_ID_RE.fullmatch(attempt):
        _fail("нет номера попытки прогона-источника")
    return attempt


def select_artifact(artifacts: list[dict], inputs: ApplyInputs, attempt: str, now: datetime) -> dict:
    want = artifact_name(inputs.tenant_id, inputs.source_run_id, attempt)
    same = [a for a in artifacts if a.get("name") == want]
    if len(same) != 1:
        _fail(f"артефакт {want}: найдено {len(same)}, нужен ровно один")
    a = same[0]
    if a.get("expired"):
        _fail("артефакт истёк")
    created = datetime.strptime(a.get("created_at", ""), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    if now - created > MAX_ARTIFACT_AGE or created > now + timedelta(minutes=5):
        _fail(f"артефакт вне окна свежести {MAX_ARTIFACT_AGE}")
    if not isinstance(a.get("size_in_bytes"), int) or a["size_in_bytes"] > MAX_ZIP_BYTES:
        _fail("размер артефакта неизвестен или больше предела")
    digest = a.get("digest") or ""
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        _fail("у артефакта нет дайджеста sha256 от GitHub")
    return a


def unpack_artifact(zip_bytes: bytes, digest: str) -> dict[str, bytes]:
    if len(zip_bytes) > MAX_ZIP_BYTES:
        _fail("zip артефакта больше предела")
    if "sha256:" + sha256_bytes(zip_bytes) != digest:
        _fail("zip артефакта не совпадает с дайджестом GitHub")
    try:
        z = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile:
        _fail("артефакт — не zip")
    names = z.namelist()
    if sorted(names) != sorted(ARTIFACT_FILES):
        _fail(f"состав артефакта {sorted(names)} != {sorted(ARTIFACT_FILES)}")
    out = {}
    for info in z.infolist():
        if info.file_size > MAX_FILE_BYTES or info.is_dir():
            _fail(f"{info.filename}: недопустимый член артефакта")
        out[info.filename] = z.read(info.filename)
    return out


def verify_metadata(files: dict[str, bytes], inputs: ApplyInputs, attempt: str, env: dict, *,
                    fresh_contract: bytes, lock_bytes: bytes, tree_sha: str) -> dict:
    meta = _json(files[METADATA])
    if not isinstance(meta, dict) or meta.get("schema") != SCHEMA:
        _fail("метаданные: неизвестная схема")
    expect = {
        "tenant_id": inputs.tenant_id,
        "repository": PL.GITHUB_REPOSITORY,
        "repository_id": PL.GITHUB_REPOSITORY_ID,
        "source_workflow_ref": SOURCE_WORKFLOW_REF,
        "source_run_id": inputs.source_run_id,
        "source_run_attempt": attempt,
        "source_commit_sha": inputs.expected_source_commit,
        "tree_sha": tree_sha,
        "tfplan_sha256": inputs.expected_tfplan_sha256,
        "contract_sha256": inputs.expected_contract_sha256,
        "provider_lock_sha256": sha256_bytes(lock_bytes),
    }
    for k, v in expect.items():
        if meta.get(k) != v:
            _fail(f"метаданные: {k} не совпадает с ожидаемым")
    for f, k in ((TFPLAN, "tfplan_sha256"), (PLAN_JSON, "plan_json_sha256"), (CONTRACT, "contract_sha256")):
        if sha256_bytes(files[f]) != meta.get(k):
            _fail(f"{f}: хеш не совпадает с метаданными")
    if sha256_bytes(fresh_contract) != inputs.expected_contract_sha256:
        _fail("контракт реестра на текущем main не совпадает с ожидаемым")
    if meta.get("scanner") != {"violations": 0}:
        _fail("сканер плана в метаданных не 0")
    if meta.get("delta") != inputs.delta():
        _fail("дельта плана в метаданных не равна ожидаемой")
    runner = meta.get("runner") or {}
    if runner.get("os") != env.get("RUNNER_OS") or runner.get("arch") != env.get("RUNNER_ARCH"):
        _fail("ОС/архитектура раннера отличаются от раннера плана")
    with zipfile.ZipFile(io.BytesIO(files[TFPLAN])) as z:
        if ".terraform.lock.hcl" not in z.namelist() or z.read(".terraform.lock.hcl") != lock_bytes:
            _fail("lock провайдеров внутри плана не равен закоммиченному")
    return meta


class GitHubApi:
    """Только чтение: прогон, артефакты прогона, zip артефакта. Токен — GITHUB_TOKEN с actions:read;
    на хранилище, куда GitHub перенаправляет zip, токен не отправляется."""

    def __init__(self, token: str, api: str = "https://api.github.com"):
        self.token, self.api = token, api.rstrip("/")

    def _get(self, url: str, auth: bool = True, follow: bool = False) -> tuple[int, bytes, dict]:
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None
        opener = urllib.request.build_opener(NoRedirect)
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if auth:
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            with opener.open(urllib.request.Request(url, headers=headers), timeout=120) as r:
                return r.status, r.read(), dict(r.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read(), dict(e.headers)

    def run(self, run_id: str) -> dict:
        code, body, _ = self._get(f"{self.api}/repos/{PL.GITHUB_REPOSITORY}/actions/runs/{run_id}")
        if code != 200:
            _fail(f"API прогона: HTTP {code}")
        return _json(body)

    def main_head(self) -> str:
        code, body, _ = self._get(f"{self.api}/repos/{PL.GITHUB_REPOSITORY}/git/ref/heads/main")
        if code != 200:
            _fail(f"API ветки main: HTTP {code}")
        return str((_json(body).get("object") or {}).get("sha", ""))

    def artifacts(self, run_id: str) -> list[dict]:
        code, body, _ = self._get(f"{self.api}/repos/{PL.GITHUB_REPOSITORY}/actions/runs/{run_id}/artifacts?per_page=100")
        if code != 200:
            _fail(f"API артефактов: HTTP {code}")
        return _json(body).get("artifacts") or []

    def artifact_zip(self, artifact_id: int) -> bytes:
        code, body, headers = self._get(f"{self.api}/repos/{PL.GITHUB_REPOSITORY}/actions/artifacts/{artifact_id}/zip")
        if code != 302 or not headers.get("Location", "").startswith("https://"):
            _fail(f"скачивание артефакта: HTTP {code}")
        code, body, _ = self._get(headers["Location"], auth=False)
        if code != 200:
            _fail(f"скачивание zip: HTTP {code}")
        return body


def fetch(tenant_id: str, out_dir: Path, env: dict | None = None, api=None, now: datetime | None = None) -> dict:
    from tools.tenancy.tenant_infra import render
    env = dict(os.environ if env is None else env)
    inputs = parse_inputs(env)
    if inputs.tenant_id != tenant_id:
        _fail("tenant_id аргумента и входа различаются")
    check_invocation(env, inputs)
    api = api or GitHubApi(env.get("GITHUB_TOKEN", ""), env.get("GITHUB_API_URL", "https://api.github.com"))
    now = now or datetime.now(timezone.utc)
    if api.main_head() != inputs.expected_source_commit:
        _fail("живой HEAD main не равен коммиту плана: main ушёл вперёд — нужен новый план")
    attempt = check_source_run(api.run(inputs.source_run_id), inputs)
    art = select_artifact(api.artifacts(inputs.source_run_id), inputs, attempt, now)
    files = unpack_artifact(api.artifact_zip(art["id"]), art["digest"])
    scan_artifact_for_secrets(files)
    fresh = out_dir.parent / f"{out_dir.name}-fresh-contract"
    render(tenant_id, fresh)
    meta = verify_metadata(files, inputs, attempt, env, fresh_contract=(fresh / CONTRACT).read_bytes(),
                           lock_bytes=LOCK_FILE.read_bytes(), tree_sha=_git_tree())
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        (out_dir / name).write_bytes(data)
    print(f"артефакт {art['name']} (id {art['id']}, {art['digest']}) проверен")
    for k in ("source_run_id", "source_run_attempt", "source_commit_sha", "tfplan_sha256", "contract_sha256",
              "plan_json_sha256"):
        print(f"{k:22} {meta[k]}")
    print(f"{'metadata_sha256':22} {sha256_bytes(files[METADATA])}")
    return meta


# ═══════════════════════════════════════ применение
def _tf(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    if args and args[0] == "plan":
        _fail("terraform plan в пути apply запрещён")
    if args and args[0] == "apply":
        if len(args) < 2 or not args[-1].endswith(TFPLAN):
            _fail("terraform apply только с файлом плана")
        for bad in ("-target", "-replace", "-destroy", "-refresh-only", "-auto-approve", "-var", "-var-file"):
            if any(a == bad or a.startswith(bad + "=") for a in args):
                _fail(f"terraform apply {bad} запрещён")
    env = {k: v for k, v in os.environ.items() if not k.startswith("TF_")}   # TF_CLI_ARGS*, TF_CLI_CONFIG_FILE…
    proc = subprocess.run(["terraform", *args], cwd=cwd, text=True, capture_output=True, env=env)
    if args[:1] != ["show"]:
        sys.stdout.write(proc.stdout)
    sys.stderr.write(proc.stderr)
    if proc.returncode != 0:
        _fail(f"terraform {args[0]} завершился с кодом {proc.returncode}")
    return proc


def apply(tenant_id: str, verified_dir: Path, env: dict | None = None, tf=_tf) -> int:
    from tools.tenancy.plan_scan import scan_plan
    from tools.tenancy.tenant_infra import contract_for
    env = dict(os.environ if env is None else env)
    inputs = parse_inputs(env)
    if inputs.tenant_id != tenant_id:
        _fail("tenant_id аргумента и входа различаются")
    check_invocation(env, inputs)
    if sorted(p.name for p in verified_dir.iterdir()) != sorted(ARTIFACT_FILES):
        _fail("в каталоге проверенного плана не ровно четыре файла артефакта")
    meta = _json((verified_dir / METADATA).read_bytes())
    for f, k in ((TFPLAN, "tfplan_sha256"), (PLAN_JSON, "plan_json_sha256"), (CONTRACT, "contract_sha256")):
        if sha256_file(verified_dir / f) != meta.get(k):
            _fail(f"{f} изменён после проверки")
    if meta.get("tfplan_sha256") != inputs.expected_tfplan_sha256:
        _fail("хеш плана не равен ожидаемому")
    contract = contract_for(tenant_id)
    tfplan = str((verified_dir / TFPLAN).resolve())
    tf(["init", "-input=false", "-reconfigure", "-lockfile=readonly",
        f"-backend-config=bucket={contract['state']['bucket']}",
        f"-backend-config=prefix={contract['state']['prefix']}"], TENANT_ROOT)
    shown = tf(["show", "-json", tfplan], TENANT_ROOT).stdout
    if sha256_bytes(shown.encode("utf-8")) != meta.get("plan_json_sha256"):
        _fail("show -json бинарного плана не равен plan.json артефакта")
    plan = _json(shown)
    findings = scan_plan(plan, contract)
    if findings:
        _fail(f"сканер плана: нарушений {len(findings)}")
    delta = plan_delta(plan)
    if delta["forbidden"]:
        _fail(f"запрещённые действия: {delta['forbidden']}")
    if {k: delta[k] for k in ("add", "change", "destroy")} != inputs.delta():
        _fail(f"дельта плана {delta} не равна ожидаемой {inputs.expected_delta}")
    if sha256_file(verified_dir / TFPLAN) != inputs.expected_tfplan_sha256:
        _fail("хеш плана изменился перед apply")
    print(f"применяется {TFPLAN} sha256 {inputs.expected_tfplan_sha256}, дельта {inputs.expected_delta}")
    tf(["apply", "-input=false", "-lock-timeout=120s", tfplan], TENANT_ROOT)
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[0] not in ("package-plan", "fetch", "apply"):
        print(__doc__, file=sys.stderr)
        return 3
    try:
        if argv[0] == "package-plan":
            package_plan(argv[1], Path(argv[2]))
        elif argv[0] == "fetch":
            fetch(argv[1], Path(argv[2]))
        else:
            apply(argv[1], Path(argv[2]))
        return 0
    except DeployError as e:
        print(f"FAIL {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
