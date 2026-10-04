"""Tenancy T3.2b — выпуск образа runtime арендатора: дескриптор, провенанс, контракт.

Офлайн: ни облака, ни сети, ни Docker. Живые части выпуска (сборка Cloud Build от
sa-runtime-builder, проверка опубликованного образа tools/tenancy/runtime_image_check.py,
аудит IAM mpa-platform) выполнены при выпуске и зафиксированы в записи провенанса.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from tools.tenancy import naming as N  # noqa: E402
from tools.tenancy import platform as PL  # noqa: E402
from tools.tenancy import registry as R  # noqa: E402
from tools.tenancy import synthetic as SY  # noqa: E402
from tools.tenancy import tenant_infra as TI  # noqa: E402

RELEASES = REPO / PL.RUNTIME_RELEASES_DIR
# Конфиги сборки неизменяемы по версиям (T5): запись выпуска называет свой файл (build.config).
CONFIG_V1 = RELEASES / "ozon-runtime.cloudbuild.yaml"
NEXT_CONFIG = RELEASES / "ozon-runtime.v2.cloudbuild.yaml"
EVETIS_PROD_DIGEST = "sha256:24e3c6d6715fa7b73d30b4270f9863d2b8680b4b02d4874ff1ea12b4fd90fa1b"
HEX40, HEX64 = re.compile(r"[0-9a-f]{40}"), re.compile(r"[0-9a-f]{64}")


def _approved():
    return PL.load_runtime_release(REPO)["ozon"]


def _records():
    return {p: json.loads(p.read_text(encoding="utf-8")) for p in sorted((RELEASES / "ozon").glob("*.json"))}


def _config():
    """Конфиг, по которому собран ТЕКУЩИЙ утверждённый образ (из его записи выпуска)."""
    return REPO / _current_record()["build"]["config"]


def _current_record():
    matches = [r for r in _records().values() if r["image"] == _approved()]
    assert len(matches) == 1, "у утверждённого digest ровно одна запись провенанса"
    return matches[0]


# ═══════════════════════════════════════ дескриптор: только неизменяемый digest платформы
def test_descriptor_points_to_an_immutable_platform_digest():
    image = _approved()
    assert image and PL.check_runtime_image(image) == image
    assert image.startswith(PL.RUNTIME_REGISTRY + "/ozon-runtime@sha256:")
    raw = (REPO / PL.RUNTIME_RELEASE_FILE).read_text(encoding="utf-8")
    for bad in (":latest", PL.EVETIS_PROJECT_ID, EVETIS_PROD_DIGEST.split(":")[1], "client_", "mpa-t-"):
        assert bad not in raw, bad


# ═══════════════════════════════════════ провенанс выпуска
def test_every_release_record_is_well_formed():
    assert _records(), "нет ни одной записи провенанса"
    for path, r in _records().items():
        assert r["release_contract"] == "vts.runtime-release.v1" and r["marketplace"] == "ozon", path
        assert PL.check_runtime_image(r["image"]) == r["image"]
        assert r["image"].endswith("@" + r["digest"]) and HEX64.fullmatch(r["digest"].split(":")[1])
        assert HEX40.fullmatch(r["source"]["commit"]) and path.stem == r["source"]["commit"][:7]
        assert r["operator_tag"] == r["source"]["commit"]          # тег неизменяем, контракт — digest
        assert r["source"]["context"] == "pipelines/ozon/runtime"
        assert r["source"]["dockerfile"] == "pipelines/ozon/runtime/Dockerfile"
        assert r["source"]["archive"]["tar_commit_id"] == r["source"]["commit"]
        assert r["build"]["source_object_sha256"] == r["source"]["archive"]["sha256"]


# Факты каждого выпуска: коммит-источник, конфиг сборки (неизменяемый по версиям) и шаги-ворота.
V1_GATES = ["identity", "build", "record", "contents", "fail-closed-without-project"]
RELEASES_FACTS = {
    "08f9438": {"commit": "08f9438fd7a49c02d5ee1786ce820d5384a5c944", "config": CONFIG_V1, "gates": V1_GATES},
    # T5: коммит слияния PR #226 = main на момент сборки; v2 добавляет модули control и их отказ.
    "e52432e": {"commit": "e52432e7cd4052db76772f1501e3f315bc994645", "config": NEXT_CONFIG,
                "gates": V1_GATES + ["control-fail-closed-without-project"]},
    "b28517b": {"commit": "b28517b72b5d7ae0d866f4d643f4982db269210c", "config": RELEASES / "ozon-runtime.v3.cloudbuild.yaml",
                "gates": V1_GATES + ["control-fail-closed-without-project", "external-binding-omission-denied"]},
    # Exact live build/qualification evidence for reviewed #242, registered independently of record JSON.
    "4d1297c": {"commit": "4d1297c774e8f594cb4ad1d55ef21347b4059ca8", "config": RELEASES / "ozon-runtime.v5.cloudbuild.yaml",
                "gates": V1_GATES + ["control-fail-closed-without-project", "external-binding-omission-denied"],
                "digest": "sha256:0016d1cf50ed5b4721da8d162bb9133ab8c6e77c7204c3b312aca6fa103a555c",
                "build_id": "e0b503bf-a4d6-471b-921c-34f1909c392e",
                "qualification_build": "5120d181-4768-4b44-93bb-08e8a29366cc",
                "archive_sha256": "2a928040603aa5eba9e87b5c39a227af753b91b558ccf5eb5192b7e91615bd1f"},
    # Independently observed immutable v6 build/qualification, not derived from release JSON.
    "fe60d6b": {"commit": "fe60d6b4685609769f4dad320564e906475d24db", "config": RELEASES / "ozon-runtime.v6.cloudbuild.yaml",
                "gates": V1_GATES + ["control-fail-closed-without-project", "external-binding-omission-denied", "backfill-invalid-scope-denied"],
                "digest": "sha256:a9917169e40c367fa17c3e54babde245fd0998cb1fbf090af6bb8c63f9ffe170",
                "build_id": "2c0fb3ce-1389-4cb8-83ef-3fa59806ecf9",
                "qualification_build": "f854c64f-7e46-48b6-a2ac-088402b76bcf",
                "archive_sha256": "d241b7cc43d2e97e9fa0d753e32ed7bb617a27996f6e1c35dd9c9908ea16a827"},
}


def test_every_release_has_known_facts_and_its_own_config():
    for path, r in _records().items():
        f = RELEASES_FACTS[path.stem]
        assert r["source"]["commit"] == f["commit"], path
        assert r["build"]["config"] == str(f["config"].relative_to(REPO)), path
        assert r["build"]["config_sha256"] == hashlib.sha256(f["config"].read_bytes()).hexdigest(), path
        if "digest" in f:
            assert r["digest"] == f["digest"], path
            assert r["build"]["build_id"] == f["build_id"], path
            assert r["verification"]["image_qualification_build"] == f["qualification_build"], path
            assert r["source"]["archive"]["sha256"] == f["archive_sha256"], path


def test_current_release_is_bound_to_source_build_and_identity():
    r = _current_record()
    assert r["digest"] == _approved().split("@")[1]
    assert r["source"]["commit"] == RELEASES_FACTS[r["source"]["commit"][:7]]["commit"]
    b = r["build"]
    assert b["mechanism"] == "Cloud Build" and b["project"] == PL.PLATFORM_PROJECT_ID
    assert b["region"] == PL.RUNTIME_REGION and b["status"] == "SUCCESS"
    assert b["service_account"] == PL.RUNTIME_BUILDER_SA != PL.PROVISIONER_SA
    assert "@cloudbuild.gserviceaccount.com" not in json.dumps(b)  # не устаревший SA Cloud Build
    assert b["source_object"].startswith(f"gs://{PL.RUNTIME_BUILD_BUCKET}/source/")
    assert b["build_args"] == [] and b["secrets"] == []
    assert b["config"] == str(RELEASES_FACTS[r["source"]["commit"][:7]]["config"].relative_to(REPO))
    assert r["image_facts"]["labels"]["org.opencontainers.image.revision"] == r["source"]["commit"]
    assert r["image_facts"]["entrypoint"] == ["python", "main.py"]


def test_release_record_states_evetis_was_not_deployed():
    d = _current_record()["deployment"]
    assert d["evetis"].startswith("NOT_DEPLOYED") and EVETIS_PROD_DIGEST in d["evetis"]
    assert d["tenants"].startswith("INERT")


def test_release_source_matches_git_when_history_is_available():
    r = _current_record()
    sha = r["source"]["commit"]
    if subprocess.run(["git", "cat-file", "-e", f"{sha}^{{commit}}"], cwd=REPO).returncode != 0:
        pytest.skip("мелкий клон без исходного коммита выпуска")
    tree = subprocess.run(["git", "rev-parse", f"{sha}:pipelines/ozon/runtime"], cwd=REPO,
                          capture_output=True, text=True, check=True).stdout.strip()
    assert tree == r["source"]["context_tree"]
    for name, digest in r["source"]["context_files_sha256"].items():
        blob = subprocess.run(["git", "show", f"{sha}:pipelines/ozon/runtime/{name}"], cwd=REPO,
                              capture_output=True, check=True).stdout
        assert hashlib.sha256(blob).hexdigest() == digest, name
    archive = subprocess.run(["git", "archive", "--format=tar.gz", sha, "pipelines/ozon/runtime"],
                             cwd=REPO, capture_output=True, check=True).stdout
    assert hashlib.sha256(archive).hexdigest() == r["source"]["archive"]["sha256"]


def test_release_record_and_config_carry_no_credentials():
    configs = sorted(RELEASES.glob("*.cloudbuild.yaml"))
    text = "\n".join(p.read_text(encoding="utf-8") for p in [*_records(), *configs, REPO / PL.RUNTIME_RELEASE_FILE])
    for pat in (r"BEGIN [A-Z ]*PRIVATE KEY", r'"private_key', r"ya29\.", r"gh[pousr]_[A-Za-z0-9]{20}",
                r"AIza[0-9A-Za-z_-]{30}", r"EVETIS_OZON_[A-Z_]+=", r"(?i)client_secret\s*[:=]"):
        assert not re.search(pat, text), pat


# ═══════════════════════════════════════ конфиг сборки
def test_build_config_pins_builder_gates_identity_and_takes_no_secrets():
    text = _config().read_text(encoding="utf-8")
    r = _current_record()
    assert r["build"]["builder_image"] in text
    assert re.findall(r"name: (\S+)", text) and all("@sha256:" in n for n in re.findall(r"name: (\S+)", text))
    assert f"_BUILDER: {PL.RUNTIME_BUILDER_SA}" in text
    ids = re.findall(r"^  - id: (\S+)$", text, flags=re.M)
    assert ids == RELEASES_FACTS[r["source"]["commit"][:7]]["gates"]
    assert 'test "$$who" = "${_BUILDER}"' in text                     # гейт идентичности — до сборки
    # Конфиг — часть провенанса текущего выпуска: менять его можно только вместе с новым выпуском.
    assert hashlib.sha256(_config().read_bytes()).hexdigest() == r["build"]["config_sha256"]
    # v1 — провенанс выпуска 08f9438: байты не меняются никогда.
    assert hashlib.sha256(CONFIG_V1.read_bytes()).hexdigest() == \
        "ba89d1fd17047df51a1d8ec6981ae8096fce44be1e7cf9dbda200ada379a9905"
    for bad in ("secretEnv", "availableSecrets", "--build-arg", "latest"):
        assert bad not in text, bad
    assert f"{PL.RUNTIME_REGISTRY}/ozon-runtime" in text and "logging: CLOUD_LOGGING_ONLY" in text


# ═══════════════════════════════════════ контракт арендатора на настоящем выпуске
def test_client_001_contract_resolves_the_approved_digest():
    c = R.terraform_inputs("client_001")
    assert c["marketplaces"]["ozon"]["runtime_image"] == _approved()


def test_two_tenants_share_the_image_and_differ_in_everything_tenant_specific():
    a, b = SY.release_contract("client_001"), SY.release_contract("client_002")
    oa, ob = a["marketplaces"]["ozon"], b["marketplaces"]["ozon"]
    assert oa["runtime_image"] == ob["runtime_image"] == _approved()      # образ арендатора не бывает
    assert a["project_id"] == "mpa-t-client-001" and b["project_id"] == "mpa-t-client-002"
    assert a["state"]["prefix"] == "tenants/client_001" and b["state"]["prefix"] == "tenants/client_002"
    assert {j: s["env"]["GCP_PROJECT_ID"] for j, s in oa["jobs"].items()} == dict.fromkeys(oa["jobs"], a["project_id"])
    assert {j: s["env"]["GCP_PROJECT_ID"] for j, s in ob["jobs"].items()} == dict.fromkeys(ob["jobs"], b["project_id"])
    sa = N.dedicated_service_accounts("client_001", "ozon")["runtime"]
    assert f"{sa}@{a['project_id']}.iam.gserviceaccount.com" != f"{sa}@{b['project_id']}.iam.gserviceaccount.com"
    assert not (REPO / "tenants" / "client_002").exists()                  # эфемерная фикстура


@pytest.mark.parametrize("tid", SY.SYNTHETIC_TENANTS)
def test_release_fixtures_match_the_registry_and_descriptor(tid):
    path = SY.RELEASE_FIXTURE_DIR / f"{tid}.contract.json"
    assert path.read_text(encoding="utf-8") == SY.release_fixture_text(tid)
    assert json.loads(path.read_text(encoding="utf-8"))["marketplaces"]["ozon"]["runtime_image"] == _approved()


def test_release_runs_exist_in_terraform_tests():
    t = (REPO / "infra" / "tenant" / "tests" / "tenant.tftest.hcl").read_text(encoding="utf-8")
    for tid in SY.SYNTHETIC_TENANTS:
        assert f"tests/fixtures/release/{tid}.contract.json" in t


def test_client_001_plan_now_passes_the_image_gate_without_touching_the_cloud(tmp_path, monkeypatch):
    """Рендер и ворота образа проходят; дальше — проверка биллинга, которую здесь останавливаем."""
    calls = []

    class Stop(Exception):
        pass

    monkeypatch.setattr(TI, "_tf", lambda args, cwd: calls.append(args[0]))

    def no_cloud(project_id):
        calls.append(("billing", project_id))
        raise Stop

    monkeypatch.setattr(TI, "check_billing_enabled", no_cloud)
    with pytest.raises(Stop):
        TI.plan("client_001", tmp_path)
    assert calls == ["fmt", ("billing", "mpa-t-client-001")]            # ни init, ни plan
    rendered = json.loads((tmp_path / TI.CONTRACT_FILE).read_text(encoding="utf-8"))
    assert rendered["contract"]["marketplaces"]["ozon"]["runtime_image"] == _approved()


# ═══════════════════════════════════════ идентичность сборки — отдельно от провижининга
def test_builder_identity_is_separate_from_tenant_provisioning():
    assert PL.RUNTIME_BUILDER_SA.endswith(f"@{PL.PLATFORM_PROJECT_ID}.iam.gserviceaccount.com")
    assert PL.RUNTIME_BUILDER_SA != PL.PROVISIONER_SA
    assert PL.RUNTIME_BUILD_BUCKET != PL.STATE_BUCKET
    wf = (REPO / PL.TENANT_INFRA_WORKFLOW).read_text(encoding="utf-8")
    assert PL.RUNTIME_BUILDER_SA not in wf                                # WIF провижионера его не получает


# ═══════════════════════════════════════ следующий конфиг (T5): совпадает с Dockerfile
@pytest.mark.parametrize("candidate", [False, True])
def test_next_build_config_expects_exactly_the_dockerfile_contents(candidate):
    """Шаг contents v2 ждёт ровно файлы из COPY Dockerfile (+ requirements.txt): иначе сборка
    кандидата T5 упала бы на собственной проверке состава (или пропустила лишнее)."""
    text = (RELEASES / "ozon-runtime.v6.cloudbuild.yaml" if candidate else NEXT_CONFIG).read_text(encoding="utf-8")
    docker = (REPO / "pipelines/ozon/runtime/Dockerfile").read_text(encoding="utf-8")
    copied = set()
    for line in docker.splitlines():
        if line.startswith("COPY "):
            copied |= set(line.split()[1:-1])
    if not candidate:
        # V2 is immutable historical release evidence, not the candidate Dockerfile.
        copied = set(json.loads((RELEASES / "ozon/e52432e.json").read_text())["image_facts"]["app_files"])
    want = " ".join(sorted(copied)) + " "
    got = re.search(r'test "\$\$got" = "([^"]*)"', text).group(1)
    assert got == want, (got, want)
    assert {"lifecycle.py", "seller_method_policy.json", "requirements.txt"} <= copied


def test_next_build_config_keeps_every_v1_gate():
    text = NEXT_CONFIG.read_text(encoding="utf-8")
    ids = re.findall(r"^  - id: (\S+)$", text, flags=re.M)
    assert ids == ["identity", "build", "record", "contents", "fail-closed-without-project",
                   "control-fail-closed-without-project"]
    assert all("@sha256:" in n for n in re.findall(r"name: (\S+)", text))
    assert f"_BUILDER: {PL.RUNTIME_BUILDER_SA}" in text and 'test "$$who" = "${_BUILDER}"' in text
    for bad in ("secretEnv", "availableSecrets", "--build-arg", "latest"):
        assert bad not in text, bad
    assert "substitutionOption: MUST_MATCH" in text and "logging: CLOUD_LOGGING_ONLY" in text
    assert "--entrypoint=python ${_IMAGE}:${_SOURCE_SHA} lifecycle.py status" in text


def test_policy_identity_candidate_build_preserves_gates_without_deployment():
    text=(RELEASES/"ozon-runtime.v3.cloudbuild.yaml").read_text()
    ids=re.findall(r"^  - id: (\S+)$",text,flags=re.M)
    assert ids==["identity","build","record","contents","fail-closed-without-project",
                 "control-fail-closed-without-project","external-binding-omission-denied"]
    assert all("@sha256:" in n for n in re.findall(r"name: (\S+)",text))
    assert "--network=none -e GCP_PROJECT_ID=mpa-t-client-001" in text
    assert 'test "$$rc" -eq 2' in text
    assert "TENANT_BINDING_REQUIRED=" not in text
    for bad in ("secretEnv","availableSecrets","--build-arg","latest"):
        assert bad not in text


@pytest.mark.parametrize('omission_exit,expected_pass', [(2,True),(1,False),(0,False),(3,False)])
def test_image_verifier_checks_omitted_binding_contract_without_docker(monkeypatch,omission_exit,expected_pass):
    from types import SimpleNamespace
    from tools.tenancy import runtime_image_check as V
    project='mpa-t-synthetic';sha='a'*40
    env={'GCP_PROJECT_ID':project,'TENANT_BINDING_REQUIRED':'1','ENTITIES':'catalog'}
    monkeypatch.setattr(V,'tenant_envs',lambda:{'synthetic':{'project':project,'secret_ids':[],
        'jobs':{'ozon-runtime-daily':env},'control_env':env}})
    cfg={'Entrypoint':['python','main.py'],'WorkingDir':'/app',
         'Labels':{'org.opencontainers.image.revision':sha},'Env':[]}
    monkeypatch.setattr(V,'_docker',lambda *a,**k:SimpleNamespace(stdout=json.dumps(cfg)))
    def run(image,environment,driver=None):
        if driver=='_portability_driver.py':
            return SimpleNamespace(returncode=0,stdout=json.dumps({'config':{'project':project},'secret_paths':[],'bq_objects':[]}),stderr='')
        if driver:
            flagged=environment.get('TENANT_BINDING_REQUIRED')=='1'
            seen={'exit':3 if flagged else omission_exit,'http':[],'secret_paths':[],
                  'bq':[['list_tables',project+'.ref']] if flagged else [],
                  'config':{'project':project,'ref_dataset':'ref'}}
            return SimpleNamespace(returncode=0,stdout=json.dumps(seen),stderr='')
        return SimpleNamespace(returncode=3,stdout='',stderr='' if environment else 'GCP_PROJECT_ID не задан')
    monkeypatch.setattr(V,'_run',run)
    monkeypatch.setattr(V,'_run_control',lambda image,e:SimpleNamespace(returncode=2,stdout='',stderr='' if e else 'GCP_PROJECT_ID не задан'))
    fails=V.check(PL.RUNTIME_REGISTRY+'/synthetic@sha256:'+('b'*64),sha)
    assert (not fails)==expected_pass, fails
    if not expected_pass:assert any('omitted binding contract' in f for f in fails)


def test_broad_inventory_release_retains_independent_security_qualification():
    record = next(r for path, r in _records().items() if path.stem == '4d1297c')
    qualification = record['verification']
    assert qualification['security_prs'] == [239, 240] and qualification['model_pr'] == 242
    assert qualification['built_artifact']['seller_transport_ast'] == 'PASS'
    assert qualification['built_artifact']['source_hashes_verified'] == len(record['image_facts']['app_files']) == 17
    results = qualification['adversarial_results']
    assert any(r.get('dangerous_paths_tested') == 8 and r.get('denied_before_credentials_network') == 33 for r in results)
    assert any(r.get('raw_sdk_static_negative_controls') == 'PASS' for r in results)
    assert any(r.get('parent_import_external_dispatch') == 'PASS' for r in results)
    assert any(r.get('broad_inventory_model') == 'PASS_WITH_WARNINGS' and r.get('hard_blockers') == 'PASS' and r.get('legacy_strict') == 'PASS' for r in results)
