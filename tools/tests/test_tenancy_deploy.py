"""Tenancy T4 — путь применения замороженного плана и развёртывания SQL: состязательные тесты.

Офлайн. API GitHub, Terraform и BigQuery подменяются; проверяется, что каждая подмена
артефакта, входа, провенанса или плана останавливает применение (fail closed), а в пути
apply нет `terraform plan` и нет apply без файла плана.
"""
from __future__ import annotations

import ast
import copy
import hashlib
import io
import json
import re
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from tools.tenancy import platform as PL  # noqa: E402
from tools.tenancy import sql_deploy as SD  # noqa: E402
from tools.tenancy import synthetic as SY  # noqa: E402
from tools.tenancy import tenant_deploy as D  # noqa: E402
from tools.tenancy import tenant_infra as TI  # noqa: E402
from tools.tests.test_tenancy_t32_remediation import _plan_for, _rc  # noqa: E402

WF = (REPO / ".github" / "workflows" / "tenant-infra.yml").read_text(encoding="utf-8")
TENANT = "client_001"
CONTRACT_OBJ = SY.fixture_contract(TENANT)
COMMIT = "a" * 40
RUN_ID = "123456789"
NOW = datetime(2026, 9, 27, 20, 0, tzinfo=timezone.utc)
LOCK = D.LOCK_FILE.read_bytes()
TREE = D._git_tree()


def _plan_bytes(plan: dict) -> bytes:
    return (json.dumps(plan, sort_keys=True) + "\n").encode()


def _tfplan(lock: bytes = LOCK, extra: dict | None = None) -> bytes:
    buf = io.BytesIO()
    members = {"tfplan": b"\x08\x01binary-plan", "tfstate": b"{}", ".terraform.lock.hcl": lock}
    members.update(extra or {})
    with zipfile.ZipFile(buf, "w") as z:
        for k, v in members.items():
            z.writestr(k, v)
    return buf.getvalue()


PLAN = _plan_for(CONTRACT_OBJ)
DELTA = D.plan_delta(PLAN)
DELTA_S = f"{DELTA['add']}/{DELTA['change']}/{DELTA['destroy']}"
CONTRACT_BYTES = (json.dumps(CONTRACT_OBJ, sort_keys=True) + "\n").encode()


def _files(plan: dict = PLAN, tfplan: bytes | None = None, meta_edit=None, drop: str | None = None,
           add: dict | None = None) -> dict[str, bytes]:
    files = {D.TFPLAN: tfplan or _tfplan(), D.PLAN_JSON: _plan_bytes(plan), D.CONTRACT: CONTRACT_BYTES}
    meta = {
        "schema": D.SCHEMA, "tenant_id": TENANT, "project_id": CONTRACT_OBJ["project_id"],
        "repository": PL.GITHUB_REPOSITORY, "repository_id": PL.GITHUB_REPOSITORY_ID,
        "source_workflow_ref": D.SOURCE_WORKFLOW_REF, "source_run_id": RUN_ID, "source_run_attempt": "1",
        "source_commit_sha": COMMIT, "tree_sha": TREE,
        "contract_sha256": D.sha256_bytes(files[D.CONTRACT]), "tfplan_sha256": D.sha256_bytes(files[D.TFPLAN]),
        "plan_json_sha256": D.sha256_bytes(files[D.PLAN_JSON]), "terraform_version": "1.15.8",
        "terraform_platform": "linux_amd64", "provider_lock_sha256": D.sha256_bytes(LOCK),
        "runner": {"os": "Linux", "arch": "X64", "image": "ubuntu24"}, "scanner": {"violations": 0},
        "delta": {k: DELTA[k] for k in ("add", "change", "destroy")}, "created_at": "2026-09-27T19:00:00Z"}
    if meta_edit:
        meta_edit(meta)
    files[D.METADATA] = (json.dumps(meta, sort_keys=True) + "\n").encode()
    if drop:
        files.pop(drop)
    files.update(add or {})
    return files


def _zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for k, v in files.items():
            z.writestr(k, v)
    return buf.getvalue()


