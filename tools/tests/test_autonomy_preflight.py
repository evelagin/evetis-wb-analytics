"""Фаза 0 ввода AE: preflight аутентификации Claude API — только обмен токена, ничего больше.

Доказательства машинные и двух видов:
  * конфигурация workflows: маленький интерпретатор условий `if:`/`needs:` перебирает ВСЕ сочетания
    выключателей (AE_ENABLED, AE_PREFLIGHT_ENABLED, вход preflight, вход mode reusable-файлов) и
    показывает, какие job'ы вообще могут выполниться; плюс запреты на содержимое preflight-job'ов;
  * код `anthropic_scope.preflight`: решение, отказ до обмена, отсутствие учётных данных в выводе.
Нумерация в именах тестов — пункты ACK владельца (2026-09-25).
"""
from __future__ import annotations

import base64
import email.message
import io
import itertools
import json
import re
import urllib.error
from datetime import date
from pathlib import Path

import pytest

from tools.autonomy import anthropic_scope as S
from tools.autonomy.policy import load_policy
from tools.autonomy.redact import find_secrets
from tools.autonomy.wif_check import MAIN, claims

REPO = Path(__file__).resolve().parents[2]
WF = REPO / ".github" / "workflows"
RUN = WF / "autonomy-run.yml"
REUSABLE = {"autonomy-engineer.yml": "engineer", "autonomy-review.yml": "reviewer"}
PREFLIGHT_JOBS = {"preflight-enabled", "preflight-engineer", "preflight-reviewer"}
RT = load_policy()["anthropic_runtime"]
SPEC = json.loads((REPO / "quality/autonomy/anthropic_federation.json").read_text(encoding="utf-8"))


def text(p: Path) -> str:
    return p.read_text(encoding="utf-8")


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


def field(body: str, key: str) -> str | None:
    m = re.search(rf"^    {key}: (.+)$", body, re.M)
    return m.group(1).strip() if m else None


def needs_of(body: str) -> list[str]:
    n = field(body, "needs")
    if not n:
        return []
    return [x.strip() for x in n.strip("[]").split(",")] if n.startswith("[") else [n]


# ---------------------------------------------------- интерпретатор условий GitHub ---
_STATUS = re.compile(r"\b(always|success|failure|cancelled)\(\)")


def gh_eval(expr: str, ctx: dict) -> bool:
    """Подмножество выражений GitHub, которое встречается в `if:` AE: vars/inputs/needs, ==, !=, !, &&,
    ||, always(). Сравнение строк без учёта регистра, отсутствующая переменная — пустая строка."""
    e = expr.strip()
    if e.startswith("${{"):
        e = e[3:-2]

    def ref(m):
        root, *rest = m.group(0).split(".")
        if root == "vars":
            v = ctx["vars"].get(rest[0]) or ""
        elif root == "inputs":
            v = ctx["inputs"].get(rest[0], "")
        else:  # needs.<job>.result
            v = ctx["results"][rest[0]]
        return repr(v.lower() if isinstance(v, str) else v)

    py = re.sub(r"\b(vars|inputs|needs)\.[A-Za-z0-9_.-]+", ref, e)
    py = re.sub(r"'([^']*)'", lambda m: repr(m.group(1).lower()), py)
    py = py.replace("&&", " and ").replace("||", " or ")
    py = re.sub(r"!(?!=)", " not ", py)
    py = _STATUS.sub("True", py)
    assert re.fullmatch(r"[\sA-Za-z0-9_'=!()\-]*", py), f"неподдержанное выражение: {expr}"
    return bool(eval(py, {"__builtins__": {}}, {}))  # noqa: S307 — только литералы после подстановки


def reusable_default_mode(src: str) -> str:
    return re.search(r"      mode:\n(?:        .*\n)*?        default: (\S+)", src).group(1)


