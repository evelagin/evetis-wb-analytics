"""v3 decision pipeline for ONE communication (shadow only).

message -> product resolution -> classification + safety -> required facts -> knowledge
resolution -> strategy -> generation / abstention -> deterministic verification -> decision.

Failure is explicit: CLASSIFIER_ERROR, PRODUCT_NOT_VERIFIED, KNOWLEDGE_SNAPSHOT_ERROR,
REQUIRED_FACT_UNKNOWN, KNOWLEDGE_CONFLICT, GENERATION_ERROR, VERIFIER_BLOCK, SAFETY_ESCALATION.
There is no silent fallback to v1/v2 text.
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from app.v3 import ENGINE_VERSION, classifier as classifier_mod, generator as generator_mod
from app.v3 import planner as planner_mod, resolver as resolver_mod, verifier as verifier_mod
from app.v3.snapshot import KnowledgeSnapshot


@dataclass
class Decision:
    communication_id: str
    engine_version: str
    snapshot_id: str
    policy_version: str
    resolution: Any
    classification: Any
    plan: Any
    generation: Any
    verification: Any
    final_outcome: str
    failure_code: Optional[str]
    draft: Optional[str]
    latency_ms: dict = field(default_factory=dict)
    v2_checks: dict = field(default_factory=dict)
    error_class: Optional[str] = None


def _sha(text: str | None) -> Optional[str]:
    return hashlib.sha256(text.encode("utf-8")).hexdigest() if text else None


class V3Engine:
    def __init__(self, snapshot: KnowledgeSnapshot, llm_factory=None, *, use_llm_classifier: bool = True):
        self.snapshot = snapshot
        self._llm_factory = llm_factory          # () -> V3LLM (fresh stats per run) or None
        self._use_llm_classifier = use_llm_classifier

    def decide(self, msg: dict) -> tuple[Decision, Any]:
        snap = self.snapshot
        lat: dict[str, int] = {}
        t0 = time.monotonic()
        llm = self._llm_factory() if self._llm_factory else None

        t = time.monotonic()
        res = resolver_mod.resolve(snap, nm_id=msg.get("nm_id"), supplier_article=msg.get("supplier_article"),
                                   barcode=msg.get("barcode"), marketplace=msg.get("marketplace") or "WB")
        lat["resolver"] = int((time.monotonic() - t) * 1000)

        t = time.monotonic()
        cls = classifier_mod.classify(msg, snap, llm if (llm and self._use_llm_classifier) else None)
        lat["classifier"] = int((time.monotonic() - t) * 1000)

        t = time.monotonic()
        pl = planner_mod.plan(snap, res, cls, msg)
        lat["planner"] = int((time.monotonic() - t) * 1000)

        t = time.monotonic()
        gen = generator_mod.generate(pl, snap, msg, llm)
        lat["generator"] = int((time.monotonic() - t) * 1000)

        t = time.monotonic()
        ver = None
        if gen.text:
            ver = verifier_mod.verify(gen.text, verifier_mod.context_for_plan(pl, snap), snap)
        lat["verifier"] = int((time.monotonic() - t) * 1000)

        if pl.strategy == "HUMAN_REVIEW":
            outcome, failure = "HUMAN_REVIEW", pl.failure_code
        elif gen.error or not gen.text:
            outcome, failure = "HUMAN_REVIEW", "GENERATION_ERROR"
        elif ver and ver.blocked:
            outcome, failure = "BLOCK", "VERIFIER_BLOCK"
        else:
            outcome, failure = pl.strategy, None
        if cls.llm_error and failure is None:
            failure = None  # classifier LLM error is recorded separately; rules result stands

        # same verifier on the v2 production text (AI draft and, if different, the final/manual text)
        t = time.monotonic()
        v2 = {}
        ctx_free = verifier_mod.context_for_free_text(snap, [res.product_id] if res.product_id else [])
        for key in ("v2_ai_answer", "v2_final_answer"):
            text = msg.get(key)
            if text and (key == "v2_ai_answer" or text != msg.get("v2_ai_answer")):
                r = verifier_mod.verify(text, ctx_free, snap)
                v2[key] = {"sha256": _sha(text), "verdict": r.verdict, "block_rules": r.rule_ids("BLOCK"),
                           "violations": [v.__dict__ for v in r.violations]}
        lat["v2_verifier"] = int((time.monotonic() - t) * 1000)
        lat["total"] = int((time.monotonic() - t0) * 1000)

        d = Decision(
            communication_id=msg.get("communication_id") or "", engine_version=ENGINE_VERSION,
            snapshot_id=snap.snapshot_id, policy_version=snap.policy.get("policy_version"),
            resolution=res, classification=cls, plan=pl, generation=gen, verification=ver,
            final_outcome=outcome, failure_code=failure,
            draft=gen.text, latency_ms=lat, v2_checks=v2,
            error_class=cls.llm_error,
        )
        return d, (llm.stats if llm else None)