def _env(**over) -> dict:
    files = _files()
    env = {"TENANT_ID": TENANT, "SOURCE_RUN_ID": RUN_ID, "EXPECTED_SOURCE_COMMIT": COMMIT,
           "EXPECTED_TFPLAN_SHA256": D.sha256_bytes(files[D.TFPLAN]),
           "EXPECTED_CONTRACT_SHA256": D.sha256_bytes(CONTRACT_BYTES), "EXPECTED_DELTA": DELTA_S,
           "GITHUB_REPOSITORY": PL.GITHUB_REPOSITORY, "GITHUB_REPOSITORY_ID": PL.GITHUB_REPOSITORY_ID,
           "GITHUB_REF": "refs/heads/main", "GITHUB_EVENT_NAME": "workflow_dispatch",
           "GITHUB_WORKFLOW_REF": D.SOURCE_WORKFLOW_REF, "GITHUB_SHA": COMMIT, "RUNNER_OS": "Linux",
           "RUNNER_ARCH": "X64", "GITHUB_RUN_ATTEMPT": "1", "GITHUB_ACTOR_ID": PL.GITHUB_OWNER_ID}
    env.update(over)
    return env


class FakeApi:
    def __init__(self, files=None, run=None, artifacts=None, zip_bytes=None, head=COMMIT):
        self.head = head
        self.files = files if files is not None else _files()
        self.zip = zip_bytes if zip_bytes is not None else _zip(self.files)
        self._run = {"id": int(RUN_ID), "repository": {"id": int(PL.GITHUB_REPOSITORY_ID)},
                     "path": PL.TENANT_INFRA_WORKFLOW, "event": "workflow_dispatch", "head_branch": "main",
                     "head_sha": COMMIT, "status": "completed", "conclusion": "success", "run_attempt": 1}
        self._run.update(run or {})
        self._arts = artifacts if artifacts is not None else [{
            "id": 77, "name": D.artifact_name(TENANT, RUN_ID, "1"), "expired": False,
            "created_at": "2026-09-27T19:00:00Z", "digest": "sha256:" + D.sha256_bytes(self.zip),
            "size_in_bytes": len(self.zip)}]

    def main_head(self):
        return self.head

    def run(self, run_id):
        return copy.deepcopy(self._run)

    def artifacts(self, run_id):
        return copy.deepcopy(self._arts)

    def artifact_zip(self, artifact_id):
        return self.zip


@pytest.fixture(autouse=True)
def _fixture_contract(monkeypatch):
    monkeypatch.setattr(TI, "contract_for", lambda t: copy.deepcopy(CONTRACT_OBJ))

    def render(t, out):
        Path(out).mkdir(parents=True, exist_ok=True)
        (Path(out) / D.CONTRACT).write_bytes(CONTRACT_BYTES)
        return CONTRACT_OBJ
    monkeypatch.setattr(TI, "render", render)


def _fetch(tmp_path, env=None, api=None, now=NOW):
    return D.fetch(TENANT, tmp_path / "frozen", env=env or _env(), api=api or FakeApi(), now=now)


def _fails(fn, *a, match: str = "", **k):
    with pytest.raises(D.DeployError) as e:
        fn(*a, **k)
    assert match in str(e.value), str(e.value)
    return str(e.value)


# ═══════════════════════════════════════ счастливый путь
def test_valid_frozen_artifact_is_fetched_and_verified(tmp_path):
    meta = _fetch(tmp_path)
    assert meta["tfplan_sha256"] == _env()["EXPECTED_TFPLAN_SHA256"]
    assert sorted(p.name for p in (tmp_path / "frozen").iterdir()) == sorted(D.ARTIFACT_FILES)


# ═══════════════════════════════════════ 20 заданных атак
def test_01_wrong_tenant_id(tmp_path):
    _fails(_fetch, tmp_path, env=_env(TENANT_ID="client_002"), match="tenant_id")


def test_02_wrong_plan_sha(tmp_path):
    _fails(_fetch, tmp_path, env=_env(EXPECTED_TFPLAN_SHA256="0" * 64), match="tfplan_sha256")


def test_03_wrong_contract_sha(tmp_path):
    _fails(_fetch, tmp_path, env=_env(EXPECTED_CONTRACT_SHA256="0" * 64), match="contract_sha256")


def test_04_wrong_source_commit(tmp_path):
    _fails(_fetch, tmp_path, env=_env(EXPECTED_SOURCE_COMMIT="b" * 40), match="HEAD main")
    _fails(_fetch, tmp_path, api=FakeApi(run={"head_sha": "c" * 40}), match="коммит прогона")