def run_reusable(name: str, mode: str | None, vars_: dict) -> set[tuple[str, str]]:
    src = text(WF / name)
    if name not in REUSABLE:                     # gate/test: входа mode нет — худший случай, все job'ы
        assert mode is None, f"{name} не принимает mode"
        return {(name, j) for j in jobs(src)}
    ctx = {"vars": vars_, "inputs": {"mode": reusable_default_mode(src) if mode is None else mode}}
    return {(name, j) for j, body in jobs(src).items() if field(body, "if") is None or gh_eval(field(body, "if"), ctx)}


def simulate(vars_: dict, inputs: dict) -> tuple[set[str], set[tuple[str, str]]]:
    """Какие job'ы autonomy-run.yml и какие job'ы вызванных reusable-файлов выполнятся.
    Худший случай для экспозиции: каждый выполнившийся job считается успешным."""
    results, inner = {}, set()
    ctx = {"vars": vars_, "inputs": {"preflight": False, "objective_path": "", "run_id": "", **inputs},
           "results": results}
    for name, body in jobs(text(RUN)).items():
        cond, deps = field(body, "if"), needs_of(body)
        deps_ok = all(results[d] == "success" for d in deps)
        if cond is None:
            runs = deps_ok
        else:
            runs = gh_eval(cond, ctx) and (bool(_STATUS.search(cond)) or deps_ok)
        results[name] = "success" if runs else "skipped"
        uses = re.search(r"^    uses: \./\.github/workflows/(\S+)", body, re.M)
        if runs and uses:
            mode = re.search(r"^      mode: (\S+)", body, re.M)
            inner |= run_reusable(uses.group(1), mode.group(1) if mode else None, vars_)
    return {j for j, r in results.items() if r == "success"}, inner


SWITCH = [None, "true", "false"]
COMBOS = list(itertools.product(SWITCH, SWITCH, [False, True]))
ALL_RUN_JOBS = set(jobs(text(RUN)))
PREFLIGHT_INNER = {("autonomy-engineer.yml", "preflight"), ("autonomy-review.yml", "preflight")}


def _vars(ae, pre):
    return {k: v for k, v in (("AE_ENABLED", ae), ("AE_PREFLIGHT_ENABLED", pre)) if v is not None}


def test_interpreter_sanity():
    ctx = {"vars": {"A": "true"}, "inputs": {"p": False, "m": "run"}, "results": {"x": "skipped"}}
    assert gh_eval("vars.A == 'true' && !inputs.p", ctx) and not gh_eval("vars.B == 'true'", ctx)
    assert gh_eval("vars.B != 'true'", ctx) and gh_eval("inputs.m == 'RUN'", ctx)
    assert not gh_eval("always() && needs.x.result == 'success'", ctx)
    with pytest.raises(AssertionError):
        gh_eval("contains(github.ref, 'x')", ctx)


# --------------------------------------------------- 6, 9, 10: полный перебор выключателей ---
@pytest.mark.parametrize("ae,pre,preflight", COMBOS)
def test_06_09_10_switch_matrix(ae, pre, preflight):
    top, inner = simulate(_vars(ae, pre), {"preflight": preflight})
    if preflight:
        # 6: ни одного job'а прохода; только аутентификация в reusable-файлах
        assert top <= PREFLIGHT_JOBS and inner <= PREFLIGHT_INNER
        # 10: AE_ENABLED preflight не включает; при включённом AE_ENABLED preflight не идёт вовсе
        assert bool(top) == (pre == "true" and ae != "true")
        assert inner == (PREFLIGHT_INNER if top else set())
    else:
        # 9: AE_PREFLIGHT_ENABLED не включает обычный AE и не открывает preflight-job'ы
        assert not top & PREFLIGHT_JOBS and not inner & PREFLIGHT_INNER
        # семантика AE_ENABLED без изменений: проход идёт ровно тогда, когда AE_ENABLED == 'true'
        assert bool(top) == (ae == "true")
        if top:
            assert top == ALL_RUN_JOBS - PREFLIGHT_JOBS
            assert inner >= {("autonomy-engineer.yml", "engineer"), ("autonomy-review.yml", "review")}


