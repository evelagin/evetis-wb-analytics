#!/usr/bin/env python3
"""Mig1b-0: проверки целевого выката ОДНОГО Job'а `wb-stocks-prod`.

Используется `.github/workflows/deploy-prod.yml` (режим `only_job=wb-stocks-prod`) до и после
`gcloud run jobs update`. Сам ничего не вызывает: читает JSON `gcloud run jobs describe/list
--format=json` и байты манифеста/конфига образа, которые workflow скачал из Artifact Registry.

  config-digest <manifest.json>
        — печатает digest конфига образа из манифеста (workflow скачивает этот blob).
  pre   <shadow.json> <jobs_before.json> <manifest.json> <image_config.json> <digest> <git_sha>
        — digest = образ wb-stocks-shadow (проверен в shadow);
          происхождение: sha256(manifest) = digest, sha256(config) = manifest.config.digest,
          метка `git-sha` конфига = git_sha (её ставит deploy-shadow при сборке);
          текущий wb-stocks-prod — точный проект, BQ_RAW_DATASET, SA prod (до любого update).
  post  <stocks_prod.json> <before_list.json> <after_list.json> <digest> <git_sha>
        — у wb-stocks-prod образ = digest, env = боевые таблицы + provenance + проект/датасет,
          SA = prod, args не изменены этим выкатом; ни один ДРУГОЙ Job не изменился
          (образ, generation, env, args).

Код выхода 0 — проверка пройдена, 1 — нарушение (выкат считается неудачным), 2 — ошибка ввода.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys

TARGET_JOB = "wb-stocks-prod"
PROJECT_ID = "project-fa311fc0-4d87-4781-986"
PROJECT_NUMBER = "37074083763"  # metadata.namespace у Cloud Run Job'а
PROD_SA = f"sa-loaders-prod@{PROJECT_ID}.iam.gserviceaccount.com"
IMAGE_PREFIX = f"europe-west1-docker.pkg.dev/{PROJECT_ID}/wb-loaders/wb-loader@sha256:"
GIT_SHA_LABEL = "git-sha"  # deploy-shadow.yml: docker build --label "git-sha=${GITHUB_SHA}"

# Боевые таблицы и режим — те же, что пишет Apps Script (cloud/src/loaders/stocks/targets.ts).
# Их ставит выкат (--update-env-vars).
PROD_STOCKS_ENV = {
    "STOCKS_RAW_TABLE": "RAW_WB_STOCKS",
    "STOCKS_SNAPSHOT_TABLE": "WB_STOCKS_SNAPSHOTS",
    "STOCKS_T5_TABLE": "RAW_WB_STOCKS_T5",
    "STOCKS_T5_MODE": "on",
    "REF_SKU_TABLE": "REF_SKU_MASTER",
    "WB_ANALYTICS_SECRET": "WB_TOKEN_ANALYTICS",
}
# Контур — уже есть у Job'а (Terraform common_env), выкат их не меняет; проверяются до и после.
PROD_RUNTIME_ENV = {
    "ENVIRONMENT": "prod",
    "GCP_PROJECT_ID": PROJECT_ID,
    "BQ_RAW_DATASET": "wb_raw",
}
_SINGLE_IMAGE_MANIFESTS = (
    "application/vnd.docker.distribution.manifest.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
)


def _container(job: dict) -> dict:
    return job["spec"]["template"]["spec"]["template"]["spec"]["containers"][0]


def _state(job: dict) -> dict:
    c = _container(job)
    return {
        "image": c.get("image"),
        "generation": job.get("metadata", {}).get("generation"),
        "args": c.get("args"),
        # секреты приходят как valueFrom без value — сравниваются целиком
        "env": {e["name"]: e.get("value", json.dumps(e.get("valueFrom"), sort_keys=True)) for e in c.get("env", [])},
        "sa": job["spec"]["template"]["spec"]["template"]["spec"].get("serviceAccountName"),
    }


def _by_name(jobs: list) -> dict:
    return {j["metadata"]["name"]: _state(j) for j in jobs}


def _sha256(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _check_digest_format(digest: str) -> list[str]:
    if not digest.startswith(IMAGE_PREFIX) or not re.fullmatch(r"[0-9a-f]{64}", digest[len(IMAGE_PREFIX):]):
        return [f"digest не из реестра проекта: ожидался {IMAGE_PREFIX}<64 hex>, получен {digest}"]
    return []


def _check_contour(job: dict, name: str) -> list[str]:
    """Проект, датасет и SA prod-Job'а."""
    problems: list[str] = []
    ns = job.get("metadata", {}).get("namespace")
    if ns != PROJECT_NUMBER:
        problems.append(f"{name}: проект (namespace) {ns!r}, ожидался {PROJECT_NUMBER}")
    s = _state(job)
    for k, v in PROD_RUNTIME_ENV.items():
        if s["env"].get(k) != v:
            problems.append(f"{name}: env {k}={s['env'].get(k)!r}, ожидалось {v!r}")
    if s["sa"] != PROD_SA:
        problems.append(f"{name}: SA {s['sa']!r}, ожидался {PROD_SA}")
    return problems