def test_05_wrong_repository_id(tmp_path):
    _fails(_fetch, tmp_path, env=_env(GITHUB_REPOSITORY_ID="1111111111"), match="числовой id")
    _fails(_fetch, tmp_path, api=FakeApi(run={"repository": {"id": 1111111111}}), match="чужого репозитория")


def test_06_failed_source_workflow(tmp_path):
    _fails(_fetch, tmp_path, api=FakeApi(run={"conclusion": "failure"}), match="успешно")
    _fails(_fetch, tmp_path, api=FakeApi(run={"status": "in_progress", "conclusion": None}), match="успешно")


def test_07_artifact_from_wrong_workflow(tmp_path):
    _fails(_fetch, tmp_path, api=FakeApi(run={"path": ".github/workflows/ci.yml"}), match="tenant-infra.yml")


def test_08_artifact_from_pr_or_untrusted_ref(tmp_path):
    _fails(_fetch, tmp_path, api=FakeApi(run={"event": "pull_request"}), match="workflow_dispatch")
    _fails(_fetch, tmp_path, api=FakeApi(run={"head_branch": "feature-x"}), match="workflow_dispatch")
    _fails(_fetch, tmp_path, env=_env(GITHUB_REF="refs/heads/feature-x"), match="refs/heads/main")
    _fails(_fetch, tmp_path, env=_env(GITHUB_EVENT_NAME="pull_request"), match="workflow_dispatch")


def test_09_plan_modified_after_hashing(tmp_path):
    files = _files()
    files[D.TFPLAN] = _tfplan(extra={"tfplan-evil": b"x"})              # хеш в метаданных — от прежнего
    _fails(_fetch, tmp_path, api=FakeApi(files=files), match="tenant.tfplan")


def test_09b_plan_modified_between_fetch_and_apply(tmp_path):
    _fetch(tmp_path)
    (tmp_path / "frozen" / D.TFPLAN).write_bytes(_tfplan(extra={"x": b"y"}))
    _fails(D.apply, TENANT, tmp_path / "frozen", env=_env(), tf=_fake_tf(), match="изменён после проверки")


def test_10_modified_metadata(tmp_path):
    files = _files(meta_edit=lambda m: m.update(tree_sha="0" * 40))
    _fails(_fetch, tmp_path, api=FakeApi(files=files), match="tree_sha")
    good = FakeApi()
    tampered = _files(meta_edit=lambda m: m.update(created_at="2030-01-01T00:00:00Z"))
    good.zip = _zip(tampered)                                           # подмена после дайджеста GitHub
    _fails(_fetch, tmp_path, api=good, match="дайджестом")


def test_11_scanner_non_zero(tmp_path):
    files = _files(meta_edit=lambda m: m.update(scanner={"violations": 1}))
    _fails(_fetch, tmp_path, api=FakeApi(files=files), match="сканер")
    bad = copy.deepcopy(PLAN)
    bad["resource_changes"].append(_rc("google_project_iam_member.x", "google_project_iam_member",
                                       {"project": CONTRACT_OBJ["project_id"], "role": "roles/owner",
                                        "member": "user:x@example.com"}))
    _fails(D.build_metadata, TENANT, _workdir(tmp_path, bad), _env(GITHUB_RUN_ID=RUN_ID, GITHUB_RUN_ATTEMPT="1"),
           CONTRACT_OBJ, tree_sha=TREE, tf={"terraform_version": "1.15.8", "platform": "linux_amd64"}, now=NOW,
           match="сканер")


def test_12_missing_metadata(tmp_path):
    _fails(_fetch, tmp_path, api=FakeApi(files=_files(drop=D.METADATA)), match="состав артефакта")


def test_13_stale_or_expired_artifact(tmp_path):
    api = FakeApi()
    api._arts[0]["expired"] = True
    _fails(_fetch, tmp_path, api=api, match="истёк")
    _fails(_fetch, tmp_path, now=NOW + timedelta(hours=73), match="свежести")


def test_14_apply_without_plan_filename_is_impossible():
    for args in (["apply"], ["apply", "-input=false"], ["apply", "-auto-approve", "x/tenant.tfplan"],
                 ["apply", "-target=google_bigquery_dataset.x", "x/tenant.tfplan"],
                 ["apply", "-replace=x", "x/tenant.tfplan"], ["apply", "-destroy", "x/tenant.tfplan"],
                 ["apply", "-var-file=evil.json", "x/tenant.tfplan"]):
        _fails(D._tf, args, REPO)


