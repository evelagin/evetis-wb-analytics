#!/usr/bin/env python3
"""Проверка опубликованного образа runtime арендатора до записи его digest (Tenancy T3.2b).

  python tools/tenancy/runtime_image_check.py <образ@sha256:...> --source-sha <40 hex>

Запускает САМ опубликованный образ (не исходники) локально в Docker, без сети
(--network=none), с синтетической конфигурацией арендаторов из реестра:
client_001 (реестр) и эфемерного client_002 (tools/tenancy/synthetic.py). Учётных данных
нет ни настоящих, ни поддельных GCP: драйверы подменяют клиентов облака внутри процесса.

  1. конфигурация образа: точка входа, рабочий каталог, метка исходного коммита;
  2. нет GCP_PROJECT_ID → отказ при старте;
  3. T5: job арендатора (TENANT_BINDING_REQUIRED=1) без подтверждённой привязки → отказ (выход 3)
     первым шагом: без секретов и HTTP, только листинг знаков владельца в ref своего проекта;
     без флага (слой T3.2) контейнеры секретов без версий → отказ до любого HTTP к Ozon, секреты
     только своего проекта и по именам реестра (tests/_empty_secret_driver.py);
  4. переносимость: проект, датасеты и секреты — арендатора, не EVETIS
     (tests/_portability_driver.py);
  5. настоящая точка входа с конфигурацией арендатора и без сети → отказ, EVETIS не упомянут;
  6. T5: точка входа control (python lifecycle.py status) — без проекта отказ при старте, с
     конфигурацией арендатора и без сети — отказ, EVETIS не упомянут.

Требует Docker и права на чтение mpa-runtime. В CI не входит: CI не читает реестр платформы.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from tools.tenancy import platform as PL  # noqa: E402
from tools.tenancy import registry as R  # noqa: E402
from tools.tenancy.validation import parse_tenant_json  # noqa: E402

TESTS = REPO / "pipelines" / "ozon" / "tests"
EVETIS_MARKERS = (PL.EVETIS_PROJECT_ID, PL.EVETIS_PROJECT_NUMBER, "EVETIS_OZON_", "evetis_ref")
DRIVER_MOUNT = "/verify/tests"
# Драйверы ищут runtime в ../runtime относительно себя; в образе код лежит в /app.
DRIVER_PREFIX = "ln -s /app /verify/runtime && exec python "


def _docker(args: list[str], timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)


def _run(image: str, env: dict[str, str], driver: str | None = None) -> subprocess.CompletedProcess:
    cmd = ["run", "--rm", "--platform=linux/amd64", "--network=none"]
    for k, v in sorted(env.items()):
        cmd += ["-e", f"{k}={v}"]
    if driver:
        cmd += ["-v", f"{TESTS}:{DRIVER_MOUNT}:ro", "--entrypoint=sh", image, "-c",
                DRIVER_PREFIX + f"{DRIVER_MOUNT}/{driver}"]
    else:
        cmd += [image]
    return _docker(cmd)


def _run_control(image: str, env: dict[str, str]) -> subprocess.CompletedProcess:
    """Точка входа job'а tenant-control: python lifecycle.py status (аргумент по умолчанию)."""
    cmd = ["run", "--rm", "--platform=linux/amd64", "--network=none"]
    for k, v in sorted(env.items()):
        cmd += ["-e", f"{k}={v}"]
    return _docker(cmd + ["--entrypoint=python", image, "lifecycle.py", "status"])


def tenant_envs() -> dict[str, dict[str, dict[str, str]]]:
    """Окружение каждого job'а двух арендаторов — тем же путём, что и контракт Terraform."""
    from tools.tenancy import synthetic as SY

    out = {"client_001": R.terraform_inputs("client_001"), "client_002": SY.release_contract("client_002")}
    return {tid: {"project": c["project_id"],
                  "secret_ids": sorted(c["marketplaces"]["ozon"]["secret_ids"].values()),
                  "jobs": {j: s["env"] for j, s in c["marketplaces"]["ozon"]["jobs"].items()},
                  "control_env": c["control"]["job"]["env"]}
            for tid, c in out.items()}


