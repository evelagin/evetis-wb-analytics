"""CLI Autonomous Engineering v1. Точка входа для workflows и для оператора.

  python -m tools.autonomy.cli validate --kind objective FILE
  python -m tools.autonomy.cli watch --state-dir D --out D (--fixture F | --live ...)
  python -m tools.autonomy.cli submit --state-dir D --objective F
  python -m tools.autonomy.cli advance --state-dir D --run-id R [--stop-before S ...]
  python -m tools.autonomy.cli status --state-dir D [--run-id R]
  python -m tools.autonomy.cli report --state-dir D --run-id R
  python -m tools.autonomy.cli publish --state-dir D --run-id R [--dry-run]
  python -m tools.autonomy.cli verify-ci --state-dir D --run-id R --repo OWNER/NAME
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))

from tools.autonomy.schema import load_schema, require_valid  # noqa: E402
from tools.autonomy.state import StateStore  # noqa: E402


def _sha() -> str:
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True,
                          check=True).stdout.strip()


CI_KEEP_ENV = ("GOOGLE_APPLICATION_CREDENTIALS", "CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE", "CLOUDSDK_CONFIG",
               "CLOUDSDK_PROJECT", "ANTHROPIC_FEDERATION_RULE_ID", "ANTHROPIC_ORGANIZATION_ID",
               "ANTHROPIC_SERVICE_ACCOUNT_ID", "ANTHROPIC_WORKSPACE_ID", "ANTHROPIC_IDENTITY_TOKEN_FILE")


def _adapter(kind: str, a, store: StateStore):
    from tools.autonomy.agents import ClaudeCliAdapter, NoAgentAdapter, ReplayAdapter
    if kind == "claude":
        if getattr(a, "ci", False):
            # CI: агенту передаются только read-only SA и идентификаторы федерации Anthropic.
            return ClaudeCliAdapter(keep_env=CI_KEEP_ENV,
                                    pre_invoke=["bash", str(REPO / "tools/autonomy/ci/refresh_anthropic_oidc.sh")])
        return ClaudeCliAdapter()
    if kind == "replay":
        return ReplayAdapter(Path(a.pending_dir), _expect(a))
    return NoAgentAdapter()


def _expect(a) -> dict[str, str] | None:
    """--expect NAME=SHA256: хеши, объявленные job'ом-производителем через outputs."""
    if not getattr(a, "expect", None):
        return None
    out = {}
    for item in a.expect:
        name, _, sha = item.partition("=")
        if not sha:
            raise SystemExit(f"--expect ожидает NAME=SHA256, получено {item!r}")
        out[name] = sha
    return out


def _evidence(a):
    from tools.autonomy.evidence import RepoEvidenceRunner, ReplayEvidenceRunner
    runner = RepoEvidenceRunner(a.project, a.token_command)
    if getattr(a, "evidence", "live") == "replay":
        exp = _expect(a) or {}
        return ReplayEvidenceRunner(Path(a.pending_dir) / "evidence.json", exp.get("evidence.json", ""), runner,
                                    trusted_repo=REPO)
    return runner


def _orchestrator(a, store: StateStore):
    from tools.autonomy.evidence import RepoEvidenceRunner
    from tools.autonomy.orchestrator import Orchestrator
    from tools.autonomy.publisher import GitPublisher
    audit = lambda run: {"status": "NOT_APPLICABLE", "mutations": 0}  # noqa: E731
    if a.project and a.token_command:
        from tools.autonomy.audit import count_mutations
        ids = [i for i in (a.audit_identity or []) if i]

        def audit(run):  # noqa: F811 — BLOCKED остаётся BLOCKED, а не нулём
            return count_mutations(a.project, a.token_command, run["created_at"], ids)
    publisher = GitPublisher(REPO, dry_run=a.dry_run) if getattr(a, "publish", False) else None
    verifier = None
    if getattr(a, "verify_repo", None):
        from tools.autonomy.policy import load_policy
        from tools.autonomy.verification import GitHubVerifier
        verifier = GitHubVerifier(a.verify_repo, load_policy()["required_verification"]["workflows"])
    return Orchestrator(store, REPO, _adapter(getattr(a, "engineer", "claude"), a, store),
                        _adapter(getattr(a, "reviewer", "claude"), a, store),
                        _evidence(a), Path(a.sandbox_root),
                        audit=audit, publisher=publisher, verifier=verifier,
                        trusted_base_ref=getattr(a, "trusted_base_ref", None))


# Код выхода недоверенного job'а агента: 0 — вывод есть; 3 — агент отказал, но диагностика записана и
# передаётся доверенному ingest (ДОКУМЕНТИРОВАННАЯ передача: workflow пишет ::error:: и не прячет отказ);
# любой иной код — сбой обёртки, job падает.
HANDOFF_EXIT = 3


def _handoff_code(diag: Path) -> int:
    try:
        return 0 if json.loads(diag.read_text(encoding="utf-8"))["outcome"] == "SUCCESS" else HANDOFF_EXIT
    except (OSError, ValueError, KeyError):
        return HANDOFF_EXIT