def test_15_no_terraform_plan_in_apply_path():
    _fails(D._tf, ["plan", "-out=x"], REPO, match="запрещён")
    src = ast.parse((REPO / "tools" / "tenancy" / "tenant_deploy.py").read_text(encoding="utf-8"))
    fn = next(n for n in src.body if isinstance(n, ast.FunctionDef) and n.name == "apply")
    literals = [c.value for c in ast.walk(fn) if isinstance(c, ast.Constant) and isinstance(c.value, str)]
    assert "plan" not in literals and "apply" in literals and "show" in literals and "init" in literals
    for job in ("verify", "apply"):
        assert "tenant_infra.py plan" not in _wf_job(job) and "terraform plan" not in _wf_job(job)


def test_16_unexpected_delete_change_replace_forget(tmp_path):
    for acts in (["delete"], ["delete", "create"], ["create", "delete"], ["forget"]):
        bad = copy.deepcopy(PLAN)
        bad["resource_changes"][1]["change"]["actions"] = acts
        assert D.plan_delta(bad)["forbidden"], acts
    upd = copy.deepcopy(PLAN)
    upd["resource_changes"][1]["change"]["actions"] = ["update"]
    _fetch(tmp_path)
    tf = _fake_tf(shown=_plan_bytes(upd))
    _fails(D.apply, TENANT, tmp_path / "frozen", env=_env(), tf=tf, match="show -json")   # не тот план
    assert D.plan_delta(upd)["change"] == 1                                                  # и дельта ≠ ожидаемой


def test_17_tenant_mismatch_between_artifact_and_request(tmp_path):
    files = _files(meta_edit=lambda m: m.update(tenant_id="client_002"))
    _fails(_fetch, tmp_path, api=FakeApi(files=files), match="tenant_id")


def test_18_provider_lock_mismatch(tmp_path):
    files = _files(tfplan=_tfplan(lock=LOCK + b"\n# other providers\n"))
    files[D.METADATA] = _files(tfplan=files[D.TFPLAN])[D.METADATA]
    env = _env(EXPECTED_TFPLAN_SHA256=D.sha256_bytes(files[D.TFPLAN]))
    _fails(_fetch, tmp_path, env=env, api=FakeApi(files=files), match="lock провайдеров")
    files2 = _files(meta_edit=lambda m: m.update(provider_lock_sha256="0" * 64))
    _fails(_fetch, tmp_path, api=FakeApi(files=files2), match="provider_lock_sha256")


def test_19_arbitrary_artifact_injection(tmp_path):
    _fails(_fetch, tmp_path, api=FakeApi(files=_files(add={"payload.sh": b"curl evil"})), match="состав артефакта")
    _fails(_fetch, tmp_path, api=FakeApi(files=_files(add={"../../etc/x": b"x"})), match="состав артефакта")
    api = FakeApi()
    api._arts.append(dict(api._arts[0], id=78))                         # два артефакта с тем же именем
    _fails(_fetch, tmp_path, api=api, match="ровно один")
    api = FakeApi()
    api._arts[0]["name"] = "tenant-plan-client_001-999-1"
    _fails(_fetch, tmp_path, api=api, match="ровно один")
    api = FakeApi()
    api._arts[0]["digest"] = ""
    _fails(_fetch, tmp_path, api=api, match="дайджеста")


@pytest.mark.parametrize("secret", [b"-----BEGIN PRIVATE KEY-----", b'"private_key": "x"', b"ya29." + b"A" * 30,
                                    b'"type": "external_account"', b"ghp_" + b"a" * 36, b'"access_token": "abcdefghijk"'])
def test_20_secret_like_material_in_artifact(tmp_path, secret):
    tfplan = _tfplan(extra={"tfstate": b'{"x": ' + secret + b"}"})
    files = _files(tfplan=tfplan)
    env = _env(EXPECTED_TFPLAN_SHA256=D.sha256_bytes(tfplan))
    _fails(_fetch, tmp_path, env=env, api=FakeApi(files=files), match="секретоподобное")


# ═══════════════════════════════════════ apply: полный путь на поддельном Terraform
def _workdir(tmp_path, plan):
    w = tmp_path / "w"
    w.mkdir(exist_ok=True)
    (w / D.PLAN_JSON).write_bytes(_plan_bytes(plan))
    (w / D.TFPLAN).write_bytes(_tfplan())
    (w / D.CONTRACT).write_bytes(CONTRACT_BYTES)
    return w


