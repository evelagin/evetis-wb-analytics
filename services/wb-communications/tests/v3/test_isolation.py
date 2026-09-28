"""Phase 3 invariants: v3 cannot reach customers; v2 keeps working when v3 fails."""
from __future__ import annotations

import ast
from pathlib import Path

from app.v3 import V3_CUSTOMER_FACING
from app.v3.registry import load_policy

V3_DIR = Path(__file__).resolve().parents[2] / "app" / "v3"
FORBIDDEN_IMPORTS = ("app.services.wb_client", "app.services.telegram_client", "app.services.pipeline",
                     "app.services.repository")


def test_v3_package_cannot_import_publication_clients():
    for f in V3_DIR.glob("*.py"):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                mod = getattr(node, "module", None) or ""
                names = [a.name for a in node.names]
                for m in [mod, *names]:
                    assert not any(m.startswith(x) for x in FORBIDDEN_IMPORTS), f"{f.name} imports {m}"


def test_hard_invariants():
    p = load_policy()
    assert V3_CUSTOMER_FACING is False
    assert p["runtime"]["auto_publish"] is False and p["runtime"]["v3_customer_facing"] is False
    assert p["claim_policy"]["cosmetic_claim_generation"] is False
    assert p["approved_general_guidance"] == [] and p["runtime"]["few_shot_from_history"] is False


def test_poll_unaffected_when_v3_disabled_or_broken():
    from app.services.pipeline import run_poll
    from tests.conftest import SAMPLE_FEEDBACK, make_deps
    deps = make_deps([dict(SAMPLE_FEEDBACK)])
    deps.settings.v3_shadow_enabled = True

    class Broken:
        settings = deps.settings

        @property
        def engine(self):
            raise RuntimeError("v3 down")
        store = writer = None
    deps.v3 = Broken()
    summary = run_poll(deps)
    assert summary["processed"] == 1 and summary["v3_shadow"]["error"] == "RuntimeError"
    assert len(deps.telegram.sent) == 1          # v2 card sent exactly as before


def test_poll_without_flag_has_no_v3_key():
    from app.services.pipeline import run_poll
    from tests.conftest import SAMPLE_FEEDBACK, make_deps
    summary = run_poll(make_deps([dict(SAMPLE_FEEDBACK)]))
    assert "v3_shadow" not in summary


def test_logging_redaction_still_active():
    from app.utils.logging import redact
    assert "<telegram_token>" in redact("https://api.telegram.org/bot1234567890:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA/send")
