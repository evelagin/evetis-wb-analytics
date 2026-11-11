"""Охват deploy-prod.yml: каждый режим only_job обновляет ровно свои Job'ы — и ни одного чужого.

Две независимые линии защиты, и тест держит их согласованными:
  * статически — шаги workflow: какие Job'ы обновит каждый режим (разбор текста шагов);
  * в рантайме — tools/deploy_scope_check.py сравнивает снимки Job'ов до и после выката.
Состав режимов задан ОДИН раз (deploy_scope_check.STREAM_JOBS); шаги workflow обязаны с ним совпадать.

Негативные проверки: шаг без условия, шаг потока без своего режима, Job вне режима в снимке —
каждая ломает тест (мутации в тестах ниже).
"""
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tools"))

import deploy_scope_check as scope  # noqa: E402
from test_mig1b0_stocks_prod import job  # noqa: E402  (форма JSON gcloud run jobs list)

WF = REPO / ".github/workflows/deploy-prod.yml"
D = "europe-west1-docker.pkg.dev/p/wb-loaders/wb-loader@sha256:" + "a" * 64
OLD = "europe-west1-docker.pkg.dev/p/wb-loaders/wb-loader@sha256:" + "b" * 64


def _steps(text=None):
    """[(name, if, run)] — разбор текста без yaml-зависимости CI."""
    text = text if text is not None else WF.read_text(encoding="utf-8")
    body = text[text.index("    steps:"):]
    out = []
    for chunk in re.split(r"\n      - ", "\n" + body)[1:]:
        name = re.search(r"name:\s*(.+)", chunk)
        cond = re.search(r"\n\s+if:\s*(.+)", "\n" + chunk)
        run = chunk.split("run: |", 1)[1] if "run: |" in chunk else ""
        out.append((name.group(1).strip().strip('"') if name else chunk.split("\n")[0],
                    cond.group(1).strip() if cond else None, run))
    return out


def _cond_true(cond, mode):
    """Условие вида inputs.only_job == 'X' [|| inputs.only_job == 'Y' ...]. Иной синтаксис — ошибка теста."""
    vals = []
    for p in (x.strip() for x in cond.split("||")):
        m = re.fullmatch(r"inputs\.only_job == '([a-z0-9-]+)'", p)
        assert m, f"неподдерживаемое условие шага выката: {cond}"
        vals.append(m.group(1))
    return mode in vals


def _updated_jobs(run):
    """Job'ы, которые обновляет шаг: явные имена и цикл `for JOB in a b c; do`."""
    names = set(re.findall(r"gcloud run jobs update\s+\"?\$?\{?([A-Za-z0-9_-]+)", run))
    loop = re.search(r"for JOB in ([^;]+); do", run)
    if {"JOB"} & names:
        assert loop, "gcloud run jobs update $JOB без цикла for JOB in …"
        names = (names - {"JOB"}) | set(loop.group(1).split())
    return names


def _touched(steps, mode):
    out = set()
    for n, c, r in steps:
        if "gcloud run jobs update" not in r:
            continue
        assert c, f"шаг выката без условия only_job: {n}"
        if _cond_true(c, mode):
            out |= _updated_jobs(r)
    return out


# ── статика: шаги workflow ↔ состав режимов ───────────────────────────────────

def test_options_are_exactly_the_checker_modes():
    text = WF.read_text(encoding="utf-8")
    block = text[text.index("      only_job:"):text.index("permissions:")]
    opts = re.search(r"options: \[(.*)\]", block).group(1)
    assert [o.strip().strip('"') for o in opts.split(",")] == list(scope.MODES)


@pytest.mark.parametrize("mode", scope.MODES)
def test_each_mode_touches_exactly_its_jobs(mode):
    assert _touched(_steps(), mode) == scope.allowed(mode), mode


def test_stocks_mode_does_not_touch_any_m2_m5_job():
    # F1: остатки — ни одного Job'а новых потоков (и наоборот).
    t = _touched(_steps(), "wb-stocks-prod")
    for stream, jobs in scope.STREAM_JOBS.items():
        if stream != "wb-stocks-prod":
            assert not (t & jobs), (stream, t & jobs)
    for stream in scope.STREAM_JOBS:
        if stream != "wb-stocks-prod":
            assert "wb-stocks-prod" not in _touched(_steps(), stream), stream


def test_stream_modes_are_disjoint():
    seen = {}
    for stream, jobs in scope.STREAM_JOBS.items():
        for j in jobs:
            assert j not in seen, f"{j} в двух режимах: {seen.get(j)} и {stream}"
            seen[j] = stream