def _fake_tf(shown: bytes | None = None, calls: list | None = None):
    calls = calls if calls is not None else []

    class P:
        def __init__(self, out):
            self.stdout = out

    def tf(args, cwd):
        calls.append(list(args))
        if args[0] == "plan":
            raise AssertionError("plan в пути apply")
        if args[0] == "apply":
            assert args[-1].endswith(D.TFPLAN)
        return P((shown if shown is not None else _plan_bytes(PLAN)).decode())
    tf.calls = calls
    return tf


def test_apply_runs_init_show_apply_exact_file_only(tmp_path):
    _fetch(tmp_path)
    tf = _fake_tf()
    assert D.apply(TENANT, tmp_path / "frozen", env=_env(), tf=tf) == 0
    assert [c[0] for c in tf.calls] == ["init", "show", "apply"]
    assert "-lockfile=readonly" in tf.calls[0] and tf.calls[2][-1].endswith("frozen/tenant.tfplan")


def test_apply_refuses_when_expected_delta_differs(tmp_path):
    _fetch(tmp_path)
    _fails(D.apply, TENANT, tmp_path / "frozen", env=_env(EXPECTED_DELTA="1/0/0"), tf=_fake_tf(), match="дельта")


def test_package_plan_metadata_binds_every_hash(tmp_path):
    w = _workdir(tmp_path, PLAN)
    meta = D.build_metadata(TENANT, w, _env(GITHUB_RUN_ID=RUN_ID, GITHUB_RUN_ATTEMPT="1"), CONTRACT_OBJ,
                            tree_sha=TREE, tf={"terraform_version": "1.15.8", "platform": "linux_amd64"}, now=NOW)
    for k in ("tenant_id", "repository_id", "source_workflow_ref", "source_run_id", "source_run_attempt",
              "source_commit_sha", "tree_sha", "contract_sha256", "tfplan_sha256", "plan_json_sha256",
              "terraform_version", "provider_lock_sha256", "scanner", "delta", "created_at"):
        assert meta.get(k) not in (None, ""), k
    assert meta["delta"] == {k: DELTA[k] for k in ("add", "change", "destroy")}


def test_metadata_is_only_created_inside_github_actions(tmp_path):
    env = {k: v for k, v in _env().items() if k != "GITHUB_WORKFLOW_REF"}
    _fails(D.build_metadata, TENANT, _workdir(tmp_path, PLAN), env, CONTRACT_OBJ, tree_sha=TREE,
           tf={"terraform_version": "1.15.8", "platform": "linux_amd64"}, now=NOW, match="GitHub Actions")


def test_download_never_forwards_the_github_token_to_the_redirect_target(monkeypatch):
    seen = []

    def fake_get(self, url, auth=True, follow=False):
        seen.append((url, auth))
        if url.endswith("/zip"):
            return 302, b"", {"Location": "https://blob.example/zip?sig=1"}
        return 200, b"zipdata", {}
    monkeypatch.setattr(D.GitHubApi, "_get", fake_get)
    assert D.GitHubApi("tok").artifact_zip(5) == b"zipdata"
    assert seen[-1] == ("https://blob.example/zip?sig=1", False)


# ═══════════════════════════════════════ workflow: статические инварианты
def test_workflow_uploads_exactly_the_four_files_with_short_explicit_retention():
    plan_job = _wf_job("plan")
    up = plan_job.split("actions/upload-artifact@", 1)[1]
    assert "retention-days: 3" in up and "if-no-files-found: error" in up
    assert sorted(re.findall(r"/tenant/([a-z.\-]+)\n", up)) == sorted(D.ARTIFACT_FILES)
    assert D.ARTIFACT_RETENTION_DAYS == 3 and D.MAX_ARTIFACT_AGE <= timedelta(days=3)


def _wf_job(name):
    from tools.tests.test_tenancy_t32 import _job
    return _job(WF, name)