def check(image: str, source_sha: str) -> list[str]:
    PL.check_runtime_image(image)
    fails: list[str] = []

    cfg = parse_tenant_json(_docker(["image", "inspect", image, "--format", "{{json .Config}}"]).stdout)
    if cfg.get("Entrypoint") != ["python", "main.py"] or cfg.get("WorkingDir") != "/app":
        fails.append(f"точка входа/каталог: {cfg.get('Entrypoint')} {cfg.get('WorkingDir')}")
    if (cfg.get("Labels") or {}).get("org.opencontainers.image.revision") != source_sha:
        fails.append("метка org.opencontainers.image.revision не равна исходному коммиту")
    if any(e.split("=", 1)[0].startswith(("OZON_", "GCP_", "GOOGLE_", "BQ_")) for e in cfg.get("Env") or []):
        fails.append("в образ вшито окружение runtime/облака")

    r = _run(image, {})
    if r.returncode == 0 or "GCP_PROJECT_ID не задан" not in r.stderr:
        fails.append("без GCP_PROJECT_ID образ не отказал при старте")

    for tid, t in tenant_envs().items():
        allowed = {f"projects/{t['project']}/secrets/{s}/versions/latest" for s in t["secret_ids"]}
        for job, env in sorted(t["jobs"].items()):
            # T5: с проверкой привязки — отказ первым шагом, без секретов и без HTTP.
            if env.get("TENANT_BINDING_REQUIRED") != "1":
                fails.append(f"{tid}/{job}: в контракте нет TENANT_BINDING_REQUIRED=1")
            r = _run(image, env, "_empty_secret_driver.py")
            if r.returncode != 0:
                fails.append(f"{tid}/{job}: драйвер (привязка) упал: {r.stderr[-300:]}")
            else:
                seen = parse_tenant_json(r.stdout.strip().splitlines()[-1])
                if seen["exit"] != 3 or seen["http"] or seen["secret_paths"] or \
                        seen["bq"] != [["list_tables", f"{t['project']}.ref"]]:
                    fails.append(f"{tid}/{job}: без привязки нет отказа первым шагом "
                                 f"({seen['exit']}, {seen['http']}, {len(seen['secret_paths'])}, {seen['bq']})")
                fails += [f"{tid}/{job}: упомянут EVETIS ({m})" for m in EVETIS_MARKERS if m in r.stdout + r.stderr]
            # Слой T3.2 под проверкой привязки: пустые секреты — отказ до HTTP, только свои секреты.
            env = {k: v for k, v in env.items() if k != "TENANT_BINDING_REQUIRED"}
            r = _run(image, env, "_empty_secret_driver.py")
            if r.returncode != 0:
                fails.append(f"{tid}/{job}: драйвер пустых секретов упал: {r.stderr[-300:]}")
                continue
            seen = parse_tenant_json(r.stdout.strip().splitlines()[-1])
            if seen["exit"] != 1 or seen["http"] or not seen["secret_paths"]:
                fails.append(f"{tid}/{job}: без версий секретов нет отказа до HTTP ({seen['exit']}, {seen['http']})")
            if not set(seen["secret_paths"]) <= allowed:
                fails.append(f"{tid}/{job}: запрошены секреты вне проекта арендатора")
            if seen["config"]["project"] != t["project"] or seen["config"]["ref_dataset"] != "ref":
                fails.append(f"{tid}/{job}: конфигурация не арендатора: {seen['config']['project']}")
            blob = r.stdout + r.stderr
            fails += [f"{tid}/{job}: упомянут EVETIS ({m})" for m in EVETIS_MARKERS if m in blob]

        env = dict(t["jobs"]["ozon-runtime-daily"])
        env.pop("ENTITIES", None)
        r = _run(image, env, "_portability_driver.py")
        if r.returncode != 0:
            fails.append(f"{tid}: драйвер переносимости упал: {r.stderr[-300:]}")
        else:
            seen = parse_tenant_json(r.stdout.strip().splitlines()[-1])
            if seen["config"]["project"] != t["project"] or not set(seen["secret_paths"]) <= allowed:
                fails.append(f"{tid}: переносимость — проект или секреты не арендатора")
            if any(not ref.startswith(t["project"] + ".") for _, ref in seen["bq_objects"]):
                fails.append(f"{tid}: запись BigQuery вне проекта арендатора")
            fails += [f"{tid}: упомянут EVETIS ({m})" for m in EVETIS_MARKERS if m in r.stdout + r.stderr]

        r = _run(image, t["jobs"]["ozon-runtime-daily"])
        if r.returncode == 0:
            fails.append(f"{tid}: настоящая точка входа без учётных данных и сети завершилась успешно")
        fails += [f"{tid}: точка входа упомянула EVETIS ({m})" for m in EVETIS_MARKERS
                  if m in r.stdout + r.stderr]
        r = _run_control(image, t["control_env"])
        if r.returncode == 0:
            fails.append(f"{tid}: точка входа control без учётных данных и сети завершилась успешно")
        fails += [f"{tid}: точка входа control упомянула EVETIS ({m})" for m in EVETIS_MARKERS
                  if m in r.stdout + r.stderr]
    r = _run_control(image, {})
    if r.returncode == 0 or "GCP_PROJECT_ID не задан" not in r.stderr:
        fails.append("без GCP_PROJECT_ID точка входа control не отказала при старте")
    return fails


def main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[1] != "--source-sha":
        print(__doc__, file=sys.stderr)
        return 2
    fails = check(argv[0], argv[2])
    for f in fails:
        print(f"FAIL {f}")
    print("OK: образ прошёл проверку" if not fails else f"отказов: {len(fails)}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
