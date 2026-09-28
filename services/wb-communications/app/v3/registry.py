"""Knowledge registry seed (knowledge_v3/registry/*.yaml) -> REF_* table rows.

The YAML files are the reviewed authoring point. ``registry_tables()`` flattens them into
the exact rows loaded into BigQuery ``evetis_ref`` and consumed by the snapshot builder, so
the BigQuery registry and the snapshot can never drift from each other.

Tables owned by v3 (created and fully reloaded by scripts/v3_registry.py):
  REF_KNOWLEDGE_SOURCE, REF_PRODUCT_KNOWLEDGE_PROFILE (deviation: product header fields),
  REF_PRODUCT_IDENTIFIER, REF_PRODUCT_FACT, REF_USAGE,
  REF_PRODUCT_INGREDIENT, REF_INGREDIENT, REF_CLAIM, REF_KNOWLEDGE_CONFLICT,
  REF_OWNER_DECISION   (+ KNOWLEDGE_SNAPSHOT, append-only).
Read, never written (existing authoritative tables):
  REF_SKU_CHANNEL_MAP (WB identity), REF_PRODUCT_MASTER, REF_BUNDLE_COMPONENTS (BOM).
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
from pathlib import Path

import yaml

SERVICE_ROOT = Path(__file__).resolve().parents[2]
REGISTRY_DIR = SERVICE_ROOT / "knowledge_v3" / "registry"
POLICY_FILE = SERVICE_ROOT / "knowledge_v3" / "policy" / "policy_v3.yaml"

FACT_STATUSES = {"VERIFIED", "COMMERCIAL_VERIFIED", "CONFLICT", "UNKNOWN", "OWNER_APPROVED", "DERIVED", "PROHIBITED"}
RELIABILITY = {"A", "B", "C", "D", "E"}
DISCLOSURE = {"PUBLIC", "PUBLIC_WITH_APPROVED_WORDING", "INTERNAL_ONLY", "DO_NOT_DISCLOSE", "DO_NOT_DISCLOSE_PENDING"}
QUALITY_FLAGS = {"TEMPLATE_SUSPECT", "LABEL_RECIPE_MISMATCH", "ORDER_INCONSISTENT", "OCR_UNREVIEWED", "STALE",
                 "BATCH_SPECIFIC", "RECIPE_SUM_MISMATCH"}
CONFIDENCE = {"HIGH", "MEDIUM", "LOW"}
TIERS = {"T1a", "T1b", "T1c", "T1-derived", "T2", "T3", "T4", "POLICY"}
USAGE_FACT_TYPES = {"directions", "frequency", "amount", "rinse_off", "precautions", "intended_use"}

OWNED_TABLES = ("REF_KNOWLEDGE_SOURCE", "REF_PRODUCT_KNOWLEDGE_PROFILE", "REF_PRODUCT_IDENTIFIER",
                "REF_PRODUCT_FACT", "REF_USAGE",
                "REF_PRODUCT_INGREDIENT", "REF_INGREDIENT", "REF_CLAIM", "REF_KNOWLEDGE_CONFLICT",
                "REF_OWNER_DECISION")


def _load(path: Path) -> dict:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _iso(v) -> str | None:
    if v is None or v == "":
        return None
    if isinstance(v, (_dt.date, _dt.datetime)):
        return v.isoformat()
    return str(v)


def _j(v) -> str:
    return json.dumps(v, ensure_ascii=False, sort_keys=True, default=str)


def load_seed(registry_dir: Path = REGISTRY_DIR) -> dict:
    """Raw YAML documents of the registry."""
    products = {}
    for f in sorted((registry_dir / "products").glob("*.yaml")):
        doc = _load(f)
        products[doc["product_id"]] = doc
    return {
        "sources": _load(registry_dir / "sources.yaml")["sources"],
        "ingredients": _load(registry_dir / "ingredients.yaml")["ingredients"],
        "products": products,
        "conflicts": _load(registry_dir / "conflicts.yaml")["conflicts"],
        "owner_decisions": _load(registry_dir / "owner_decisions.yaml")["decisions"],
        "claims": _load(registry_dir / "claims.yaml")["claims"],
    }


def load_policy(path: Path = POLICY_FILE) -> dict:
    return _load(path)


def seed_sha256(registry_dir: Path = REGISTRY_DIR, policy_file: Path = POLICY_FILE) -> str:
    h = hashlib.sha256()
    for f in sorted(registry_dir.rglob("*.yaml")) + [policy_file]:
        h.update(str(f.relative_to(SERVICE_ROOT)).encode())
        h.update(f.read_bytes())
    return h.hexdigest()


def registry_tables(seed: dict | None = None) -> dict[str, list[dict]]:
    """Flatten the seed into REF_* rows (all values JSON/BigQuery friendly)."""
    seed = seed or load_seed()
    sources = {s["source_id"]: s for s in seed["sources"]}
    t: dict[str, list[dict]] = {name: [] for name in OWNED_TABLES}

    for s in seed["sources"]:
        t["REF_KNOWLEDGE_SOURCE"].append({
            "source_id": s["source_id"], "tier": s["tier"], "source_type": s.get("source_type"),
            "document_code": s.get("document_code"), "document_name": s.get("document_name"),
            "document_version": s.get("document_version"), "document_date": _iso(s.get("document_date")),
            "file_uri": s.get("file_uri"), "file_sha256": s.get("file_sha256"),
            "extraction_method": s.get("extraction_method"),
            "extraction_confidence": s.get("extraction_confidence"), "reliability": s.get("reliability"),
            "quality_notes": s.get("quality_notes"),
        })

    for i in seed["ingredients"]:
        t["REF_INGREDIENT"].append({
            "ingredient_id": i["ingredient_id"], "inci": i["inci"], "ru_name": i.get("ru_name") or None,
            "patterns_json": _j(i.get("patterns") or []), "family": i.get("family"),
            "classes_json": _j(i.get("classes") or []), "note": i.get("note"),
        })

    for pid, p in seed["products"].items():
        derived = p.get("spec_association_status") == "DERIVED"
        ing0 = p.get("ingredients") or {}
        t["REF_PRODUCT_KNOWLEDGE_PROFILE"].append({
            "product_id": pid, "product_type": p.get("product_type"), "line": p.get("line"),
            "customer_name_ru": p.get("customer_name_ru"),
            "internal_aliases_json": _j(p.get("internal_aliases") or []),
            "spec_association_status": p.get("spec_association_status"),
            "customer_fact_generation": p.get("customer_fact_generation", "ALLOWED"),
            "primary_source": p.get("primary_source"),
            "recipe_status": ing0.get("recipe_status"), "label_status": ing0.get("label_status"),
        })
        for ident in p.get("identifiers") or []:
            t["REF_PRODUCT_IDENTIFIER"].append({
                "product_id": pid, "id_type": ident["id_type"], "value": str(ident["value"]),
                "status": ident.get("status", "VERIFIED"), "source_id": ident.get("source_id"),
                "locator": ident.get("locator"), "notes": ident.get("notes"),
            })
        counts: dict[str, int] = {}
        for f in p.get("facts") or []:
            ft = f["fact_type"]
            counts[ft] = counts.get(ft, 0) + 1
            fact_id = f"{pid}.{ft}" + (f".{counts[ft]}" if counts[ft] > 1 else "")
            src = sources.get(f.get("source_id"), {})
            status = f.get("fact_status") or ("DERIVED" if derived else "VERIFIED")
            row = {
                "fact_id": fact_id, "product_id": pid, "fact_type": ft,
                "value_json": _j(f.get("value")), "unit": f.get("unit"),
                "customer_value_ru": f.get("customer_value_ru"),
                "source_id": f.get("source_id"), "source_tier": src.get("tier"),
                "fact_status": status, "reliability": f.get("reliability") or src.get("reliability"),
                "extraction_confidence": f.get("extraction_confidence") or src.get("extraction_confidence"),
                "disclosure_policy": f.get("disclosure_policy") or "PUBLIC",
                "quality_flags_json": _j(f.get("quality_flags") or []),
                "locator": f.get("locator"), "conflict_id": f.get("conflict_id"),
                "owner_decision_id": f.get("owner_decision_id"),
                "valid_from": _iso(src.get("document_date")), "valid_to": None, "notes": f.get("notes"),
            }
            t["REF_USAGE" if ft in USAGE_FACT_TYPES else "REF_PRODUCT_FACT"].append(row)
        ing = p.get("ingredients") or {}
        disclosure = ing.get("concentration_disclosure") or {}
        for kind in ("recipe", "label"):
            src_id = ing.get(f"{kind}_source")
            src = sources.get(src_id, {})
            flags = ing.get(f"{kind}_quality_flags") or []
            for row in ing.get(kind) or []:
                pos, iid, written = row[0], row[1], row[2]
                pct = row[3] if kind == "recipe" and len(row) > 3 else None
                d = disclosure.get(iid, {}) if kind == "recipe" else {}
                t["REF_PRODUCT_INGREDIENT"].append({
                    "occurrence_id": f"{pid}.{kind}.{pos}", "product_id": pid, "list_kind": kind,
                    "position": pos, "ingredient_id": iid, "inci_as_written": written,
                    "concentration_pct": pct, "concentration_basis": "mixture" if pct is not None else None,
                    "source_id": src_id, "locator": ing.get(f"{kind}_locator"),
                    "fact_status": "DERIVED" if derived else "VERIFIED",
                    "extraction_confidence": src.get("extraction_confidence"),
                    "concentration_disclosure_policy": d.get("disclosure_policy", "PUBLIC") if pct is not None else None,
                    "owner_decision_id": d.get("owner_decision_id"), "conflict_id": d.get("conflict_id"),
                    "customer_abstraction_ru": d.get("customer_abstraction_ru"),
                    "quality_flags_json": _j(flags),
                })

    for c in seed["conflicts"]:
        t["REF_KNOWLEDGE_CONFLICT"].append({
            "conflict_id": c["conflict_id"], "fact_type": c.get("fact_type"), "subject": c.get("subject"),
            "products_json": _j(c.get("products") or []), "status": c.get("status"),
            "default_behaviour": c.get("default_behaviour"), "owner_decision_id": c.get("owner_decision_id"),
            "blocked_values_json": _j(c.get("blocked_values") or []),
            "candidates_json": _j(c.get("candidates") or []), "note": c.get("note"),
        })
    for d in seed["owner_decisions"]:
        t["REF_OWNER_DECISION"].append({
            "decision_id": d["decision_id"], "status": d["status"], "topic": d.get("topic"),
            "decision": d.get("decision"), "template_ids_json": _j(d.get("template_ids") or []),
            "affected_facts_json": _j(d.get("affected_facts") or []), "safe_default": d.get("safe_default"),
            "decided_at": _iso(d.get("decided_at")),
        })
    for c in seed["claims"]:
        t["REF_CLAIM"].append({
            "claim_id": c["claim_id"], "claim_class": c["claim_class"], "text_ru": c["text_ru"],
            "source_id": c.get("source_id"), "locator": c.get("locator"),
            "evidence_fact_type": c.get("evidence_fact_type"), "note": c.get("note"),
        })
    return t


def _canon_row(row: dict) -> dict:
    """Type-stable form for hashing: BigQuery returns FLOAT64 5.0 for a seed 5 (and None for
    absent keys), so numbers are compared as floats and None-valued keys are dropped."""
    out = {}
    for k, v in row.items():
        if v is None:
            continue
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            v = float(v)
        out[k] = v
    return out


def rows_sha256(rows: list[dict]) -> str:
    canon = [_canon_row(r) for r in rows]
    return hashlib.sha256(_j(sorted(canon, key=_j)).encode()).hexdigest()


# BigQuery schemas of the v3-owned tables (evetis_ref). Everything STRING except the few
# numeric/ordinal columns; JSON payloads are STRING columns suffixed *_json.
_INT_COLS = {"position"}
_FLOAT_COLS = {"concentration_pct"}


def table_schema(name: str, rows: list[dict] | None = None) -> list[tuple[str, str]]:
    rows = rows if rows is not None else registry_tables()[name]
    cols: list[str] = []
    for r in rows:
        for k in r:
            if k not in cols:
                cols.append(k)
    return [(c, "INT64" if c in _INT_COLS else "FLOAT64" if c in _FLOAT_COLS else "STRING") for c in cols]


KNOWLEDGE_SNAPSHOT_SCHEMA = [
    ("snapshot_id", "STRING"), ("generated_at", "TIMESTAMP"), ("content_sha256", "STRING"),
    ("schema_version", "STRING"), ("engine_version", "STRING"), ("policy_version", "STRING"),
    ("registry_seed_sha256", "STRING"), ("origin", "STRING"), ("validation_status", "STRING"),
    ("manifest_json", "STRING"), ("activated", "BOOL"),
]