def test_workflow_verify_job_has_no_id_token_and_apply_job_has_no_github_token():
    verify, apply_job = _wf_job("verify"), _wf_job("apply")
    code = "\n".join(l for l in verify.splitlines() if not l.strip().startswith("#"))
    assert not re.search(r"(?m)^\s+id-token:", code) and "google-github-actions/auth@" not in code
    assert "    permissions:\n      contents: read\n      actions: read\n" in verify
    assert "tenant_deploy.py fetch" in verify and "GITHUB_TOKEN: ${{ github.token }}" in verify
    assert "tenant_deploy.py fetch" not in apply_job and "github.token" not in apply_job
    assert "actions: read" not in apply_job and "needs: verify" in apply_job
    assert "name: verified-plan-${{ github.run_id }}-${{ github.run_attempt }}" in verify
    assert "name: verified-plan-${{ github.run_id }}-${{ github.run_attempt }}" in apply_job
    assert "retention-days: 1" in verify
    assert apply_job.index("actions/download-artifact@") < apply_job.index("google-github-actions/auth@")
    assert apply_job.index("google-github-actions/auth@") < apply_job.index("tenant_deploy.py apply")
    assert f"workload_identity_provider: {PL.WIF_PROVIDER}" in apply_job
    assert f"service_account: {PL.PROVISIONER_SA}" in apply_job


def test_workflow_runners_are_pinned_for_plan_apply_portability():
    assert WF.count("runs-on: ubuntu-24.04") == 4 and "ubuntu-latest" not in WF


def test_repository_id_constant_matches_wif_condition():
    assert f"assertion.repository_id == '{PL.GITHUB_REPOSITORY_ID}'" in PL.WIF_ATTRIBUTE_CONDITION


# ═══════════════════════════════════════ SQL: Tables API, только представления пакета
MANIFEST_OK = None


@pytest.fixture
def rendered(tmp_path, monkeypatch):
    from tools.tenancy import registry as R
    from tools.tenancy import sql_package as SP
    monkeypatch.setattr(R, "terraform_inputs", lambda t: SY.fixture_contract(t))
    out = tmp_path / "sql"
    SP.render(TENANT, out)
    env = {"EXPECTED_MANIFEST_SHA256": hashlib.sha256((out / SD.MANIFEST).read_bytes()).hexdigest(),
           "EXPECTED_PACKAGE_SHA256": SD.rendered_package_sha256(out)}
    return out, env


def test_sql_package_loads_all_views_in_manifest_order(rendered):
    out, env = rendered
    specs = SD.load_package(TENANT, out, CONTRACT_OBJ, env)
    assert len(specs) == 30 and [s.order for s in specs] == list(range(30))
    assert {s.dataset for s in specs} == {"ozon_mart", "tenant_ops", "analytics_share"}


@pytest.mark.parametrize("mutate,match", [
    (lambda out, env: env.update(EXPECTED_PACKAGE_SHA256="0" * 64), "пакета"),
    (lambda out, env: env.update(EXPECTED_MANIFEST_SHA256="0" * 64), "манифеста"),
    (lambda out, env: (out / "ozon_mart" / "EXTRA.sql").write_text("SELECT 1"), "пакета"),
])
def test_sql_package_rejects_tampering(rendered, mutate, match):
    out, env = rendered
    mutate(out, env)
    with pytest.raises(SD.SqlDeployError, match=match):
        SD.load_package(TENANT, out, CONTRACT_OBJ, env)


def test_sql_package_rejects_non_view_or_foreign_identifier(rendered):
    out, env = rendered
    f = out / "analytics_share" / "orders.sql"
    for evil in (f.read_text().replace("CREATE OR REPLACE VIEW", "CREATE OR REPLACE TABLE"),
                 f.read_text().replace(CONTRACT_OBJ["project_id"], "project-fa311fc0-4d87-4781-986", 1)):
        f.write_text(evil)
        m = SD._json((out / SD.MANIFEST).read_bytes())
        for o in m["objects"]:
            if o["name"] == "orders":
                o["sql_sha256"] = hashlib.sha256(f.read_bytes()).hexdigest()
        (out / SD.MANIFEST).write_text(json.dumps(m))
        env2 = {"EXPECTED_MANIFEST_SHA256": hashlib.sha256((out / SD.MANIFEST).read_bytes()).hexdigest(),
                "EXPECTED_PACKAGE_SHA256": SD.rendered_package_sha256(out)}
        with pytest.raises(SD.SqlDeployError):
            SD.load_package(TENANT, out, CONTRACT_OBJ, env2)