def test_06_no_run_job_depends_on_preflight_and_every_run_job_depends_on_kill_switch():
    js = jobs(text(RUN))

    def closure(j):
        seen, todo = set(), list(needs_of(js[j]))
        while todo:
            d = todo.pop()
            if d not in seen:
                seen.add(d)
                todo += needs_of(js[d])
        return seen

    for j in ALL_RUN_JOBS - PREFLIGHT_JOBS - {"enabled"}:
        assert "enabled" in closure(j), j
        assert not closure(j) & PREFLIGHT_JOBS, j
    for j in PREFLIGHT_JOBS - {"preflight-enabled"}:
        assert closure(j) == {"preflight-enabled"}, j


def test_preflight_input_is_boolean_default_false():
    on = text(RUN).split("\non:\n", 1)[1].split("\npermissions:", 1)[0]
    block = on.split("      preflight:\n", 1)[1]
    assert "type: boolean" in block and "default: false" in block


# ------------------------------------------------ 1–5: содержимое preflight-job'ов ---
FORBIDDEN_IN_PREFLIGHT = (
    "claude-code", "setup-node", "npm ", "--engineer", "--reviewer", "agent-run", "cli review",   # 1, 2
    "tools.autonomy.cli", "refresh_anthropic_oidc", "ANTHROPIC_IDENTITY_TOKEN_FILE",               # 1, 2
    "google-github-actions", "workload_identity_provider", "gcloud", "bq ",                          # 3
    "AE_READER_SA", "sa-ae-reader", "service_account:",                                             # 4
    "state_clone", "state_push", "autonomy-state", "STATE_DIR", "STATE_SHA", "objective",          # 5
    "upload-artifact", "download-artifact", "pip install", "contents: write", "actions: write",
    "pull-requests: write", "issues: write", "secrets.")


@pytest.mark.parametrize("name,role", sorted(REUSABLE.items()))
def test_01_to_05_preflight_job_is_authentication_only(name, role):
    body = jobs(text(WF / name))["preflight"]
    for bad in FORBIDDEN_IN_PREFLIGHT:
        assert bad not in body, f"{name}/preflight: {bad}"
    runs = re.findall(r"^\s*(?:- )?run: (.+)$", body, re.M)
    assert [r for r in runs if "python" in r] == [f"python3 -m tools.autonomy.anthropic_scope preflight --role {role}"]
    assert set(re.findall(r"uses: ([\w./-]+)@", body)) == {"actions/checkout"}          # stdlib: ни pip, ни setup-python
    assert "persist-credentials: false" in body and "id-token: write" in body and "contents: read" in body


@pytest.mark.parametrize("name", sorted(REUSABLE))
def test_06_run_job_is_unreachable_in_preflight_mode_and_preflight_needs_its_switch(name):
    for mode in ("preflight", "", "x", "none"):
        for pre in SWITCH:
            got = run_reusable(name, mode, _vars("true", pre))
            assert got <= PREFLIGHT_INNER, (mode, pre, got)
            assert bool(got) == (mode == "preflight" and pre == "true"), (mode, pre, got)


def test_01_module_talks_only_to_the_token_endpoint():
    src = text(REPO / "tools/autonomy/anthropic_scope.py")
    assert set(re.findall(r"https://[^\s\"'&]+", src)) == {"https://api.anthropic.com/v1/oauth/token",
                                                          "https://api.anthropic.com"}
    for bad in ("/v1/messages", "/v1/complete", "subprocess", "claude ", "anthropic import", "import anthropic"):
        assert bad not in src, bad


def test_run_preflight_jobs_call_exactly_the_federated_files_in_preflight_mode():
    js = jobs(text(RUN))
    for job, wf in (("preflight-engineer", "autonomy-engineer.yml"), ("preflight-reviewer", "autonomy-review.yml")):
        assert field(js[job], "uses") == f"./.github/workflows/{wf}"
        assert re.search(r"^      mode: preflight$", js[job], re.M)
        assert "id-token: write" in js[job] and "contents: write" not in js[job]
    assert "AE_ENABLED" not in js["preflight-enabled"].split("steps:")[0].replace("vars.AE_ENABLED != 'true'", "")


