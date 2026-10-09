"""R2.3 old-card migration plan (READ-ONLY). Lists pending R2/R2.1 cards and whether each may
move into the R2.2 operator flow. It never writes Firestore, Telegram or WB; the move itself
(`app.services.pipeline.migrate_to_v31_only`) runs only after an explicit owner ACK.

    python scripts/r23_pending_cards.py            # needs read access to Firestore
"""
import json
import sys
from collections import Counter

sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__file__)))

from app.config import Settings  # noqa: E402
from app.domain.statuses import DRAFTABLE_FROM  # noqa: E402
from app.services.pipeline import migration_plan  # noqa: E402
from app.services.repository import FirestoreRepository  # noqa: E402


def main():
    repo = FirestoreRepository(Settings())
    rows = []
    for status in sorted(DRAFTABLE_FROM):
        for doc_id, doc in repo.list_by_status(status, limit=500):
            rows.append({"id": doc_id, "status": status, "entity": doc.get("entity_type"),
                         "mode": doc.get("operator_mode"), "generation": doc.get("generation_number"),
                         "first_seen": doc.get("first_seen_at"), "plan": migration_plan(doc)})
    for row in sorted(rows, key=lambda r: r["first_seen"] or ""):
        print(json.dumps(row, ensure_ascii=False))
    print(json.dumps(dict(Counter(r["plan"].split(":")[0] for r in rows)), ensure_ascii=False))


if __name__ == "__main__":
    main()
