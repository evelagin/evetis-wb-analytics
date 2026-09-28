"""Build the immutable v3 knowledge snapshot + run the knowledge build gate.

Input: REF_* rows (the v3-owned registry tables, read from BigQuery in production or from
``registry_tables()`` offline) + the existing authoritative tables REF_SKU_CHANNEL_MAP (WB
rows), REF_PRODUCT_MASTER and REF_BUNDLE_COMPONENTS + the runtime policy.

Output: (snapshot dict, validation report). A snapshot whose report has errors must never be
written (``build_snapshot`` raises ``SnapshotGateError``).
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import re
from typing import Any

from app.v3 import ENGINE_VERSION
from app.v3.registry import (CONFIDENCE, DISCLOSURE, FACT_STATUSES, QUALITY_FLAGS, RELIABILITY, TIERS,
                             OWNED_TABLES, rows_sha256)

SCHEMA_VERSION = "knowledge_snapshot.v3.1"
BUILDER_VERSION = "v3.snapshot_builder.1"
EXTERNAL_TABLES = ("REF_SKU_CHANNEL_MAP", "REF_PRODUCT_MASTER", "REF_BUNDLE_COMPONENTS")
RESTRICTED_DISCLOSURE = {"INTERNAL_ONLY", "DO_NOT_DISCLOSE", "DO_NOT_DISCLOSE_PENDING"}
CUSTOMER_DISCLOSURE = {"PUBLIC", "PUBLIC_WITH_APPROVED_WORDING"}
_SECRET_RX = re.compile(r"\d{6,}:[A-Za-z0-9_-]{30,}|\beyJ[A-Za-z0-9._\-]{20,}|sk-[A-Za-z0-9]{20,}")


class SnapshotGateError(Exception):
    def __init__(self, report: dict):
        super().__init__("knowledge build gate FAILED: " + "; ".join(report["errors"][:10]))
        self.report = report


def _loads(v, default):
    if v is None or v == "":
        return default
    return json.loads(v) if isinstance(v, str) else v


def _canonical(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def content_hash(snapshot: dict) -> str:
    body = {k: v for k, v in snapshot.items() if k not in ("content_sha256",)}
    return hashlib.sha256(_canonical(body).encode()).hexdigest()


def _reconcile(recipe: list[dict], label: list[dict], label_status: str | None) -> str:
    if label_status == "RESTRICTED":
        return "RESTRICTED"
    if not label:
        return "LABEL_UNAVAILABLE"
    r = [x["ingredient_id"] for x in sorted(recipe, key=lambda x: x["position"])]
    lb = [x["ingredient_id"] for x in sorted(label, key=lambda x: x["position"])]
    if set(r) != set(lb) or len(r) != len(lb):
        return "CONTENT_MISMATCH"
    return "MATCH" if r == lb else "ORDER_ONLY"


def assemble(tables: dict[str, list[dict]], policy: dict, *, as_of: str | None = None) -> dict:
    """Assemble the snapshot body (no id/hash yet)."""
    as_of = as_of or _dt.date.today().isoformat()
    sources = {r["source_id"]: r for r in tables["REF_KNOWLEDGE_SOURCE"]}
    ingredients = {}
    for r in tables["REF_INGREDIENT"]:
        ingredients[r["ingredient_id"]] = {
            "ingredient_id": r["ingredient_id"], "inci": r["inci"], "ru_name": r.get("ru_name"),
            "patterns": _loads(r.get("patterns_json"), []), "family": r.get("family"),
            "classes": _loads(r.get("classes_json"), []),
        }
    master = {r["internal_sku"]: r for r in tables["REF_PRODUCT_MASTER"]}

    products: dict[str, dict] = {}
    seeded = {r["product_id"] for r in tables["REF_PRODUCT_FACT"] + tables["REF_USAGE"]} | \
             {r["product_id"] for r in tables["REF_PRODUCT_INGREDIENT"]} | \
             {r["product_id"] for r in tables["REF_PRODUCT_IDENTIFIER"]}
    meta_by = {m["product_id"]: dict(m, internal_aliases=_loads(m.get("internal_aliases_json"), []))
               for m in tables["REF_PRODUCT_KNOWLEDGE_PROFILE"]}
    for pid in sorted(seeded | set(meta_by)):
        m = meta_by.get(pid, {})
        facts = sorted([dict(r, value=_loads(r.get("value_json"), None),
                             quality_flags=_loads(r.get("quality_flags_json"), []))
                        for r in tables["REF_PRODUCT_FACT"] + tables["REF_USAGE"] if r["product_id"] == pid],
                       key=lambda r: r["fact_id"])
        for f in facts:
            f.pop("value_json", None)
            f.pop("quality_flags_json", None)
        occ = [dict(r, quality_flags=_loads(r.get("quality_flags_json"), []))
               for r in tables["REF_PRODUCT_INGREDIENT"] if r["product_id"] == pid]
        for o in occ:
            o.pop("quality_flags_json", None)
        recipe = sorted([o for o in occ if o["list_kind"] == "recipe"], key=lambda o: o["position"])
        label = sorted([o for o in occ if o["list_kind"] == "label"], key=lambda o: o["position"])
        products[pid] = {
            "product_id": pid, "kind": "single",
            "product_type": m.get("product_type"), "line": m.get("line"),
            "customer_name_ru": m.get("customer_name_ru"),
            "commercial_name": (master.get(pid) or {}).get("canonical_product_name"),
            "internal_aliases": m.get("internal_aliases") or [],
            "identity_status": "VERIFIED" if pid in master else "UNVERIFIED",
            "spec_association_status": m.get("spec_association_status"),
            "customer_fact_generation": m.get("customer_fact_generation", "ALLOWED"),
            "primary_source": m.get("primary_source"),
            "facts": facts,
            "ingredients": {
                "recipe": recipe, "label": label,
                "recipe_status": m.get("recipe_status") or ("AVAILABLE" if recipe else "UNAVAILABLE"),
                "label_status": m.get("label_status") or ("AVAILABLE" if label else "UNAVAILABLE"),
                "reconciliation": _reconcile(recipe, label, m.get("label_status")),
                "recipe_quality_flags": recipe[0]["quality_flags"] if recipe else [],
            },
            "conflicts": sorted({c["conflict_id"] for c in tables["REF_KNOWLEDGE_CONFLICT"]
                                 if pid in _loads(c.get("products_json"), [])}),
        }

    # bundles from the authoritative BOM, effective as_of the build date
    bundles: dict[str, dict] = {}
    for r in tables["REF_BUNDLE_COMPONENTS"]:
        frm, to = str(r.get("effective_from") or ""), str(r.get("effective_to") or "")
        if frm and frm > as_of or to and to < as_of:
            continue
        b = bundles.setdefault(r["bundle_internal_sku"], {"components": [], "effective_from": frm})
        b["components"].append(r["component_internal_sku"])
        b["effective_from"] = min(b["effective_from"], frm) if b["effective_from"] else frm
    for bid, b in bundles.items():
        comps = sorted(set(b["components"]))
        names = [products.get(c, {}).get("customer_name_ru") or c for c in comps]
        products[bid] = {
            "product_id": bid, "kind": "bundle", "product_type": "bundle", "components": comps,
            "customer_name_ru": "набор EVETIS: " + " + ".join(names),
            "commercial_name": (master.get(bid) or {}).get("canonical_product_name"),
            "identity_status": "VERIFIED" if bid in master else "UNVERIFIED",
            "spec_association_status": "BOM", "bom_effective_from": b["effective_from"],
            "customer_fact_generation": ("RESTRICTED_PARTIAL" if any(
                products.get(c, {}).get("customer_fact_generation") == "RESTRICTED" for c in comps) else "ALLOWED"),
            "facts": [], "ingredients": None, "conflicts": [],
        }

    identifiers = []
    for r in tables["REF_SKU_CHANNEL_MAP"]:
        if str(r.get("marketplace")).upper() != "WB" or str(r.get("is_current")).lower() not in ("true", "1"):
            continue
        identifiers.append({"id_type": "wb_nm_id", "value": str(r["marketplace_sku"]),
                            "product_id": r["internal_sku"], "status": "VERIFIED",
                            "source_id": "SRC-REF-CHANNEL-MAP"})
        if r.get("vendor_code"):
            identifiers.append({"id_type": "wb_vendor_code", "value": str(r["vendor_code"]),
                                "product_id": r["internal_sku"], "status": "VERIFIED",
                                "source_id": "SRC-REF-CHANNEL-MAP"})
    for r in tables["REF_PRODUCT_IDENTIFIER"]:
        identifiers.append({"id_type": r["id_type"], "value": r["value"], "product_id": r["product_id"],
                            "status": r.get("status") or "VERIFIED", "source_id": r.get("source_id")})

    claim_policy = policy.get("claim_policy", {})
    enabled_classes = set(claim_policy.get("enabled_classes") or [])
    claims = [dict(c, generation_enabled=c["claim_class"] in enabled_classes) for c in tables["REF_CLAIM"]]
    conflicts = {c["conflict_id"]: dict(c, products=_loads(c.get("products_json"), []),
                                        blocked_values=_loads(c.get("blocked_values_json"), []),
                                        candidates=_loads(c.get("candidates_json"), []))
                 for c in tables["REF_KNOWLEDGE_CONFLICT"]}
    for c in conflicts.values():
        for k in ("products_json", "blocked_values_json", "candidates_json"):
            c.pop(k, None)
    decisions = {d["decision_id"]: dict(d, template_ids=_loads(d.get("template_ids_json"), []),
                                        affected_facts=_loads(d.get("affected_facts_json"), []))
                 for d in tables["REF_OWNER_DECISION"]}
    for d in decisions.values():
        d.pop("template_ids_json", None)
        d.pop("affected_facts_json", None)

    return {
        "schema_version": SCHEMA_VERSION, "builder_version": BUILDER_VERSION,
        "engine_version": ENGINE_VERSION, "as_of": as_of,
        "policy": policy, "sources": sources, "ingredients": ingredients, "products": products,
        "identifiers": sorted(identifiers, key=lambda i: (i["id_type"], i["value"], i["product_id"])),
        "claims": claims, "conflicts": conflicts, "owner_decisions": decisions,
    }


def validate(snap: dict, tables: dict[str, list[dict]]) -> dict:
    """The knowledge build gate. Returns a machine-readable report."""
    errors: list[str] = []
    warnings: list[str] = []
    checks: dict[str, str] = {}

    def check(name: str, problems: list[str], *, warn: bool = False):
        checks[name] = "PASS" if not problems else ("WARN" if warn else "FAIL")
        (warnings if warn else errors).extend(f"{name}: {p}" for p in problems)

    products, sources, ingredients = snap["products"], snap["sources"], snap["ingredients"]
    policy = snap["policy"]

    # 1. duplicate active identifiers
    seen: dict[tuple, set] = {}
    for i in snap["identifiers"]:
        if i["status"] == "VERIFIED":
            seen.setdefault((i["id_type"], i["value"]), set()).add(i["product_id"])
    check("unique_active_identifiers", [f"{k} -> {sorted(v)}" for k, v in seen.items() if len(v) > 1])
    # 2. identifiers and bundles reference known products
    check("identifier_targets_known", sorted({f"{i['id_type']}={i['value']} -> {i['product_id']}"
                                              for i in snap["identifiers"] if i["product_id"] not in products}))
    check("bundle_components_known", [f"{b['product_id']} -> {c}" for b in products.values()
                                      if b["kind"] == "bundle" for c in b["components"] if c not in products])
    # 3. every WB nmId resolves; every registry single has a WB nmId
    nm_products = {i["product_id"] for i in snap["identifiers"] if i["id_type"] == "wb_nm_id"}
    check("singles_have_wb_identity", [p for p, v in products.items()
                                       if v["kind"] == "single" and p not in nm_products])
    # 4. facts: sources, enums, restricted values
    probs, enum_probs, restricted_probs = [], [], []
    for p in products.values():
        for f in p["facts"]:
            if not f.get("source_id") or f["source_id"] not in sources:
                probs.append(f"{f['fact_id']} source={f.get('source_id')}")
            if f["fact_status"] not in FACT_STATUSES:
                enum_probs.append(f"{f['fact_id']} fact_status={f['fact_status']}")
            if f["disclosure_policy"] not in DISCLOSURE:
                enum_probs.append(f"{f['fact_id']} disclosure={f['disclosure_policy']}")
            if f.get("reliability") not in RELIABILITY:
                enum_probs.append(f"{f['fact_id']} reliability={f.get('reliability')}")
            if f.get("extraction_confidence") not in CONFIDENCE:
                enum_probs.append(f"{f['fact_id']} confidence={f.get('extraction_confidence')}")
            if f.get("source_tier") not in TIERS:
                enum_probs.append(f"{f['fact_id']} tier={f.get('source_tier')}")
            for q in f.get("quality_flags") or []:
                if q not in QUALITY_FLAGS:
                    enum_probs.append(f"{f['fact_id']} flag={q}")
            public = f["disclosure_policy"] in CUSTOMER_DISCLOSURE
            if public and f["fact_status"] in ("CONFLICT", "PROHIBITED", "UNKNOWN"):
                restricted_probs.append(f"{f['fact_id']} is {f['fact_status']} but {f['disclosure_policy']}")
            if public and "TEMPLATE_SUSPECT" in (f.get("quality_flags") or []):
                restricted_probs.append(f"{f['fact_id']} TEMPLATE_SUSPECT but {f['disclosure_policy']}")
            if public and f["fact_status"] == "COMMERCIAL_VERIFIED" and \
                    f["fact_type"] not in policy.get("commercial_verified_allowlist", []):
                restricted_probs.append(f"{f['fact_id']} COMMERCIAL_VERIFIED {f['fact_type']} not in ODR-12 allowlist")
            if public and not f.get("customer_value_ru") and f["fact_type"] not in ("product_name_t1",):
                restricted_probs.append(f"{f['fact_id']} customer-facing without customer_value_ru")
            if public and p.get("customer_fact_generation") == "RESTRICTED":
                restricted_probs.append(f"{f['fact_id']} PUBLIC on a RESTRICTED product")
        ing = p.get("ingredients") or {}
        for o in ing.get("recipe", []) + ing.get("label", []):
            if o["ingredient_id"] not in ingredients:
                enum_probs.append(f"{o['occurrence_id']} unknown ingredient {o['ingredient_id']}")
            if o.get("source_id") not in sources:
                probs.append(f"{o['occurrence_id']} source={o.get('source_id')}")
            cdp = o.get("concentration_disclosure_policy")
            if cdp is not None and cdp not in DISCLOSURE:
                enum_probs.append(f"{o['occurrence_id']} concentration disclosure={cdp}")
        if p.get("customer_fact_generation") == "RESTRICTED" and (ing.get("recipe") or ing.get("label")):
            restricted_probs.append(f"{p['product_id']} RESTRICTED but has customer ingredient rows")
    check("facts_have_known_sources", probs)
    check("enums_known", enum_probs)
    check("restricted_not_public", restricted_probs)

    # 5. conflict blocked values never PUBLIC; owner-decision affected facts exist
    block_probs = []
    for c in snap["conflicts"].values():
        for pid in c["products"]:
            if pid not in products:
                block_probs.append(f"{c['conflict_id']} references unknown product {pid}")
                continue
            for bv in c.get("blocked_values") or []:
                for f in products[pid]["facts"]:
                    if f["disclosure_policy"] in CUSTOMER_DISCLOSURE and \
                            bv.lower() in json.dumps(f.get("value"), ensure_ascii=False).lower() + \
                            str(f.get("customer_value_ru") or "").lower():
                        block_probs.append(f"{c['conflict_id']} blocked value '{bv}' is PUBLIC in {f['fact_id']}")
    fact_ids = {f["fact_id"] for p in products.values() for f in p["facts"]}
    occ_keys = {f"{p['product_id']}.ingredient.{o['list_kind']}.{o['ingredient_id']}"
                for p in products.values() for o in (p.get("ingredients") or {}).get("recipe", [])}
    for d in snap["owner_decisions"].values():
        for af in d.get("affected_facts") or []:
            if af not in fact_ids and af not in occ_keys:
                block_probs.append(f"{d['decision_id']} references missing fact {af}")
            elif af in occ_keys:
                pid, _, _, iid = af.split(".", 3)
                occ = [o for o in products[pid]["ingredients"]["recipe"] if o["ingredient_id"] == iid]
                if any(o.get("concentration_disclosure_policy") in CUSTOMER_DISCLOSURE for o in occ):
                    block_probs.append(f"{d['decision_id']} restricts {af} but it is PUBLIC")
    check("conflicts_and_decisions_consistent", block_probs)

    # 6. CONTENT_MISMATCH must be visible and mapped to a conflict record
    mm = [p["product_id"] for p in products.values()
          if (p.get("ingredients") or {}).get("reconciliation") == "CONTENT_MISMATCH"
          and not any(snap["conflicts"].get(c, {}).get("fact_type") == "label_inci" for c in p["conflicts"])]
    check("content_mismatch_has_conflict", mm)

    # 7. claims: only enabled classes may generate; cosmetic generation must stay disabled
    cp = policy.get("claim_policy", {})
    claim_probs = [c["claim_id"] for c in snap["claims"] if c["generation_enabled"] and c["claim_class"] != "FACT_CLAIM"]
    if cp.get("cosmetic_claim_generation") is not False:
        claim_probs.append("policy.claim_policy.cosmetic_claim_generation must be false (ODR-06)")
    if policy.get("runtime", {}).get("auto_publish") is not False:
        claim_probs.append("policy.runtime.auto_publish must be false (ODR-18)")
    if policy.get("approved_general_guidance"):
        claim_probs.extend(f"guidance {g.get('guidance_id')} present: requires owner approval record"
                           for g in policy["approved_general_guidance"] if not g.get("approved_by"))
    check("claim_policy", claim_probs)

    # 8. recipe integrity
    sum_probs, sum_warn = [], []
    for p in products.values():
        ing = p.get("ingredients") or {}
        rec = ing.get("recipe") or []
        if rec:
            total = round(sum(o["concentration_pct"] or 0 for o in rec), 3)
            if abs(total - 100) > 0.05:
                (sum_warn if "RECIPE_SUM_MISMATCH" in ing.get("recipe_quality_flags", []) else sum_probs).append(
                    f"{p['product_id']} recipe sums to {total}")
    check("recipe_sum_100", sum_probs)
    check("recipe_sum_flagged_documents", sum_warn, warn=True)

    # 9. policy regexes compile; templates referenced by decisions exist
    rx_probs = []

    def walk(obj, path):
        if isinstance(obj, dict):
            for k, v in obj.items():
                walk(v, f"{path}.{k}")
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                walk(v, f"{path}[{i}]")
        elif isinstance(obj, str) and any(ch in obj for ch in "\\[]()|^$") and ".templates." not in path:
            try:
                re.compile(obj)
            except re.error as exc:
                rx_probs.append(f"{path}: {exc}")
    for key in ("safety", "situations", "verifier", "trademark_policy", "ceramide_policy", "product_type_words"):
        walk(policy.get(key), key)
    for i in ingredients.values():
        for pat in i["patterns"]:
            try:
                re.compile(pat)
            except re.error as exc:
                rx_probs.append(f"ingredient {i['ingredient_id']}: {exc}")
    tpl = set(policy.get("templates", {}))
    rx_probs.extend(f"{d['decision_id']} -> missing template {t}" for d in snap["owner_decisions"].values()
                    for t in d.get("template_ids") or [] if t not in tpl)
    check("policy_compiles", rx_probs)

    # 10. no secrets in the snapshot
    check("no_secrets", ["secret-like token found"] if _SECRET_RX.search(_canonical(snap)) else [])

    # 11. warnings (quality, not blocking)
    low = [f"{f['fact_id']}" for p in products.values() for f in p["facts"]
           if f.get("extraction_confidence") == "LOW" and f["disclosure_policy"] in CUSTOMER_DISCLOSURE]
    check("public_facts_low_confidence", low, warn=True)
    manifest = {name: {"rows": len(tables.get(name, [])), "sha256": rows_sha256(tables.get(name, []))}
                for name in OWNED_TABLES + EXTERNAL_TABLES}
    return {"status": "PASS" if not errors else "FAIL", "checks": checks, "errors": errors,
            "warnings": warnings, "table_manifest": manifest}


def build_snapshot(tables: dict[str, list[dict]], policy: dict, *, seed_sha256: str,
                   origin: str, as_of: str | None = None, generated_at: str | None = None) -> tuple[dict, dict]:
    """Assemble + validate. Raises SnapshotGateError on any gate error."""
    snap = assemble(tables, policy, as_of=as_of)
    report = validate(snap, tables)
    if report["status"] != "PASS":
        raise SnapshotGateError(report)
    generated_at = generated_at or _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    snap["generated_at"] = generated_at
    snap["source_manifest"] = {"origin": origin, "registry_seed_sha256": seed_sha256,
                               "policy_version": policy.get("policy_version"),
                               "tables": report["table_manifest"],
                               "sources": sorted(snap["sources"])}
    snap["validation"] = {k: report[k] for k in ("status", "checks", "warnings")}
    digest = content_hash(snap)
    snap["snapshot_id"] = "ks_v3_" + generated_at.replace("-", "").replace(":", "").replace("T", "T")[:15] + "_" + digest[:8]
    snap["content_sha256"] = content_hash(snap)
    return snap, report