# --------------------------------------------------------- 7: раздельные идентичности ---
def test_07_engineer_and_reviewer_preflight_use_distinct_variables():
    eng = jobs(text(WF / "autonomy-engineer.yml"))["preflight"]
    rev = jobs(text(WF / "autonomy-review.yml"))["preflight"]
    assert "ANTHROPIC_FEDERATION_RULE_ID: ${{ vars.AE_ANTHROPIC_FEDERATION_RULE_ID }}" in eng
    assert "ANTHROPIC_SERVICE_ACCOUNT_ID: ${{ vars.AE_ANTHROPIC_SERVICE_ACCOUNT_ID }}" in eng
    assert "REVIEWER" not in eng
    assert "ANTHROPIC_FEDERATION_RULE_ID: ${{ vars.AE_ANTHROPIC_REVIEWER_FEDERATION_RULE_ID }}" in rev
    assert "ANTHROPIC_SERVICE_ACCOUNT_ID: ${{ vars.AE_ANTHROPIC_REVIEWER_SERVICE_ACCOUNT_ID }}" in rev
    assert "ENGINEER_RULE_ID: ${{ vars.AE_ANTHROPIC_FEDERATION_RULE_ID }}" in rev
    assert "ENGINEER_SERVICE_ACCOUNT_ID: ${{ vars.AE_ANTHROPIC_SERVICE_ACCOUNT_ID }}" in rev


# Фиктивные ID правильного вида (не живые): ревьюер отличается от инженера.
ENG_ENV = {"ANTHROPIC_FEDERATION_RULE_ID": "fdrl_" + "E" * 24, "ANTHROPIC_SERVICE_ACCOUNT_ID": "svac_" + "e" * 24,
           "ANTHROPIC_WORKSPACE_ID": "wrkspc_" + "W1" * 12, "ANTHROPIC_ORGANIZATION_ID": "0" * 8 + "-0000-4000-8000-" + "0" * 12}
REV_ENV = {**ENG_ENV, "ANTHROPIC_FEDERATION_RULE_ID": "fdrl_" + "R" * 24, "ANTHROPIC_SERVICE_ACCOUNT_ID": "svac_" + "r" * 24,
           "ENGINEER_RULE_ID": ENG_ENV["ANTHROPIC_FEDERATION_RULE_ID"],
           "ENGINEER_SERVICE_ACCOUNT_ID": ENG_ENV["ANTHROPIC_SERVICE_ACCOUNT_ID"]}
JOB = {"engineer": "autonomy-engineer.yml", "reviewer": "autonomy-review.yml"}


def fake_jwt(role: str = "engineer", workflow: str = "autonomy-run.yml", **kw) -> tuple[str, str]:
    c = claims(workflow, kw.pop("ref", MAIN), job_workflow=kw.pop("job_workflow", JOB[role]), **kw)
    jti = "jti-" + "q" * 20
    c.update(aud="https://api.anthropic.com", jti=jti, iat=1000, exp=1300)
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")  # noqa: E731
    return enc({"alg": "RS256", "typ": "JWT"}) + "." + enc(c) + "." + "s" * 40, jti


class Exch:
    """Подмена обмена: считает вызовы и отдаёт заданный ответ."""
    def __init__(self, scope="workspace:developer", ttl=600, rid="req_" + "0" * 20, error=None):
        self.calls, self.r, self.error = 0, (scope, ttl, rid), error

    def __call__(self, assertion):
        self.calls += 1
        if self.error:
            raise self.error
        return self.r


def pf(role="engineer", env=None, today=date(2026, 9, 26), jwt=None, exch=None):
    exch = exch or Exch()
    token = jwt or fake_jwt(role)[0]
    ev = S.preflight(role, REV_ENV if role == "reviewer" and env is None else (env or ENG_ENV), RT, SPEC, today,
                     oidc=lambda: token, exch=exch)
    return ev, exch