def test_unknown_mode_updates_nothing():
    assert _touched(_steps(), "wb-everything") == set()


def test_mutation_unconditioned_step_is_caught():
    text = WF.read_text(encoding="utf-8")
    cond = "        if: inputs.only_job == 'all' || inputs.only_job == 'wb-ads'\n"
    assert text.count(cond) == 1
    with pytest.raises(AssertionError, match="без условия"):
        _touched(_steps(text.replace(cond, "")), "wb-stocks-prod")


def test_mutation_stream_step_leaking_into_stocks_is_caught():
    text = WF.read_text(encoding="utf-8")
    cond = "if: inputs.only_job == 'all' || inputs.only_job == 'wb-finance'"
    leaked = text.replace(cond, cond + " || inputs.only_job == 'wb-stocks-prod'")
    assert _touched(_steps(leaked), "wb-stocks-prod") != scope.allowed("wb-stocks-prod")


def test_guard_all_runs_first_and_before_any_token():
    steps = _steps()
    name, cond, run = steps[0]
    assert name.startswith("Guard") and cond is None
    assert '[ "$ONLY_JOB" = "all" ]' in run and scope.CONFIRM_ALL_PHRASE in run and "exit 1" in run
    text = WF.read_text(encoding="utf-8")
    assert text.index("Guard:") < text.index("google-github-actions/auth")
    assert "CONFIRM_ALL: ${{ inputs.confirm_all }}" in text and "ONLY_JOB: ${{ inputs.only_job }}" in text


def test_guard_logic_in_shell():
    run = _steps()[0][2]
    script = "\n".join(line[10:] if line.startswith("          ") else line for line in run.splitlines())
    def rc(mode, confirm):
        return subprocess.run(["bash", "-c", script], env={"ONLY_JOB": mode, "CONFIRM_ALL": confirm, "PATH": "/usr/bin:/bin"},
                              capture_output=True).returncode
    assert rc("all", "") == 1
    assert rc("all", "promote-all-prod") == 1
    assert rc("all", "PROMOTE-ALL-PROD ") == 1
    assert rc("all", scope.CONFIRM_ALL_PHRASE) == 0
    for m in scope.STREAM_JOBS:
        assert rc(m, "") == 0, m


def test_scope_check_runs_in_every_mode_and_after_failures():
    text = WF.read_text(encoding="utf-8")
    steps = _steps()
    before = [s for s in steps if "> scope_before.json" in s[2]]
    after = [s for s in steps if "deploy_scope_check.py check" in s[2]]
    assert len(before) == 1 and before[0][1] is None
    assert len(after) == 1 and after[0][1] == "always() && steps.scope_before.outcome == 'success'"
    assert steps.index(after[0]) == len(steps) - 1
    assert '"$ONLY_JOB"' in after[0][2]
    # снимок ДО — раньше любого обновления
    first_update = min(i for i, s in enumerate(steps) if "gcloud run jobs update" in s[2])
    assert steps.index(before[0]) < first_update
    assert "${{ inputs" not in "\n".join(r for _, _, r in steps)  # входы — только через env
    for forbidden in ("jobs execute", "gcloud scheduler", "--args", "bq query"):
        assert forbidden not in text, forbidden


# ── рантайм: tools/deploy_scope_check.py ──────────────────────────────────────

def _fleet(image=OLD):
    names = sorted(scope.allowed("all")) + [
        "wb-stocks-shadow", "wb-orders-shadow", "heldi-controller", "ozon-postings-prod", "unitka-web",
        "ingest-cutover-prod", "ingest-qualify-prod",
    ]
    return [job(n, image, gen=3) for n in names]


def _promote(fleet, names, digest=D):
    out = json.loads(json.dumps(fleet))
    for j in out:
        if j["metadata"]["name"] in names:
            c = j["spec"]["template"]["spec"]["template"]["spec"]["containers"][0]
            c["image"] = digest
            j["metadata"]["generation"] += 1
    return out


@pytest.mark.parametrize("mode", scope.MODES)
def test_clean_promotion_passes(mode):
    before = _fleet()
    assert scope.check(mode, before, _promote(before, scope.allowed(mode)), D) == []


@pytest.mark.parametrize("mode", [m for m in scope.STREAM_JOBS])
def test_stream_mode_rejects_touching_any_other_job(mode):
    before = _fleet()
    for other in ["wb-mart-prod", "heldi-controller", "wb-stocks-shadow", "ozon-postings-prod"] + sorted(
        scope.allowed("all") - scope.allowed(mode)
    ):
        after = _promote(before, scope.allowed(mode) | {other})
        problems = scope.check(mode, before, after, D)
        assert any(other in p and "вне режима" in p for p in problems), (mode, other)


