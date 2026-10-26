"""Mig1b-0 — подготовка боевого контура остатков WB без эффекта на production.

Проверяется:
  * tools/stocks_prod_deploy_check.py — pre/post проверки целевого выката одного Job'а;
  * .github/workflows/deploy-prod.yml, режим only_job=wb-stocks-prod — только этот Job, без запуска Job'а
    и без Scheduler; режим по умолчанию (all) не изменился;
  * infra/terraform — args=["stocks"], боевой env для пути CREATE, IAM prod-SA ровно на три таблицы.
"""
import copy
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tools"))

import stocks_prod_deploy_check as chk  # noqa: E402

D = "europe-west1-docker.pkg.dev/p/wb-loaders/wb-loader@sha256:" + "a" * 64
OLD = "europe-west1-docker.pkg.dev/p/wb-loaders/wb-loader@sha256:" + "b" * 64
SHA = "c" * 40


def job(name, image, gen=1, args=None, env=None, sa="sa-loaders-prod@p.iam.gserviceaccount.com"):
    return {
        "metadata": {"name": name, "generation": gen},
        "spec": {"template": {"spec": {"template": {"spec": {
            "serviceAccountName": sa,
            "containers": [{"image": image, "args": args or [],
                            "env": [{"name": k, "value": v} for k, v in (env or {}).items()]}],
        }}}}},
    }


GOOD_ENV = dict(chk.PROD_STOCKS_ENV, IMAGE_DIGEST=D, GIT_SHA=SHA, ENVIRONMENT="prod", LOADER_NAME="wb-stocks")


def fleet(stocks_prod):
    return [
        stocks_prod,
        job("wb-mart-prod", OLD, 40, ["mart"], {"ENVIRONMENT": "prod"}),
        job("unitka-engine-prod", OLD, 39, ["unitka"], {"ENVIRONMENT": "prod"}),
        job("wb-stocks-shadow", D, 69, ["stocks"], {"ENVIRONMENT": "shadow"}, sa="sa-loaders-shadow@p"),
    ]


BEFORE_TARGET = job("wb-stocks-prod", OLD, 39, ["stocks"], {"ENVIRONMENT": "prod", "LOADER_NAME": "wb-stocks"})
AFTER_TARGET = job("wb-stocks-prod", D, 40, ["stocks"], GOOD_ENV)


def test_pre_requires_digest_of_shadow():
    assert chk.check_pre(job("wb-stocks-shadow", D), D) == []
    assert chk.check_pre(job("wb-stocks-shadow", OLD), D)


def test_post_clean_promotion_passes():
    assert chk.check_post(AFTER_TARGET, fleet(BEFORE_TARGET), fleet(AFTER_TARGET), D, SHA) == []


def test_post_rejects_wrong_image_env_or_sa():
    bad_env = copy.deepcopy(GOOD_ENV)
    bad_env["STOCKS_RAW_TABLE"] = "RAW_WB_STOCKS__CR"
    t = job("wb-stocks-prod", D, 40, ["stocks"], bad_env)
    assert any("STOCKS_RAW_TABLE" in p for p in chk.check_post(t, fleet(BEFORE_TARGET), fleet(t), D, SHA))
    t = job("wb-stocks-prod", OLD, 40, ["stocks"], GOOD_ENV)
    assert any("образ" in p for p in chk.check_post(t, fleet(BEFORE_TARGET), fleet(t), D, SHA))
    t = job("wb-stocks-prod", D, 40, ["stocks"], GOOD_ENV, sa="sa-loaders-shadow@p")
    assert any("SA" in p for p in chk.check_post(t, fleet(BEFORE_TARGET), fleet(t), D, SHA))
    no_t5 = dict(GOOD_ENV, STOCKS_T5_MODE="off")
    t = job("wb-stocks-prod", D, 40, ["stocks"], no_t5)
    assert any("STOCKS_T5_MODE" in p for p in chk.check_post(t, fleet(BEFORE_TARGET), fleet(t), D, SHA))


def test_post_rejects_any_other_job_change():
    after = fleet(AFTER_TARGET)
    after[1] = job("wb-mart-prod", D, 41, ["mart"], {"ENVIRONMENT": "prod"})
    problems = chk.check_post(AFTER_TARGET, fleet(BEFORE_TARGET), after, D, SHA)
    assert any("wb-mart-prod" in p for p in problems)


def test_post_rejects_args_change_by_deploy():
    t = job("wb-stocks-prod", D, 40, ["noop"], GOOD_ENV)
    assert any("args" in p for p in chk.check_post(t, fleet(BEFORE_TARGET), fleet(t), D, SHA))


def test_cli_exit_codes(tmp_path):
    def dump(name, obj):
        p = tmp_path / name
        p.write_text(json.dumps(obj), encoding="utf-8")
        return str(p)
    target = dump("t.json", AFTER_TARGET)
    before = dump("b.json", fleet(BEFORE_TARGET))
    after = dump("a.json", fleet(AFTER_TARGET))
    assert chk.main(["x", "post", target, before, after, D, SHA]) == 0
    assert chk.main(["x", "pre", dump("s.json", job("wb-stocks-shadow", OLD)), D]) == 1
    assert chk.main(["x", "post", target]) == 2