def config_digest(manifest_raw: bytes) -> str:
    m = json.loads(manifest_raw)
    if m.get("mediaType") not in _SINGLE_IMAGE_MANIFESTS:
        raise ValueError(f"манифест {m.get('mediaType')!r}: ожидался одиночный образ")
    return m["config"]["digest"]


def check_provenance(manifest_raw: bytes, config_raw: bytes, digest: str, git_sha: str) -> list[str]:
    """Образ = digest и собран из git_sha: цепочка хэшей манифест → конфиг → метка."""
    problems: list[str] = []
    want = digest.split("@", 1)[-1]
    if _sha256(manifest_raw) != want:
        return [f"манифест не принадлежит digest: sha256 {_sha256(manifest_raw)} ≠ {want}"]
    try:
        cfg_digest = config_digest(manifest_raw)
    except (ValueError, KeyError) as e:
        return [f"манифест: {e}"]
    if _sha256(config_raw) != cfg_digest:
        return [f"конфиг образа не из манифеста: sha256 {_sha256(config_raw)} ≠ {cfg_digest}"]
    labels = (json.loads(config_raw).get("config") or {}).get("Labels") or {}
    if labels.get(GIT_SHA_LABEL) != git_sha:
        problems.append(f"образ собран не из source_git_sha: метка {GIT_SHA_LABEL}={labels.get(GIT_SHA_LABEL)!r} ≠ {git_sha}")
    return problems


def check_pre(shadow: dict, before: list, manifest_raw: bytes, config_raw: bytes, digest: str, git_sha: str) -> list[str]:
    problems = _check_digest_format(digest)
    img = _container(shadow).get("image")
    if img != digest:
        problems.append(f"digest не равен образу wb-stocks-shadow: {img} ≠ {digest}")
    problems += check_provenance(manifest_raw, config_raw, digest, git_sha)
    target = [j for j in before if j.get("metadata", {}).get("name") == TARGET_JOB]
    if len(target) != 1:
        problems.append(f"{TARGET_JOB} не найден в снимке Job'ов до выката")
    else:
        problems += _check_contour(target[0], TARGET_JOB)
    return problems


def check_post(target: dict, before: list, after: list, digest: str, git_sha: str) -> list[str]:
    problems = _check_digest_format(digest) + _check_contour(target, TARGET_JOB)
    t = _state(target)
    if t["image"] != digest:
        problems.append(f"{TARGET_JOB}: образ {t['image']} ≠ {digest}")
    want = dict(PROD_STOCKS_ENV, IMAGE_DIGEST=digest, GIT_SHA=git_sha)
    for k, v in want.items():
        if t["env"].get(k) != v:
            problems.append(f"{TARGET_JOB}: env {k}={t['env'].get(k)!r}, ожидалось {v!r}")

    b, a = _by_name(before), _by_name(after)
    if TARGET_JOB not in b:
        problems.append(f"{TARGET_JOB} отсутствует в снимке ДО выката")
    elif b[TARGET_JOB]["args"] != t["args"]:
        problems.append(f"{TARGET_JOB}: args изменились выкатом ({b[TARGET_JOB]['args']} → {t['args']}); args — зона Terraform")
    if set(b) != set(a):
        problems.append(f"состав Job'ов изменился: +{sorted(set(a) - set(b))} −{sorted(set(b) - set(a))}")
    for name in sorted(set(a) & set(b)):
        if name != TARGET_JOB and a[name] != b[name]:
            changed = [k for k in a[name] if a[name][k] != b[name][k]]
            problems.append(f"посторонний Job изменён: {name} ({', '.join(changed)})")
    return problems


def _load(path: str):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _raw(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


def main(argv: list[str]) -> int:
    try:
        if len(argv) == 3 and argv[1] == "config-digest":
            print(config_digest(_raw(argv[2])))
            return 0
        if len(argv) == 8 and argv[1] == "pre":
            problems = check_pre(_load(argv[2]), _load(argv[3]), _raw(argv[4]), _raw(argv[5]), argv[6], argv[7])
        elif len(argv) == 7 and argv[1] == "post":
            problems = check_post(_load(argv[2]), _load(argv[3]), _load(argv[4]), argv[5], argv[6])
        else:
            print(__doc__, file=sys.stderr)
            return 2
    except (OSError, ValueError, KeyError, IndexError, TypeError) as e:
        print(f"STOCKS DEPLOY CHECK: ошибка ввода — {e}", file=sys.stderr)
        return 2
    for p in problems:
        print(f"STOCKS DEPLOY CHECK: {p}", file=sys.stderr)
    if problems:
        return 1
    print(f"STOCKS DEPLOY CHECK: OK ({argv[1]})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