def test_all_mode_still_rejects_non_listed_jobs():
    before = _fleet()
    for other in ["heldi-controller", "wb-stocks-shadow", "ozon-postings-prod", "unitka-web"]:
        problems = scope.check("all", before, _promote(before, scope.allowed("all") | {other}), D)
        assert any(other in p for p in problems), other


def test_missed_job_of_mode_is_rejected():
    before = _fleet()
    want = scope.allowed("wb-statistics")
    problems = scope.check("wb-statistics", before, _promote(before, want - {"wb-sales-prod"}), D)
    assert any("wb-sales-prod" in p and "не обновлён" in p for p in problems)


def test_absent_stream_jobs_are_fine_before_terraform_creates_them():
    before = [j for j in _fleet() if j["metadata"]["name"] not in scope.allowed("wb-ads")]
    assert scope.check("wb-ads", before, before, D) == []


def test_created_or_deleted_job_is_rejected():
    before = _fleet()
    after = _promote(before, scope.allowed("wb-finance")) + [job("wb-finance-prod-2", D)]
    assert any("состав" in p for p in scope.check("wb-finance", before, after, D))
    assert any("состав" in p for p in scope.check("wb-finance", before, before[1:], D))


def test_args_or_sa_change_within_mode_is_rejected():
    before = _fleet()
    after = _promote(before, scope.allowed("wb-finance"))
    for j in after:
        if j["metadata"]["name"] == "wb-finance-prod":
            spec = j["spec"]["template"]["spec"]["template"]["spec"]
            spec["containers"][0]["args"] = ["finance", "--backfill"]
            spec["serviceAccountName"] = "sa-other@x.iam.gserviceaccount.com"
    problems = scope.check("wb-finance", before, after, D)
    assert any("args" in p for p in problems) and any("SA" in p for p in problems)


