"""R1 shadow pilot report (Phase 3.1E). Read-only.

  python scripts/r1_pilot_report.py export --activation <id> --out <file.csv>
  python scripts/r1_pilot_report.py summarize <evaluated.csv>

`export` joins the pilot records (v31_shadow_pilot), the existing shadow ledger
(v3_shadow_runs, 3.1E draft) and the communication (customer text, V2 draft) into
one row per case, with empty owner columns OWNER_PREFERENCE (V2 / V31E / NEITHER),
EDIT_CLASS (NONE / MINOR / MATERIAL), ISSUES (comma-separated: FALSE_BLOCK,
UNSUPPORTED_READY, DIRECT_QUESTION_UNANSWERED, SAFETY_ROUTE, SERVICE_ROUTE, ROBOTIC,
OTHER) and NOTE. The CSV contains customer text: keep it outside Git and delete it
after evaluation. `summarize` reads the filled CSV and prints the pilot metrics.
Nothing is written to Firestore, WB or Telegram.
"""
from __future__ import annotations

import argparse
import collections
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PREFERENCES = {"V2", "V31E", "NEITHER"}
EDIT_CLASSES = {"NONE", "MINOR", "MATERIAL"}
ISSUES = {"FALSE_BLOCK", "UNSUPPORTED_READY", "DIRECT_QUESTION_UNANSWERED", "SAFETY_ROUTE",
          "SERVICE_ROUTE", "ROBOTIC", "OTHER"}
COLUMNS = ["communication_id", "entity_type", "nm_id", "product_id", "customer_text", "v2_draft",
           "v31e_draft", "route", "hard_verdict", "quality_verdict", "operator_state",
           "selected_expertise_ids", "latency_ms", "logical_llm_calls", "status", "error_class",
           "OWNER_PREFERENCE", "EDIT_CLASS", "ISSUES", "NOTE"]


def export(activation_id: str, out: Path) -> int:
    from google.cloud import firestore
    from app.config import Settings
    from app.v3.pilot import PILOT_COLLECTION
    from app.v3.shadow import LEDGER_COLLECTION

    settings = Settings()
    client = firestore.Client(project=settings.gcp_project_id or None, database=settings.firestore_database)
    pilot = [s.to_dict() for s in client.collection(PILOT_COLLECTION)
             .where("activation_id", "==", activation_id).stream()
             if s.id.startswith("C.")]
    rows = []
    for rec in sorted(pilot, key=lambda r: r.get("claimed_at") or ""):
        comm = client.collection(settings.firestore_collection).document(rec["communication_id"]).get()
        comm = comm.to_dict() if comm.exists else {}
        ledger = client.collection(LEDGER_COLLECTION).document(rec.get("ledger_key") or "-").get()
        quality = ((ledger.to_dict() or {}) if ledger.exists else {}).get("response_quality_v31") or {}
        rows.append({
            "communication_id": rec["communication_id"], "entity_type": rec.get("entity_type"),
            "nm_id": comm.get("nm_id"), "product_id": rec.get("product_id"),
            "customer_text": " | ".join(x for x in (comm.get("text"), comm.get("pros"), comm.get("cons")) if x),
            "v2_draft": comm.get("ai_answer"), "v31e_draft": quality.get("candidate_text"),
            "route": rec.get("route"), "hard_verdict": rec.get("hard_verdict"),
            "quality_verdict": rec.get("quality_verdict"), "operator_state": rec.get("operator_state"),
            "selected_expertise_ids": json.dumps(rec.get("selected_expertise_ids") or []),
            "latency_ms": rec.get("latency_ms"), "logical_llm_calls": rec.get("logical_llm_calls"),
            "status": rec.get("status"), "error_class": rec.get("error_class"),
            "OWNER_PREFERENCE": "", "EDIT_CLASS": "", "ISSUES": "", "NOTE": ""})
    with out.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    print(f"{len(rows)} cases -> {out} (contains customer text; keep outside Git)")
    return 0


def summarize_rows(rows: list[dict]) -> dict:
    """Pilot metrics from owner-evaluated rows. Unevaluated rows are counted, not guessed."""
    evaluated = [r for r in rows if (r.get("OWNER_PREFERENCE") or "").strip()]
    bad = [r["communication_id"] for r in evaluated
           if r["OWNER_PREFERENCE"].strip() not in PREFERENCES
           or (r.get("EDIT_CLASS") or "").strip() not in EDIT_CLASSES
           or not {i.strip() for i in (r.get("ISSUES") or "").split(",") if i.strip()} <= ISSUES]
    if bad:
        raise ValueError(f"invalid owner labels: {bad}")
    issues = collections.Counter(i.strip() for r in evaluated for i in (r.get("ISSUES") or "").split(",") if i.strip())
    n = len(evaluated)
    rate = lambda k: round(k / n, 3) if n else None
    ready = [r for r in evaluated if r.get("operator_state") in {"READY", "WARNING"}]
    return {
        "cases": len(rows), "evaluated": n, "not_evaluated": len(rows) - n,
        "v31e_preferred_rate": rate(sum(r["OWNER_PREFERENCE"].strip() == "V31E" for r in evaluated)),
        "publishable_as_is_rate": rate(sum(r["EDIT_CLASS"].strip() == "NONE" for r in evaluated)),
        "minor_edit_rate": rate(sum(r["EDIT_CLASS"].strip() == "MINOR" for r in evaluated)),
        "material_edit_rate": rate(sum(r["EDIT_CLASS"].strip() == "MATERIAL" for r in evaluated)),
        "false_block_rate": rate(issues["FALSE_BLOCK"]),
        "unsupported_ready": issues["UNSUPPORTED_READY"],
        "serious_safety_misses": issues["SAFETY_ROUTE"],
        "direct_question_misses": issues["DIRECT_QUESTION_UNANSWERED"],
        "service_route_errors": issues["SERVICE_ROUTE"],
        "robotic_findings": issues["ROBOTIC"],
        "ready_or_warning_cases": len(ready),
        "errors": sum((r.get("status") or "") == "ERROR" for r in rows),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("export")
    e.add_argument("--activation", required=True)
    e.add_argument("--out", type=Path, required=True)
    s = sub.add_parser("summarize")
    s.add_argument("csv", type=Path)
    args = ap.parse_args()
    if args.cmd == "export":
        return export(args.activation, args.out)
    with args.csv.open(encoding="utf-8") as f:
        print(json.dumps(summarize_rows(list(csv.DictReader(f))), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