@pytest.mark.parametrize("mut", [
    {"ENGINEER_RULE_ID": REV_ENV["ANTHROPIC_FEDERATION_RULE_ID"]},
    {"ENGINEER_SERVICE_ACCOUNT_ID": REV_ENV["ANTHROPIC_SERVICE_ACCOUNT_ID"]},
    {"ENGINEER_RULE_ID": ""},
])
def test_07_reviewer_with_engineer_identity_fails_before_any_network(mut):
    called = []
    ev = S.preflight("reviewer", {**REV_ENV, **mut}, RT, SPEC, date(2026, 9, 26),
                     oidc=lambda: called.append("oidc") or "x", exch=Exch())
    assert ev["decision"] == "FAIL" and not called


@pytest.mark.parametrize("key,value", [("ANTHROPIC_WORKSPACE_ID", ""), ("ANTHROPIC_WORKSPACE_ID", "wrkspc_x"),
                                       ("ANTHROPIC_FEDERATION_RULE_ID", "svac_" + "e" * 24),
                                       ("ANTHROPIC_ORGANIZATION_ID", "not-a-uuid"),
                                       ("ANTHROPIC_SERVICE_ACCOUNT_ID", None)])
def test_wrong_rule_sa_workspace_config_fails_before_any_network(key, value):
    env = {**ENG_ENV, key: value} if value is not None else {k: v for k, v in ENG_ENV.items() if k != key}
    ev, ex = pf(env=env)
    assert ev["decision"] == "FAIL" and ex.calls == 0 and key in ev["reason"]


# --------------------------------------------------------- 8, 11: claims и обход ---
@pytest.mark.parametrize("role", ["engineer", "reviewer"])
def test_08_preflight_token_matches_the_role_rule_and_passes(role):
    ev, ex = pf(role)
    assert ex.calls == 1 and ev["decision"] == "PASS_WITH_EXCEPTION" and "mismatches_vs_spec" not in ev
    assert ev["claims"]["workflow_ref"].endswith("/autonomy-run.yml@refs/heads/main")
    assert ev["claims"]["job_workflow_ref"].endswith(f"/{JOB[role]}@refs/heads/main")
    assert ev["claims"]["event_name"] == "workflow_dispatch"


@pytest.mark.parametrize("role", ["engineer", "reviewer"])
@pytest.mark.parametrize("kw", [
    {"workflow": "ci.yml"},                                         # другой вызывающий workflow
    {"workflow": "autonomy-run.yml", "ref": "refs/heads/ae/x-1234abcd"},
    {"workflow": "autonomy-run.yml", "event": "push"},
    {"workflow": "autonomy-run.yml", "runner_environment": "self-hosted"},
    {"workflow": "autonomy-run.yml", "repository_id": "1111111111"},
    {"workflow": "autonomy-run.yml", "job_workflow": "autonomy-test.yml"},
])
def test_11_other_callers_or_contexts_never_reach_the_exchange(role, kw):
    ev, ex = pf(role, jwt=fake_jwt(role, **kw)[0])
    assert ev["decision"] == "FAIL" and ex.calls == 0 and ev["mismatches_vs_spec"]


def test_08_reviewer_token_is_not_accepted_for_the_engineer_rule():
    ev, ex = pf("engineer", jwt=fake_jwt("reviewer")[0])
    assert ev["decision"] == "FAIL" and ex.calls == 0


@pytest.mark.parametrize("name", sorted(REUSABLE))
def test_11_reusable_files_have_no_direct_trigger_and_preflight_guards_the_caller(name):
    src = text(WF / name)
    on = src.split("\non:\n", 1)[1].split("\npermissions:", 1)[0]
    assert re.findall(r"^  (\w+):", on, re.M) == ["workflow_call"]
    body = jobs(src)["preflight"]
    assert 'GITHUB_WORKFLOW_REF" = "$GITHUB_REPOSITORY/.github/workflows/autonomy-run.yml@refs/heads/main"' in body
    assert '"$GITHUB_EVENT_NAME" = "workflow_dispatch"' in body and '"$GITHUB_REF" = "refs/heads/main"' in body
    assert "vars.AE_PREFLIGHT_ENABLED == 'true'" in field(body, "if")
    guard = body.index("GITHUB_WORKFLOW_REF")
    assert guard < body.index("anthropic_scope preflight")