def _contract_tables():
    """Таблицы контракта в живых датасетах (T4.1: verify_live ждёт их как TABLE)."""
    return {(CONTRACT_OBJ["datasets"][t["dataset_key"]], t["table_id"]): {"type": "TABLE"}
            for t in CONTRACT_OBJ["tables"] if t["dataset_key"] in ("tenant_ops", "ozon_mart", "analytics_share")}


class FakeBQ:
    def __init__(self, existing=None, drift=False, table_type="VIEW"):
        self.store = {**_contract_tables(), **dict(existing or {})}
        self.calls = []
        self.drift, self.table_type = drift, table_type

    def get(self, ds, name):
        self.calls.append(("get", ds, name))
        r = self.store.get((ds, name))
        return (200, copy.deepcopy(r)) if r else (404, {})

    def list(self, ds):
        names = [n for (d, n) in self.store if d == ds]
        return names, {n: self.store[(ds, n)]["type"] for n in names}

    def insert(self, ds, res):
        self.calls.append(("insert", ds, res["tableReference"]["tableId"]))
        assert set(res) == {"tableReference", "description", "view"} and res["view"]["useLegacySql"] is False
        q = res["view"]["query"] + ("\n-- drift" if self.drift else "")
        self.store[(ds, res["tableReference"]["tableId"])] = {"type": self.table_type, "description": res["description"],
                                                             "view": {"query": q, "useLegacySql": False}}
        return 200, {}

    def update(self, ds, name, res):
        self.calls.append(("update", ds, name))
        return self.insert(ds, res)


def test_sql_deploy_inserts_views_reads_back_and_verifies(rendered):
    out, env = rendered
    specs = SD.load_package(TENANT, out, CONTRACT_OBJ, env)
    bq = FakeBQ()
    done = SD.deploy(specs, bq, CONTRACT_OBJ["project_id"])
    assert [op for op, _ in done] == ["insert"] * 30
    SD.verify_live(specs, bq, CONTRACT_OBJ)
    assert {c[0] for c in bq.calls} <= {"get", "insert", "update"}                 # никаких jobs/ACL/IAM
    again = SD.deploy(specs, bq, CONTRACT_OBJ["project_id"])
    assert {op for op, _ in again} == {"noop"}


def test_sql_deploy_fails_closed_on_readback_drift(rendered):
    out, env = rendered
    specs = SD.load_package(TENANT, out, CONTRACT_OBJ, env)
    with pytest.raises(SD.SqlDeployError, match="после записи"):
        SD.deploy(specs, FakeBQ(drift=True), CONTRACT_OBJ["project_id"])


def test_sql_deploy_never_touches_an_existing_table(rendered):
    out, env = rendered
    specs = SD.load_package(TENANT, out, CONTRACT_OBJ, env)
    s = specs[0]
    bq = FakeBQ(existing={(s.dataset, s.name): {"type": "TABLE"}})
    with pytest.raises(SD.SqlDeployError, match="не VIEW"):
        SD.deploy(specs, bq, CONTRACT_OBJ["project_id"])
    assert not [c for c in bq.calls if c[0] != "get"]


def test_sql_verify_live_rejects_unexpected_objects(rendered):
    out, env = rendered
    specs = SD.load_package(TENANT, out, CONTRACT_OBJ, env)
    bq = FakeBQ()
    SD.deploy(specs, bq, CONTRACT_OBJ["project_id"])
    bq.store[("analytics_share", "rogue_view")] = {"type": "VIEW", "view": {"query": "SELECT 1"}}
    with pytest.raises(SD.SqlDeployError, match="расходятся"):
        SD.verify_live(specs, bq, CONTRACT_OBJ)


def test_sql_helper_calls_no_query_jobs_or_acl_endpoints():
    src = (REPO / "tools" / "tenancy" / "sql_deploy.py").read_text(encoding="utf-8")
    code = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#") and '"""' not in l)
    # Эндпоинты IAM (":setIamPolicy"), а не имена прав: проба T4.1 спрашивает о правах IAM таблиц.
    for bad in ("/jobs", "/queries", ":setIamPolicy", ":getIamPolicy", "datasets/{" + "}", "PATCH"):
        assert bad not in code, bad
    assert "tables.insert" in src and "bigquery.jobs.create" in src      # документировано, что jobs нет


# ═══════════════════════════════════════ ревью пути: повторный запуск, владелец, живой main, план
def test_rerun_attempt_cannot_reuse_approval(tmp_path):
    _fails(_fetch, tmp_path, env=_env(GITHUB_RUN_ATTEMPT="2"), match="первой попыткой")