# ── workflow (deploy-prod.yml, режим only_job) ────────────────────────────────
WF = REPO / ".github/workflows/deploy-prod.yml"


def _steps():
    """[(name, if, run)] — разбор текста без yaml-зависимости CI."""
    text = WF.read_text(encoding="utf-8")
    body = text[text.index("    steps:"):]
    out = []
    for chunk in re.split(r"\n      - ", "\n" + body)[1:]:
        name = re.search(r"name:\s*(.+)", chunk)
        cond = re.search(r"\n\s+if:\s*(.+)", "\n" + chunk)
        run = chunk.split("run: |", 1)[1] if "run: |" in chunk else ""
        out.append((name.group(1).strip().strip('"') if name else chunk.split("\n")[0], cond.group(1).strip() if cond else None, run))
    return text, out


def test_only_job_input_defaults_to_all():
    text, _ = _steps()
    block = text[text.index("      only_job:"):text.index("permissions:")]
    assert 'default: "all"' in block and 'options: ["all", "wb-stocks-prod"]' in block


def test_default_mode_steps_unchanged_and_guarded():
    _, steps = _steps()
    deploy_all = [n for n, c, r in steps if "gcloud run jobs update" in r and c == "inputs.only_job == 'all'"]
    assert len(deploy_all) == 9
    # ни один прежний шаг не работает в целевом режиме
    for n, c, r in steps:
        if "gcloud run jobs update" in r and "wb-stocks-prod:" not in n:
            assert c == "inputs.only_job == 'all'", n


def test_stocks_mode_touches_only_wb_stocks_prod_and_never_runs_or_schedules():
    _, steps = _steps()
    stocks = [(n, r) for n, c, r in steps if c == "inputs.only_job == 'wb-stocks-prod'"]
    runs = "\n".join(r for _, r in stocks)
    assert re.findall(r"gcloud run jobs update\s+(\S+)", runs) == ["wb-stocks-prod"]
    whole = WF.read_text(encoding="utf-8")
    for forbidden in ("jobs execute", "gcloud scheduler", "--args", "bq query"):
        assert forbidden not in whole, forbidden
    assert "stocks_prod_deploy_check.py pre" in runs and "stocks_prod_deploy_check.py post" in runs
    assert "${{ inputs" not in runs  # входы — только через env


def test_stocks_mode_env_matches_checker_contract():
    _, steps = _steps()
    runs = "\n".join(r for _, c, r in steps if c == "inputs.only_job == 'wb-stocks-prod'")
    for k, v in chk.PROD_STOCKS_ENV.items():
        assert f"{k}={v}" in runs, k


def test_workflow_stays_on_production_approval_and_pinned_actions():
    text, _ = _steps()
    assert "environment: production" in text
    for ref in re.findall(r"uses:\s*(\S+)", text):
        assert re.search(r"@[0-9a-f]{40}$", ref), ref


# ── terraform ───────────────────────────────────────────────────────────────
TF = REPO / "infra/terraform"


def _block(text, kind, name):
    i = text.index(f'resource "{kind}" "{name}"')
    j = text.find('\nresource "', i + 1)
    return text[i:j if j != -1 else None]


def test_terraform_prod_job_args_env_and_lifecycle():
    text = (TF / "cloud_run_jobs.tf").read_text(encoding="utf-8")
    blk = _block(text, "google_cloud_run_v2_job", "wb_stocks_prod")
    assert 'args = ["stocks"]' in blk
    assert "local.prod_stocks_env" in blk
    for field in ("containers[0].image", "containers[0].env", "client,", "client_version,"):
        assert field in blk, field
    for k, v in chk.PROD_STOCKS_ENV.items():
        assert re.search(rf'{k}\s*=\s*"{v}"', text), k


def test_terraform_prod_sa_writes_exactly_three_stock_tables():
    text = (TF / "bigquery.tf").read_text(encoding="utf-8")
    found = {}
    for name, body in re.findall(r'resource "google_bigquery_table_iam_member" "(prod_write_[a-z0-9_]+)" \{(.*?)\n\}', text, re.S):
        if "loaders_prod" in body:
            found[name] = re.search(r'table_id\s*=\s*(\S+)', body).group(1)
    stock = {n: t for n, t in found.items() if "stock" in n.lower()}
    assert stock == {
        "prod_write_raw_stocks": '"RAW_WB_STOCKS"',
        "prod_write_raw_stocks_t5": '"RAW_WB_STOCKS_T5"',
        "prod_write_stocks_snapshots": '"WB_STOCKS_SNAPSHOTS"',
    }
    for name in stock:
        assert 'role       = "roles/bigquery.dataEditor"' in _block(text, "google_bigquery_table_iam_member", name)