def test_unknown_mode_is_input_error(tmp_path):
    f = tmp_path / "x.json"
    f.write_text("[]")
    cmd = [sys.executable, str(REPO / "tools/deploy_scope_check.py")]
    assert subprocess.run(cmd + ["check", "wb-everything", str(f), str(f), D], capture_output=True).returncode == 2
    assert subprocess.run(cmd + ["allowed", "wb-everything"], capture_output=True).returncode == 2
    r = subprocess.run(cmd + ["allowed", "wb-supplies-ref"], capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout.split() == ["ref-sync-prod", "wb-supplies-prod"]


def test_cli_exit_codes(tmp_path):
    before = _fleet()
    b, ok, bad = tmp_path / "b.json", tmp_path / "ok.json", tmp_path / "bad.json"
    b.write_text(json.dumps(before))
    ok.write_text(json.dumps(_promote(before, scope.allowed("wb-stocks-prod"))))
    bad.write_text(json.dumps(_promote(before, {"wb-stocks-prod", "wb-mart-prod"})))
    cmd = [sys.executable, str(REPO / "tools/deploy_scope_check.py"), "check", "wb-stocks-prod", str(b)]
    assert subprocess.run(cmd + [str(ok), D], capture_output=True).returncode == 0
    assert subprocess.run(cmd + [str(bad), D], capture_output=True).returncode == 1


# ── этап C: режим ingest-cutover (плоскость управления, вне all) ───────────────────────────────

def test_ingest_cutover_mode_is_exactly_the_coordinator():
    assert scope.allowed("ingest-cutover") == {"ingest-cutover-prod", "ingest-qualify-prod"}
    assert _touched(_steps(), "ingest-cutover") == {"ingest-cutover-prod", "ingest-qualify-prod"}


def test_all_mode_never_touches_the_coordinator():
    assert not {"ingest-cutover-prod", "ingest-qualify-prod"} & scope.allowed("all")
    assert not {"ingest-cutover-prod", "ingest-qualify-prod"} & _touched(_steps(), "all")
    before = _fleet()
    problems = scope.check("all", before, _promote(before, scope.allowed("all") | {"ingest-cutover-prod"}), D)
    assert any("ingest-cutover-prod" in p and "вне режима" in p for p in problems)


def test_no_stream_mode_touches_the_coordinator_and_vice_versa():
    for mode in scope.STREAM_JOBS:
        assert "ingest-cutover-prod" not in _touched(_steps(), mode), mode
    before = _fleet()
    for other in sorted(scope.allowed("all")):
        after = _promote(before, {"ingest-cutover-prod", other})
        assert any(other in p and "вне режима" in p for p in scope.check("ingest-cutover", before, after, D)), other


def test_ingest_cutover_guard_and_absent_job():
    run = _steps()[0][2]
    script = "\n".join(line[10:] if line.startswith("          ") else line for line in run.splitlines())
    rc = subprocess.run(["bash", "-c", script], env={"ONLY_JOB": "ingest-cutover", "CONFIRM_ALL": "", "PATH": "/usr/bin:/bin"},
                        capture_output=True).returncode
    assert rc == 0
    before = [j for j in _fleet() if j["metadata"]["name"] not in ("ingest-cutover-prod", "ingest-qualify-prod")]
    assert scope.check("ingest-cutover", before, before, D) == []


# ── этап C, ревью #426 (M5): предпроверка gate владения до выката ─────────────────────────────

import writer_gate_deploy_check as wg  # noqa: E402

ROWS = [{"stream": s, "owner": "APPS_SCRIPT", "epoch": "1"} for s in wg.STREAMS]
POLICY_OK = {"bindings": [{"role": "roles/bigquery.dataViewer", "members": [
    "serviceAccount:sa-wb-statistics-prod@p.iam.gserviceaccount.com", "serviceAccount:sa-loaders-prod@p.iam.gserviceaccount.com"]}]}


def _gated_fleet(stocks_gate="1"):
    from test_mig1b0_stocks_prod import job as j
    return [
        j("wb-orders-prod", OLD, 2, ["orders"], {"ENVIRONMENT": "prod"}, sa="sa-wb-statistics-prod@p.iam.gserviceaccount.com"),
        j("wb-stocks-prod", OLD, 5, ["stocks"], {"ENVIRONMENT": "prod", "WRITER_GATE_STOCKS": stocks_gate}, sa="sa-loaders-prod@p.iam.gserviceaccount.com"),
        j("wb-mart-prod", OLD, 7, ["mart"], {"ENVIRONMENT": "prod"}, sa="sa-loaders-prod@p.iam.gserviceaccount.com"),
    ]


def test_precheck_step_runs_in_every_mode_before_any_update():
    steps = _steps()
    pre = [i for i, s in enumerate(steps) if "writer_gate_deploy_check.py check" in s[2]]
    assert len(pre) == 1 and steps[pre[0]][1] is None
    first_update = min(i for i, s in enumerate(steps) if "gcloud run jobs update" in s[2])
    scope_before = min(i for i, s in enumerate(steps) if "> scope_before.json" in s[2])
    assert scope_before < pre[0] < first_update
    run = steps[pre[0]][2]
    assert "bq head" in run and "bq get-iam-policy" in run and "writer_gate_deploy_check.py needs" in run


def test_precheck_scope_and_verdicts():
    fleet = _gated_fleet()
    assert set(wg.gated_jobs("wb-statistics", fleet)) == {"wb-orders-prod"}
    assert set(wg.gated_jobs("all", fleet)) == {"wb-orders-prod", "wb-stocks-prod"}
    assert wg.gated_jobs("wb-stocks-prod", _gated_fleet(stocks_gate="")) == {}  # M1: gate остатков выключен
    assert wg.gated_jobs("ingest-cutover", fleet) == {}
    assert wg.check("all", fleet, ROWS, POLICY_OK) == []
    assert any("потоки" in p for p in wg.check("all", fleet, ROWS[:5], POLICY_OK))
    assert any("не читает" in p for p in wg.check("wb-statistics", fleet, ROWS, {"bindings": []}))
    bad = [dict(r) for r in ROWS]
    bad[0]["owner"] = "BOTH"
    assert any("owner" in p for p in wg.check("wb-statistics", fleet, bad, POLICY_OK))


def test_precheck_cli(tmp_path):
    f, rws, pol = tmp_path / "jobs.json", tmp_path / "rows.json", tmp_path / "pol.json"
    f.write_text(json.dumps(_gated_fleet()))
    rws.write_text(json.dumps(ROWS))
    pol.write_text(json.dumps({"bindings": []}))
    cmd = [sys.executable, str(REPO / "tools/writer_gate_deploy_check.py")]
    assert subprocess.run(cmd + ["needs", "wb-statistics", str(f)], capture_output=True, text=True).stdout.strip() == "yes"
    assert subprocess.run(cmd + ["needs", "ingest-cutover", str(f)], capture_output=True, text=True).stdout.strip() == "no"
    assert subprocess.run(cmd + ["check", "wb-statistics", str(f), str(rws), str(pol)], capture_output=True).returncode == 1
    pol.write_text(json.dumps(POLICY_OK))
    assert subprocess.run(cmd + ["check", "wb-statistics", str(f), str(rws), str(pol)], capture_output=True).returncode == 0