def test_only_the_repository_owner_can_apply(tmp_path):
    _fails(_fetch, tmp_path, env=_env(GITHUB_ACTOR_ID="12345"), match="владелец")


def test_live_main_moved_after_dispatch(tmp_path):
    _fails(_fetch, tmp_path, api=FakeApi(head="d" * 40), match="живой HEAD main")


@pytest.mark.parametrize("edit,why", [
    (lambda p: p.update(action_invocations=[{"address": "action.x"}]), "action_invocations"),
    (lambda p: p.update(deferred_changes=[{"reason": "x"}]), "deferred_changes"),
    (lambda p: p.update(errored=True), "ошибкой"),
    (lambda p: p.update(complete=False), "неполный"),
    (lambda p: p["resource_changes"][1]["change"].update(after_sensitive={"x": True}), "чувствительные"),
    (lambda p: p.update(output_changes={"o": {"actions": ["create"], "after_sensitive": True}}), "чувствительный"),
])
def test_plan_side_effects_and_sensitive_values_are_forbidden(edit, why):
    bad = copy.deepcopy(PLAN)
    edit(bad)
    assert any(why in f for f in D.plan_delta(bad)["forbidden"]), D.plan_delta(bad)["forbidden"]


def test_terraform_subprocess_does_not_inherit_tf_environment(monkeypatch):
    seen = {}

    class R:
        returncode, stdout, stderr = 0, "", ""

    def fake_run(cmd, **kw):
        seen.update(kw["env"])
        return R()
    monkeypatch.setenv("TF_CLI_ARGS_init", "-plugin-dir=/evil")
    monkeypatch.setenv("TF_CLI_CONFIG_FILE", "/evil.tfrc")
    monkeypatch.setattr(D.subprocess, "run", fake_run)
    D._tf(["init", "-input=false"], REPO)
    assert not [k for k in seen if k.startswith("TF_")] and "PATH" in seen


def test_oversized_artifact_is_refused(tmp_path):
    api = FakeApi()
    api._arts[0]["size_in_bytes"] = D.MAX_ZIP_BYTES + 1
    _fails(_fetch, tmp_path, api=api, match="размер")


def test_apply_dir_must_hold_exactly_the_artifact_files(tmp_path):
    _fetch(tmp_path)
    (tmp_path / "frozen" / "extra.tf").write_text("x")
    _fails(D.apply, TENANT, tmp_path / "frozen", env=_env(), tf=_fake_tf(), match="ровно четыре")


@pytest.mark.parametrize("ref", ["`evetis-analytics`.wb_raw.T", "`mpa-t-client-001.wb_mart2.T`",
                                 "`mpa-t-client-001`.`ozon_mart`.`X`.`Y`", "`mpa-t-client-001.region-eu.INFORMATION_SCHEMA.JOBS`",
                                 "other_proj.ozon_mart.T", "`mpa-t-client-001.OZON_MART.T`"])
def test_sql_references_are_checked_on_the_parse_tree(ref):
    with pytest.raises(SD.SqlDeployError):
        SD._check_references(f"SELECT a FROM {ref}", "mpa-t-client-001",
                             {"ozon_mart", "ozon_raw", "ref", "tenant_ops", "analytics_share"}, "t")
    SD._check_references("WITH c AS (SELECT 1 AS a) SELECT a FROM c JOIN `mpa-t-client-001.ozon_mart.X` x ON TRUE",
                         "mpa-t-client-001", {"ozon_mart"}, "t")


@pytest.mark.parametrize("env", [{"GITHUB_ACTOR_ID": "1", "GITHUB_RUN_ATTEMPT": "1"},
                                 {"GITHUB_ACTOR_ID": PL.GITHUB_OWNER_ID, "GITHUB_RUN_ATTEMPT": "2"}, {}])
def test_sql_deploy_requires_owner_and_first_attempt(env):
    with pytest.raises(SD.SqlDeployError):
        SD.check_writer(env)
    SD.check_writer({"GITHUB_ACTOR_ID": PL.GITHUB_OWNER_ID, "GITHUB_RUN_ATTEMPT": "1"})


def test_owner_id_constant_matches_wif_condition():
    assert f"assertion.repository_owner_id == '{PL.GITHUB_OWNER_ID}'" in PL.WIF_ATTRIBUTE_CONDITION
