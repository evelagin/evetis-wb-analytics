"""Reviews & Q&A v3 shadow evaluation report (Phase 3 §41 / WP12). Read-only.

  python scripts/v3_shadow_report.py [--snapshot <id>]

Prints separate dimensions (no composite score): counts/rates from V_V3_SHADOW_METRICS,
v2-vs-v3 failure classes from V_V3_V2_FAILURE_CLASSES, outcome distribution, and the
historical-corpus cross-tab (GOOD / NEEDS_EDIT / BAD / SAFETY_RISK labels joined by
communication_id; labels only, historical answers are never a factual source).
"""
from __future__ import annotations

import collections
import json
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.v3_registry import _Rest, PROJECT  # noqa: E402

DS = f"{PROJECT}.evetis_communications"
LABELS = Path(__file__).resolve().parents[1] / "knowledge_v3" / "eval" / "historical_labels.yaml"


def main() -> int:
    snap = sys.argv[sys.argv.index("--snapshot") + 1] if "--snapshot" in sys.argv else None
    rest = _Rest()
    where = f"WHERE knowledge_snapshot_id = '{snap}'" if snap else ""
    metrics = rest.query(f"SELECT * FROM `{DS}.V_V3_SHADOW_METRICS` {where}")
    classes = rest.query(f"SELECT * FROM `{DS}.V_V3_V2_FAILURE_CLASSES` ORDER BY rule_id")
    rows = rest.query(f"SELECT communication_id, entity_type, product_resolution_status, risk_level, strategy, "
                      f"final_outcome, failure_code, verifier_verdict, verifier_block_rules, v2_ai_verdict, "
                      f"v2_ai_block_rules, latency_ms_total FROM `{DS}.V_V3_SHADOW_LATEST` {where}")
    labels = {c["communication_id"]: c for c in yaml.safe_load(LABELS.read_text(encoding="utf-8"))["cases"]}
    outcome = collections.Counter(r["final_outcome"] for r in rows)
    failure = collections.Counter(r["failure_code"] for r in rows if r["failure_code"])
    hist = collections.defaultdict(lambda: collections.Counter())
    for r in rows:
        lab = labels.get(r["communication_id"])
        if lab:
            hist[lab["label"]]["cases"] += 1
            hist[lab["label"]]["v2_ai_BLOCK"] += r["v2_ai_verdict"] == "BLOCK"
            hist[lab["label"]][f"v3_{r['final_outcome']}"] += 1
    print(json.dumps({"metrics": metrics, "outcomes": outcome, "failure_codes": failure,
                      "v2_vs_v3_failure_classes": [c for c in classes if any(
                          c[k] for k in ("v2_ai_drafts_with_violation", "v2_final_texts_with_violation",
                                         "v3_drafts_blocked_by_rule"))],
                      "historical_corpus": {k: dict(v) for k, v in hist.items()},
                      "historical_labels_covered": sum(v["cases"] for v in hist.values())},
                     ensure_ascii=False, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