def test_run_trigger_is_dispatch_only():
    on = text(RUN).split("\non:\n", 1)[1].split("\npermissions:", 1)[0]
    assert re.findall(r"^  (\w+):", on, re.M) == ["workflow_dispatch"]


# ------------------------------------------------------ 12, 13: решение по scope/TTL ---
@pytest.mark.parametrize("role", ["engineer", "reviewer"])
def test_12_developer_passes_only_inside_the_exception_window(role):
    assert pf(role, today=date(2026, 10, 9))[0]["decision"] == "PASS_WITH_EXCEPTION"
    ev = pf(role, today=date(2026, 10, 10))[0]
    assert ev["decision"] == "FAIL" and "истекло" in ev["reason"]
    assert pf(role, exch=Exch(scope="workspace:inference"))[0]["decision"] == "PASS"


@pytest.mark.parametrize("scope,ttl", [("org:admin", 600), ("workspace:developer org:admin", 600),
                                       ("workspace:unknown", 600), ("workspace:manage_tunnels", 600),
                                       ("", 600), (None, 600), ("workspace:developer", 601),
                                       ("workspace:inference", 3600), ("workspace:inference", None),
                                       ("workspace:inference", 0), ("workspace:inference", True),
                                       ("workspace:developer", False), ("workspace:inference", 600.0)])
def test_13_admin_unknown_scope_or_long_ttl_fail_closed(scope, ttl):
    ev = pf(exch=Exch(scope=scope, ttl=ttl))[0]
    assert ev["decision"] == "FAIL"


def test_13_rejected_exchange_is_fail_with_request_id():
    ev = pf(exch=Exch(error=S.ExchangeRejected(401, "req_" + "1" * 20, "workspace_not_enabled")))[0]
    assert ev["decision"] == "FAIL" and ev["request_id"] == "req_" + "1" * 20 and ev["error"] == "workspace_not_enabled"


