"""WP8 — decision journal row (BigQuery ``evetis_communications.communication_v3_decisions``).

One row per v3 shadow decision. Reproducible: snapshot id + policy version + resolution +
classification (with evidence spans) + required/resolved facts (fact ids, source ids) +
strategy + templates + draft + verifier result + latency + model/prompt versions + cost.
No secrets. The customer message itself is NOT copied (it already lives in Firestore /
communications_current): only its sha256; evidence spans are kept because they are needed
to reconstruct the safety decision.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Optional

from app.v3.engine import Decision

SCHEMA = [
    ("decision_id", "STRING"), ("created_at", "TIMESTAMP"), ("run_kind", "STRING"),
    ("communication_id", "STRING"), ("entity_type", "STRING"), ("marketplace", "STRING"),
    ("source_id", "STRING"), ("nm_id", "STRING"), ("rating", "INT64"), ("message_sha256", "STRING"),
    ("engine_version", "STRING"), ("knowledge_snapshot_id", "STRING"), ("policy_version", "STRING"),
    ("classifier_prompt_version", "STRING"), ("generator_prompt_version", "STRING"), ("model", "STRING"),
    ("product_id", "STRING"), ("product_resolution_status", "STRING"), ("product_resolution_json", "STRING"),
    ("classification_json", "STRING"), ("safety_json", "STRING"), ("risk_level", "STRING"),
    ("escalation_domains", "STRING"), ("required_facts_json", "STRING"), ("resolved_facts_json", "STRING"),
    ("strategy", "STRING"), ("final_outcome", "STRING"), ("failure_code", "STRING"),
    ("allowed_fact_ids", "STRING"), ("template_ids", "STRING"), ("guidance_ids", "STRING"),
    ("operator_warnings", "STRING"), ("plan_reasons", "STRING"),
    ("draft_text", "STRING"), ("draft_sha256", "STRING"), ("generation_mode", "STRING"),
    ("verifier_verdict", "STRING"), ("verifier_block_rules", "STRING"), ("verifier_json", "STRING"),
    ("v2_ai_answer_sha256", "STRING"), ("v2_ai_verdict", "STRING"), ("v2_ai_block_rules", "STRING"),
    ("v2_final_answer_sha256", "STRING"), ("v2_final_verdict", "STRING"), ("v2_final_block_rules", "STRING"),
    ("v2_answer_source", "STRING"), ("v2_checks_json", "STRING"),
    ("latency_ms_total", "INT64"), ("latency_json", "STRING"),
    ("llm_calls", "INT64"), ("tokens_in", "INT64"), ("tokens_out", "INT64"), ("cost_estimate_usd", "FLOAT64"),
    ("classifier_llm_used", "BOOL"), ("classifier_llm_error", "STRING"), ("error_class", "STRING"),
]


def _j(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False, sort_keys=True, default=lambda o: getattr(o, "__dict__", str(o)))


def _sha(text: Optional[str]) -> Optional[str]:
    return hashlib.sha256(text.encode("utf-8")).hexdigest() if text else None


def decision_id(communication_id: str, engine_version: str, snapshot_id: str, run_kind: str) -> str:
    return hashlib.sha1(f"{communication_id}|{engine_version}|{snapshot_id}|{run_kind}".encode()).hexdigest()


def build_row(d: Decision, msg: dict, stats, *, run_kind: str = "shadow") -> dict:
    cls, pl, gen, ver, res = d.classification, d.plan, d.generation, d.verification, d.resolution
    v2a, v2f = d.v2_checks.get("v2_ai_answer") or {}, d.v2_checks.get("v2_final_answer") or {}
    message_text = "\n".join(x for x in (msg.get("text"), msg.get("pros"), msg.get("cons")) if x)
    tokens_in = getattr(stats, "input_tokens", 0) if stats else 0
    tokens_out = getattr(stats, "output_tokens", 0) if stats else 0
    return {
        "decision_id": decision_id(d.communication_id, d.engine_version, d.snapshot_id, run_kind),
        "created_at": datetime.now(timezone.utc).isoformat(), "run_kind": run_kind,
        "communication_id": d.communication_id, "entity_type": msg.get("entity_type"),
        "marketplace": msg.get("marketplace") or "WB", "source_id": msg.get("source_id"),
        "nm_id": str(msg.get("nm_id") or ""), "rating": msg.get("rating"),
        "message_sha256": _sha(message_text),
        "engine_version": d.engine_version, "knowledge_snapshot_id": d.snapshot_id,
        "policy_version": d.policy_version,
        "classifier_prompt_version": msg.get("_classifier_prompt_version"),
        "generator_prompt_version": msg.get("_generator_prompt_version"),
        "model": gen.model if gen else None,
        "product_id": res.product_id, "product_resolution_status": res.status,
        "product_resolution_json": _j(res.to_dict()),
        "classification_json": _j({k: v for k, v in cls.to_dict().items() if k != "safety"}),
        "safety_json": _j(cls.safety.to_dict() if cls.safety else None),
        "risk_level": pl.risk_level, "escalation_domains": _j(pl.escalation_domains),
        "required_facts_json": _j(pl.required_facts),
        "resolved_facts_json": _j([r.__dict__ for r in pl.resolved]),
        "strategy": pl.strategy, "final_outcome": d.final_outcome, "failure_code": d.failure_code,
        "allowed_fact_ids": _j(sorted({i for r in pl.allowed for i in r.fact_ids})),
        "template_ids": _j(pl.template_ids), "guidance_ids": _j(pl.guidance_ids),
        "operator_warnings": _j(pl.operator_warnings), "plan_reasons": _j(pl.reasons),
        "draft_text": d.draft, "draft_sha256": _sha(d.draft), "generation_mode": gen.mode if gen else None,
        "verifier_verdict": ver.verdict if ver else None,
        "verifier_block_rules": _j(ver.rule_ids("BLOCK") if ver else []),
        "verifier_json": _j(ver.to_dict() if ver else None),
        "v2_ai_answer_sha256": v2a.get("sha256"), "v2_ai_verdict": v2a.get("verdict"),
        "v2_ai_block_rules": _j(v2a.get("block_rules") or []),
        "v2_final_answer_sha256": v2f.get("sha256"), "v2_final_verdict": v2f.get("verdict"),
        "v2_final_block_rules": _j(v2f.get("block_rules") or []),
        "v2_answer_source": msg.get("v2_answer_source"), "v2_checks_json": _j(d.v2_checks),
        "latency_ms_total": d.latency_ms.get("total"), "latency_json": _j(d.latency_ms),
        "llm_calls": getattr(stats, "calls", 0) if stats else 0,
        "tokens_in": tokens_in, "tokens_out": tokens_out,
        "cost_estimate_usd": round(tokens_in / 1000 * float(msg.get("_cost_in", 0) or 0)
                                   + tokens_out / 1000 * float(msg.get("_cost_out", 0) or 0), 6),
        "classifier_llm_used": bool(cls.llm_used), "classifier_llm_error": cls.llm_error,
        "error_class": d.error_class,
    }


def failure_row(msg: dict, *, snapshot_id: str, engine_version: str, error_class: str,
                failure_code: str, run_kind: str = "shadow") -> dict:
    """Row for a decision that could not be computed at all (explicit, never silent)."""
    cid = msg.get("communication_id") or ""
    row = {name: None for name, _ in SCHEMA}
    row.update({"decision_id": decision_id(cid, engine_version, snapshot_id, run_kind),
                "created_at": datetime.now(timezone.utc).isoformat(), "run_kind": run_kind,
                "communication_id": cid, "entity_type": msg.get("entity_type"),
                "marketplace": msg.get("marketplace") or "WB", "source_id": msg.get("source_id"),
                "nm_id": str(msg.get("nm_id") or ""), "rating": msg.get("rating"),
                "engine_version": engine_version, "knowledge_snapshot_id": snapshot_id,
                "final_outcome": "HUMAN_REVIEW", "failure_code": failure_code, "error_class": error_class,
                "classifier_llm_used": False})
    return row
