"""Runtime access to an immutable, explicitly selected knowledge snapshot.

The service never queries the mutable BigQuery registry at runtime: it loads one JSON
snapshot baked into the image (``app/v3/snapshots/<snapshot_id>.json``), verifies its
content hash and gate status, and indexes it. Rollback = select an older snapshot id.
"""
from __future__ import annotations

import json
import re
from functools import cached_property
from pathlib import Path
from typing import Optional

from app.v3.snapshot_builder import SCHEMA_VERSION, content_hash
from app.v3.text import normalize

SNAPSHOT_DIR = Path(__file__).resolve().parent / "snapshots"
ACTIVE_FILE = SNAPSHOT_DIR / "ACTIVE"


class KnowledgeSnapshotError(Exception):
    """Snapshot missing, tampered, wrong schema or gate not passed (KNOWLEDGE_SNAPSHOT_ERROR)."""


def active_snapshot_id() -> str:
    return ACTIVE_FILE.read_text(encoding="utf-8").strip()


def load_snapshot(snapshot_id: str | None = None, directory: Path = SNAPSHOT_DIR) -> "KnowledgeSnapshot":
    sid = (snapshot_id or "").strip() or active_snapshot_id()
    path = directory / f"{sid}.json"
    if not path.exists():
        raise KnowledgeSnapshotError(f"snapshot {sid} not found")
    data = json.loads(path.read_text(encoding="utf-8"))
    return KnowledgeSnapshot(data)


class KnowledgeSnapshot:
    def __init__(self, data: dict):
        if data.get("schema_version") != SCHEMA_VERSION:
            raise KnowledgeSnapshotError(f"unsupported schema {data.get('schema_version')}")
        if (data.get("validation") or {}).get("status") != "PASS":
            raise KnowledgeSnapshotError("snapshot did not pass the knowledge build gate")
        if content_hash(data) != data.get("content_sha256"):
            raise KnowledgeSnapshotError("snapshot content hash mismatch (tampered or corrupt)")
        self.data = data
        self.snapshot_id: str = data["snapshot_id"]
        self.policy: dict = data["policy"]
        self.products: dict[str, dict] = data["products"]
        self.ingredients: dict[str, dict] = data["ingredients"]
        self.conflicts: dict[str, dict] = data["conflicts"]
        self.decisions: dict[str, dict] = data["owner_decisions"]
        self._idx: dict[tuple[str, str], str] = {}
        for i in data["identifiers"]:
            if i["status"] == "VERIFIED":
                self._idx[(i["id_type"], normalize(i["value"]))] = i["product_id"]

    # --- identity -------------------------------------------------------------------------
    def product_by_identifier(self, id_type: str, value) -> Optional[str]:
        if value in (None, ""):
            return None
        return self._idx.get((id_type, normalize(str(value))))

    def product(self, product_id: str) -> Optional[dict]:
        return self.products.get(product_id)

    def components(self, product_id: str) -> list[str]:
        p = self.products.get(product_id) or {}
        return list(p.get("components") or []) if p.get("kind") == "bundle" else [product_id]

    # --- facts ----------------------------------------------------------------------------
    def facts(self, product_id: str, fact_type: str) -> list[dict]:
        p = self.products.get(product_id) or {}
        return [f for f in p.get("facts", []) if f["fact_type"] == fact_type]

    def ingredient_rows(self, product_id: str, kind: str = "recipe") -> list[dict]:
        p = self.products.get(product_id) or {}
        return list(((p.get("ingredients") or {}).get(kind)) or [])

    def reconciliation(self, product_id: str) -> str:
        p = self.products.get(product_id) or {}
        return ((p.get("ingredients") or {}).get("reconciliation")) or "UNAVAILABLE"

    def template(self, template_id: str) -> str:
        return self.policy["templates"][template_id]["text"]

    # --- ingredient mention detection -----------------------------------------------------
    @cached_property
    def _ingredient_patterns(self) -> list[tuple[str, re.Pattern]]:
        out = []
        for iid, ing in self.ingredients.items():
            for pat in ing.get("patterns") or []:
                out.append((iid, re.compile(pat)))
        return out

    def ingredient_mentions(self, normalized_text: str) -> list[tuple[str, tuple[int, int]]]:
        """(ingredient_id, span) for every detectable mention; spans strictly inside a longer
        match of a DIFFERENT pattern are dropped (so 'масло чайного дерева' does not also
        count as the generic 'чайного дерева' of the extract)."""
        hits = []
        for iid, rx in self._ingredient_patterns:
            for m in rx.finditer(normalized_text):
                hits.append((iid, m.span()))
        # Canonical identities outrank shared stems. A generic family alias remains ambiguous;
        # it is NEVER silently converted to whichever relative happens to be in this product.
        canonical = {
            "hyaluronic_acid": (r"гиалуронов\w*\s+кислот\w*", r"\bhyaluronic acid\b"),
            "sodium_hyaluronate": (r"гиалуронат\w*\s+натри\w*", r"\bsodium hyaluronate\b"),
            "salicylic_acid": (r"\bsalicylic acid\b",),
        }
        for iid, patterns in canonical.items():
            if iid in self.ingredients:
                for pattern in patterns:
                    hits.extend((iid, m.span()) for m in re.finditer(pattern, normalized_text))
        keep = []
        for iid, (a, b) in hits:
            inside = any((a2 <= a and b <= b2) and (b2 - a2) > (b - a) and iid2 != iid
                         for iid2, (a2, b2) in hits)
            if not inside:
                if (iid, (a, b)) not in keep:
                    keep.append((iid, (a, b)))
        return keep

    def family_members(self, ingredient_id: str) -> list[str]:
        fam = (self.ingredients.get(ingredient_id) or {}).get("family")
        if not fam:
            return [ingredient_id]
        return [i for i, v in self.ingredients.items() if v.get("family") == fam]

    def ingredient_display(self, ingredient_id: str) -> str:
        ing = self.ingredients.get(ingredient_id) or {}
        return ing.get("ru_name") or ing.get("inci") or ingredient_id