# ------------------------------------------------ 14: учётные данные не выходят наружу ---
class FakeResp:
    def __init__(self, payload: dict, headers: dict):
        self._b, self.headers = json.dumps(payload).encode(), headers

    def read(self, *a):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _run_end_to_end(monkeypatch, tmp_path, capsys, role, token_reply):
    """Настоящие _oidc/exchange/run_preflight; сеть подменена на уровне urlopen."""
    jwt, jti = fake_jwt(role)
    claude_token = "-".join(("sk", "ant", "oat01", "Z" * 40))
    seen = []

    def urlopen(req, timeout=None):
        url = req if isinstance(req, str) else req.full_url
        seen.append(url)
        if url.startswith("https://actions.example/"):
            return FakeResp({"value": jwt}, {})
        assert url == S.TOKEN_URL, url
        body = json.loads(req.data)
        assert body["workspace_id"] == env["ANTHROPIC_WORKSPACE_ID"]
        assert body["federation_rule_id"] == env["ANTHROPIC_FEDERATION_RULE_ID"]
        return token_reply(claude_token, jwt)

    class FixedDate(date):
        @classmethod
        def today(cls):
            return date(2026, 9, 26)

    env = REV_ENV if role == "reviewer" else ENG_ENV
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("ACTIONS_ID_TOKEN_REQUEST_URL", "https://actions.example/token?x=1")
    monkeypatch.setenv("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "ghs_" + "A" * 36)
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setattr(S.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(S, "date", FixedDate)
    rc = S.main(["preflight", "--role", role])
    out = capsys.readouterr()
    return rc, out.out + out.err + summary.read_text(), jwt, jti, claude_token, seen


@pytest.mark.parametrize("role", ["engineer", "reviewer"])
def test_14_success_output_has_no_credentials(monkeypatch, tmp_path, capsys, role):
    reply = lambda tok, jwt: FakeResp({"access_token": tok, "token_type": "Bearer", "scope": "workspace:developer",  # noqa: E731
                                       "expires_in": 600}, {"request-id": "req_" + "2" * 20})
    rc, out, jwt, jti, tok, seen = _run_end_to_end(monkeypatch, tmp_path, capsys, role, reply)
    assert rc == 0 and "PASS_WITH_EXCEPTION" in out and "req_" + "2" * 20 in out
    assert len(seen) == 2 and seen[1] == S.TOKEN_URL              # один OIDC, один обмен, больше ничего
    for secret in (jwt, jwt.split(".")[1], jwt.split(".")[2], jti, tok, "ghs_" + "A" * 36):
        assert secret not in out
    assert find_secrets(out) == []
    ev = json.loads(out[:out.index("\n}") + 2])
    assert set(ev) <= {"mode", "role", "decision", "requested_federation_rule_id", "requested_service_account_id",
                       "requested_workspace_id", "scope", "expires_in", "request_id", "desired_scope", "claims",
                       "reason", "exception_expires", "identity_proof"}
    assert ev["requested_workspace_id"] == (REV_ENV if role == "reviewer" else ENG_ENV)["ANTHROPIC_WORKSPACE_ID"]
    assert "jti" not in ev["claims"] and ev["expires_in"] == 600 and ev["scope"] == "workspace:developer"


def test_14_rejection_output_has_no_credentials(monkeypatch, tmp_path, capsys):
    def reply(tok, jwt):
        hdrs = email.message.Message()
        hdrs["request-id"] = "req_" + "3" * 20
        body = json.dumps({"error": {"type": "invalid_grant " + tok, "message": "assertion " + jwt}}).encode()
        raise urllib.error.HTTPError(S.TOKEN_URL, 401, "Unauthorized", hdrs, io.BytesIO(body))
    rc, out, jwt, jti, tok, _ = _run_end_to_end(monkeypatch, tmp_path, capsys, "engineer", reply)
    assert rc == 1 and '"decision": "FAIL"' in out and "req_" + "3" * 20 in out
    for secret in (jwt, jwt.split(".")[2], jti, tok):
        assert secret not in out
    assert find_secrets(out) == []


def test_14_unexpected_failure_is_fail_and_redacted(monkeypatch, tmp_path, capsys):
    def reply(tok, jwt):
        raise OSError("boom " + tok + " " + jwt)
    rc, out, jwt, jti, tok, _ = _run_end_to_end(monkeypatch, tmp_path, capsys, "engineer", reply)
    assert rc == 1 and '"decision": "FAIL"' in out
    assert tok not in out and jwt not in out and find_secrets(out) == []


def test_14_output_path_redacts_even_if_evidence_carries_a_secret(monkeypatch, tmp_path, capsys):
    """Защита в глубину: что бы ни попало в доказательства, печать и итог job'а идут через редакцию #194."""
    leak = "ghp_" + "L" * 36
    monkeypatch.setattr(S, "preflight", lambda *a, **k: {"mode": "preflight", "role": "engineer",
                                                           "decision": "FAIL", "reason": "x " + leak,
                                                           "claims": {"sub": leak}})
    summary = tmp_path / "s.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    assert S.run_preflight("engineer") == 1
    out = capsys.readouterr().out + summary.read_text()
    assert leak not in out and "[REDACTED:" in out and find_secrets(out) == []


@pytest.mark.parametrize("name", sorted(REUSABLE))
def test_run_job_still_rejects_empty_run_id_and_state_sha(name):
    """run_id/state_sha больше не required (preflight их не передаёт) — job прохода обязан отвергать пустые."""
    body = jobs(text(WF / name))[{"autonomy-engineer.yml": "engineer", "autonomy-review.yml": "review"}[name]]
    guard = body.split("steps:", 1)[1].split("- uses:", 1)[0]
    assert '[[ "$RUN_ID" =~ ^run-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$ ]]' in guard
    assert '[[ "$STATE_SHA" =~ ^[0-9a-f]{40}$ ]]' in guard


def test_14_redaction_refusal_is_fail_without_traceback(monkeypatch, capsys):
    def refuse(*a, **k):
        raise S.RedactionError("остаток")
    monkeypatch.setattr(S, "preflight", lambda *a, **k: {"mode": "preflight", "role": "engineer", "decision": "PASS"})
    monkeypatch.setattr(S, "safe_dumps", refuse)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    assert S.run_preflight("engineer") == 1 and '"decision": "FAIL"' in capsys.readouterr().out