def _scope_guard(a) -> dict | None:
    """Последняя JSON-строка вывода сторожа scope этого job'а. Ошибка чтения — None (не повод падать)."""
    path = getattr(a, "scope_guard_file", None)
    if not path:
        return None
    try:
        lines = [ln for ln in Path(path).read_text(encoding="utf-8").splitlines() if ln.strip().startswith("{")]
        return json.loads(lines[-1]) if lines else None
    except (OSError, ValueError):
        return None


def _diag_summary(path: Path) -> int:
    from tools.autonomy import diagnostics as D
    from tools.autonomy.agents import sha256_file
    from tools.autonomy.schema import validate as _validate
    if not path.exists():
        print("Диагностика агента: файл отсутствует")
        return 0
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        errs = _validate(doc, load_schema("agent_diagnostics"))
    except ValueError:
        doc, errs = None, ["не JSON"]
    if errs:
        print("Диагностика агента: файл не соответствует схеме")
        return 0
    print(D.summary_line(doc, sha256_file(path)))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("validate"); v.add_argument("--kind", required=True,
                                                   choices=["incident", "objective", "review_verdict",
                                                            "engineer_report", "run_state", "agent_diagnostics"])
    v.add_argument("file")
    w = sub.add_parser("watch"); w.add_argument("--state-dir", required=True); w.add_argument("--out", required=True)
    w.add_argument("--fixture"); w.add_argument("--live", action="store_true"); w.add_argument("--project")
    w.add_argument("--token-command"); w.add_argument("--suites", nargs="*")
    w.add_argument("--no-health", action="store_true"); w.add_argument("--report")
    s = sub.add_parser("submit"); s.add_argument("--state-dir", required=True); s.add_argument("--objective", required=True)
    s.add_argument("--trusted-base-ref", help="repository_sha цели обязан быть предком этого ref (CI: origin/main)")
    for name in ("advance", "publish", "review", "agent-run", "collect", "verify-ci", "audit"):
        p = sub.add_parser(name)
        p.add_argument("--engineer", choices=["claude", "replay", "none"], default="claude")
        p.add_argument("--reviewer", choices=["claude", "replay", "none"], default="claude")
        p.add_argument("--evidence", choices=["live", "replay"], default="live")
        p.add_argument("--pending-dir", help="каталог недоверенного вывода (агент / доказательства)")
        p.add_argument("--expect", nargs="*", help="NAME=SHA256 из outputs job'а-производителя")
        p.add_argument("--out", help="куда писать недоверенный вывод (agent-run, collect)")
        p.add_argument("--ci", action="store_true", help="режим GitHub Actions: read-only SA и WIF Anthropic")
        p.add_argument("--state-dir", required=True); p.add_argument("--run-id", required=True)
        p.add_argument("--project"); p.add_argument("--token-command"); p.add_argument("--audit-identity", nargs="*")
        p.add_argument("--sandbox-root", default=str(Path.home() / ".cache" / "evetis-ae" / "sandboxes"))
        p.add_argument("--stop-before", nargs="*", default=[]); p.add_argument("--dry-run", action="store_true")
        p.add_argument("--trusted-base-ref", help="repository_sha прогона обязан быть предком этого ref")
        p.add_argument("--scope-guard-file", help="вывод anthropic_scope check этого job'а (в диагностику)")
        if name == "verify-ci":
            p.add_argument("--repo", dest="verify_repo", required=True, help="OWNER/NAME для API Actions")
            p.add_argument("--timeout-minutes", type=int); p.add_argument("--poll-seconds", type=int)
    st = sub.add_parser("status"); st.add_argument("--state-dir", required=True); st.add_argument("--run-id")
    r = sub.add_parser("report"); r.add_argument("--state-dir", required=True); r.add_argument("--run-id", required=True)
    n = sub.add_parser("next-pass"); n.add_argument("--state-dir", required=True); n.add_argument("--run-id", required=True)
    ds = sub.add_parser("diag-summary", help="строка итога по файлу диагностики агента (job summary)")
    ds.add_argument("--file", required=True)
    a = ap.parse_args(argv)

    if a.cmd == "validate":
        require_valid(json.loads(Path(a.file).read_text(encoding="utf-8")), a.kind)
        print(f"OK: {a.file} валиден по {a.kind}.schema.json")
        return 0
    if a.cmd == "diag-summary":
        return _diag_summary(Path(a.file))
    store = StateStore(Path(a.state_dir))
    if a.cmd == "watch":
        from tools.autonomy.watcher import fixture_source, live_source, watch
        if a.fixture:
            obs = fixture_source(Path(a.fixture))
        elif a.live:
            from tools.autonomy.envelope import suites_index
            suites = a.suites or [n for n, s in suites_index().items() if s.get("gate")]
            obs = live_source(a.project, a.token_command, suites, include_health=not a.no_health)
        else:
            ap.error("нужен --fixture или --live")
        rep = watch(obs, store, _sha(), Path(a.out), synthetic=bool(a.fixture and "synthetic" in a.fixture))
        from tools.autonomy.redact import safe_dumps
        text = safe_dumps(rep, indent=2)
        if a.report:
            Path(a.report).write_text(text, encoding="utf-8")
        print(text)
        return 0
    if a.cmd == "submit":
        from tools.autonomy.orchestrator import Orchestrator  # noqa: F401 — валидация и дедупликация
        obj = json.loads(Path(a.objective).read_text(encoding="utf-8"))
        a.project = a.token_command = None; a.audit_identity = []; a.dry_run = False
        a.sandbox_root = str(Path.home() / ".cache" / "evetis-ae" / "sandboxes")
        run, created = _orchestrator(a, store).submit(obj)
        print(json.dumps({"run_id": run["run_id"], "created": created, "state": run["state"],
                          "branch": run["branch"]}, ensure_ascii=False))
        return 0
    if a.cmd == "agent-run":
        # Недоверенный job инженера: авторитетное состояние не меняется.
        from tools.autonomy.orchestrator import agent_run
        hashes = agent_run(lambda scratch: _orchestrator(a, scratch), store, a.run_id, Path(a.out),
                           scope_guard=_scope_guard(a))
        print(json.dumps({"run_id": a.run_id, "outputs": hashes}, ensure_ascii=False))
        return _handoff_code(Path(a.out) / "engineer_diagnostics.json")
    if a.cmd == "collect":
        # Недоверенный job тестов: исполняет код кандидата, решений не принимает.
        from tools.autonomy.orchestrator import collect_candidate_evidence
        out = Path(a.out); out.parent.mkdir(parents=True, exist_ok=True)
        sha = collect_candidate_evidence(_orchestrator(a, store), a.run_id, out)
        print(json.dumps({"run_id": a.run_id, "evidence.json": sha}, ensure_ascii=False))
        return 0
    if a.cmd == "review":
        # Только ревьюер, без перехода состояния: вердикт сохраняется для job'а гейткипера.
        from tools.autonomy.orchestrator import review_only
        a.project = a.project or None
        out = Path(a.out) if a.out else None
        hashes = review_only(_orchestrator(a, store), a.run_id, out, scope_guard=_scope_guard(a))
        print(json.dumps({"run_id": a.run_id, "outputs": hashes}, ensure_ascii=False))
        diag = (out or store.root / "artifacts" / a.run_id) / "reviewer_diagnostics.json"
        return _handoff_code(diag)
    if a.cmd == "audit":
        # Доверенный job: аудит production-мутаций всего окна прогона перед публикацией (sa-ae-reader).
        if not (a.project and a.token_command and [i for i in (a.audit_identity or []) if i]):
            print("audit: нужны --project, --token-command и --audit-identity — без них доказательства нет",
                  file=sys.stderr)
            return 2
        a.engineer = a.reviewer = "none"
        run = _orchestrator(a, store).trusted_audit(a.run_id)
        print(json.dumps({"run_id": run["run_id"], "audit_status": run["audit_status"],
                          "audit_evidence": run.get("audit_evidence")}, ensure_ascii=False))
        return 0 if run["audit_status"] == "PASS" else 1
    if a.cmd == "verify-ci":
        # Доверенный job ci-verify (actions: read): ждать обязательные workflows опубликованного SHA.
        import time
        from tools.autonomy.policy import load_policy
        rv = load_policy()["required_verification"]
        a.engineer = a.reviewer = "none"
        orch = _orchestrator(a, store)
        deadline = time.monotonic() + 60 * (a.timeout_minutes or rv["timeout_minutes"] + 5)
        while True:
            run = orch.advance(a.run_id)
            if run["state"] != "AWAITING_VERIFICATION" or time.monotonic() > deadline:
                break
            time.sleep(a.poll_seconds or rv["poll_seconds"])
        print(json.dumps({"run_id": run["run_id"], "state": run["state"],
                          "verification": (run.get("verification") or {}).get("result")}, ensure_ascii=False))
        return 0
    if a.cmd in ("advance", "publish"):
        a.publish = a.cmd == "publish"
        orch = _orchestrator(a, store)
        run = orch.advance(a.run_id, stop_before=set(a.stop_before))
        print(json.dumps({"run_id": run["run_id"], "state": run["state"], "last_gate": run["last_gate"],
                          "pr_url": run.get("pr_url")}, ensure_ascii=False))
        return 0
    if a.cmd == "status":
        runs = [store.load(a.run_id)] if a.run_id else [
            json.loads(p.read_text(encoding="utf-8")) for p in sorted((store.root / "runs").glob("*.json"))]
        for run in runs:
            print(f"{run['run_id']}  {run['state']:<18} {run['deduplication_key']}  iter={run['iteration']} "
                  f"review={run['review_cycles']}  mutations={run['production_mutations']}")
        return 0
    if a.cmd == "next-pass":
        from tools.autonomy.orchestrator import next_pass
        print(next_pass(store, a.run_id))
        return 0
    if a.cmd == "report":
        from tools.autonomy.redact import safe_text
        from tools.autonomy.report import render_report
        run = store.load(a.run_id)
        print(safe_text(render_report(run, store.root / "artifacts" / run["run_id"])))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
