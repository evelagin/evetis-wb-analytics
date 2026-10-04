"""Strict, offline Seller capability policy. Inventory never grants runtime permission."""
from __future__ import annotations

import json
import re
from pathlib import Path

LIFECYCLES = frozenset({"ACTIVE", "RETIRED", "ALIAS", "UNRESOLVED"})
SEMANTICS = frozenset({"READ", "ARTIFACT_GENERATION", "VALIDATION_OR_CHECK",
                       "BUSINESS_STATE_MUTATION", "EXTERNAL_SIDE_EFFECT", "UNPROVEN"})
CONFIDENCES = frozenset({"REVIEWED", "SUGGESTION", "UNRESOLVED"})
LEGACY_CLASSES = frozenset({"READ", "REPORT", "MUTATION", "UNKNOWN"})


def _pairs(pairs):
    out = {}
    for k, v in pairs:
        if k in out:
            raise ValueError("duplicate policy key")
        out[k] = v
    return out


def parse(text):
    return validate(json.loads(text, object_pairs_hook=_pairs,
                               parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite policy"))))


def load(path: Path):
    return parse(path.read_text(encoding="utf-8"))


def validate(policy):
    if not isinstance(policy, dict) or type(policy.get("schema_version")) is not int:
        raise ValueError("invalid policy version")
    if policy["schema_version"] != 2 or policy.get("api") != "ozon_seller":
        raise ValueError("unsupported Seller policy")
    if not isinstance(policy.get("methods"), dict) or not policy["methods"]:
        raise ValueError("empty policy")
    if not isinstance(policy.get("spec_sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", policy["spec_sha256"]):
        raise ValueError("invalid spec provenance")
    # No capability exception was approved in this phase, including legacy v1 exceptions.
    if policy.get("approved_mutation_methods") != []:
        raise ValueError("mutation exceptions not authorized")
    for path, r in policy["methods"].items():
        if not isinstance(path, str) or not path.startswith("/") or any(c in path for c in "?#%\\") or any(ord(c) <= 32 or ord(c) >= 127 for c in path):
            raise ValueError("invalid inventory path")
        if not isinstance(r, dict) or r.get("class") not in LEGACY_CLASSES:
            raise ValueError("invalid legacy classification")
        if r.get("http") not in ("GET", "POST", "PUT", "PATCH", "DELETE") or type(r.get("deprecated")) is not bool:
            raise ValueError("invalid operation record")
        if not isinstance(r.get("reason"), str) or not r["reason"].strip():
            raise ValueError("invalid legacy reason")
        if r.get("lifecycle") not in LIFECYCLES or r.get("semantics") not in SEMANTICS:
            raise ValueError("unsupported capability state")
        if r.get("confidence") not in CONFIDENCES or r.get("side_effects") not in ("NONE", "ARTIFACT", "BUSINESS", "EXTERNAL", "UNPROVEN"):
            raise ValueError("invalid capability confidence")
        review = r.get("review")
        if not isinstance(review, dict) or not all(isinstance(review.get(k), str) and review[k] for k in ("source", "authority", "date", "reason")):
            raise ValueError("missing review provenance")
        if not isinstance(r.get("legacy_record"), dict) or any(r["legacy_record"].get(k) != r.get(k) for k in ("class", "http", "deprecated", "reason")):
            raise ValueError("missing legacy provenance")
        if r["lifecycle"] in ("ACTIVE", "RETIRED", "ALIAS") and r["confidence"] != "REVIEWED":
            raise ValueError("suggestion cannot authorize classification")
        if r["lifecycle"] == "RETIRED":
            retirement = r.get("retirement")
            if not isinstance(retirement, dict) or not all(isinstance(retirement.get(k), str) and retirement[k] for k in ("notice_date", "source")) or type(retirement.get("technical_disable_verified")) is not bool:
                raise ValueError("missing retirement evidence")
        if r["confidence"] != "REVIEWED" and r["lifecycle"] != "UNRESOLVED":
            raise ValueError("unreviewed authorization")
        if r["semantics"] == "READ" and r["side_effects"] != "NONE":
            raise ValueError("ambiguous read semantics")
        if r["semantics"] == "BUSINESS_STATE_MUTATION" and r["side_effects"] != "BUSINESS":
            raise ValueError("ambiguous mutation semantics")
        if r["semantics"] == "EXTERNAL_SIDE_EFFECT" and r["side_effects"] != "EXTERNAL":
            raise ValueError("ambiguous external semantics")
        if r["lifecycle"] == "ALIAS":
            target = r.get("alias_target")
            if target == path or target not in policy["methods"] or policy["methods"][target].get("lifecycle") != "ACTIVE":
                raise ValueError("ambiguous alias")
            if not r.get("alias_evidence") or r["semantics"] != policy["methods"][target].get("semantics"):
                raise ValueError("unproven alias")
        if r["lifecycle"] == "ACTIVE" and r["semantics"] == "UNPROVEN":
            raise ValueError("active semantics unresolved")
    return policy


def treatment(record, version):
    """Per-capability severity; never used by the HTTP allowlist."""
    if record is None:
        return "FAIL"
    if version != 2:
        raise ValueError("legacy heuristic policy cannot grant authorization")
    life, sem = record["lifecycle"], record["semantics"]
    if sem in ("BUSINESS_STATE_MUTATION", "EXTERNAL_SIDE_EFFECT"):
        return "FAIL"
    if life == "UNRESOLVED":
        return "FAIL"
    if life == "RETIRED":
        return "WARN"
    if sem == "ARTIFACT_GENERATION" and record["side_effects"] == "ARTIFACT":
        return "WARN"
    if sem in ("READ", "VALIDATION_OR_CHECK") and record["side_effects"] == "NONE":
        return "PASS"
    return "FAIL"


def dumps(policy):
    """Stable JSON: retain legacy field order, compact nested provenance without losing it."""
    validate(policy)
    lines = ["{"]
    keys = sorted(policy)
    for i, key in enumerate(keys):
        comma = "," if i < len(keys) - 1 else ""
        if key != "methods":
            lines.append(" " + json.dumps(key) + ": " + json.dumps(policy[key], ensure_ascii=False, sort_keys=True) + comma)
            continue
        lines.append(' "methods": {')
        paths = sorted(policy["methods"])
        for j, path in enumerate(paths):
            lines.append("  " + json.dumps(path) + ": {")
            record = policy["methods"][path]
            fields = ["class", "deprecated", "http", "reason"] + sorted(set(record) - {"class", "deprecated", "http", "reason"})
            for k, field in enumerate(fields):
                suffix = "," if k < len(fields) - 1 else ""
                lines.append("   " + json.dumps(field) + ": " + json.dumps(record[field], ensure_ascii=False, sort_keys=True) + suffix)
            lines.append("  }" + ("," if j < len(paths) - 1 else ""))
        lines.append(" }" + comma)
    return "\n".join([*lines, "}", ""])
