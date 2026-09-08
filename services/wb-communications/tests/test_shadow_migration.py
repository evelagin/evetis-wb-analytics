"""Offline tests for the BigQuery shadow-table schema migration.

Only the network primitives are overridden; the migration ORCHESTRATION
(create / ADD COLUMN / re-read / fail-fast) is exercised for real.
"""
from __future__ import annotations

import pytest

from app.config import Settings
from app.services.bigquery_repository import (
    SHADOW_SCHEMA,
    BigQueryRepository,
    ShadowSchemaError,
)

_ALL = {c: t for c, t in SHADOW_SCHEMA}
_OLD_V120 = {c: t for c, t in SHADOW_SCHEMA if c != "shadow_id"}  # 23-col table, no shadow_id


class FakeMigrationRepo(BigQueryRepository):
    """In-memory shadow table. ``initial`` is a {name: type} dict, or None for a
    non-existent table. ``apply_alter`` controls whether ADD COLUMN takes effect
    (set False to simulate a migration that silently did not stick)."""

    def __init__(self, initial, apply_alter: bool = True):
        super().__init__(Settings(gcp_project_id="p"))
        self._state = None if initial is None else dict(initial)
        self._apply_alter = apply_alter
        self.alters: list = []
        self.created = False

    def _ensure_dataset(self) -> None:  # no network
        pass

    def _get_shadow_schema(self):
        return None if self._state is None else dict(self._state)

    def _create_shadow_table(self) -> None:
        self.created = True
        self._state = {c: t for c, t in SHADOW_SCHEMA}

    def _add_shadow_columns(self, columns) -> None:
        self.alters.append(list(columns))
        if not self._apply_alter:
            return
        for c, t in columns:
            self._state.setdefault(c, t)


# 1) a brand-new table is created straight away WITH shadow_id -----------------
def test_new_table_created_with_shadow_id():
    repo = FakeMigrationRepo(initial=None)
    result = repo.ensure_shadow_schema()
    assert result["table_created"] is True
    assert repo.created is True
    assert "shadow_id" in repo._state
    assert "shadow_id" in result["columns_added"]


# 2) an old table without shadow_id gets ADD COLUMN --------------------------
def test_existing_old_table_gets_add_column():
    repo = FakeMigrationRepo(initial=_OLD_V120)
    result = repo.ensure_shadow_schema()
    assert result["table_created"] is False
    assert result["columns_added"] == ["shadow_id"]
    assert "shadow_id" in repo._state
    # the ALTER actually targeted shadow_id
    assert repo.alters and any(c == "shadow_id" for c, _ in repo.alters[0])


# 3) re-running the migration is idempotent ----------------------------------
def test_migration_idempotent():
    repo = FakeMigrationRepo(initial=dict(_ALL))
    first = repo.ensure_shadow_schema()
    second = repo.ensure_shadow_schema()
    assert first["columns_added"] == [] and second["columns_added"] == []
    # nothing to alter on an already-complete table
    assert repo.alters == [[]] * len(repo.alters) or all(a == [] for a in repo.alters)


# 4) a migration that fails to add the column is NOT masked -------------------
def test_migration_failure_raises_not_masked():
    # ALTER silently does nothing -> shadow_id still missing -> must raise.
    repo = FakeMigrationRepo(initial=_OLD_V120, apply_alter=False)
    with pytest.raises(ShadowSchemaError) as exc:
        repo.ensure_shadow_schema()
    assert "shadow_id" in str(exc.value)


def test_migration_wrong_type_raises():
    # shadow_id present but with the wrong type -> fail-fast.
    bad = dict(_ALL)
    bad["shadow_id"] = "INTEGER"
    repo = FakeMigrationRepo(initial=bad)
    with pytest.raises(ShadowSchemaError) as exc:
        repo.ensure_shadow_schema()
    assert "shadow_id" in str(exc.value)


def test_legacy_int_alias_accepted():
    # INTEGER vs INT64 / BOOLEAN vs BOOL must be treated as compatible.
    live = dict(_ALL)
    live["v2_latency_ms"] = "INTEGER"
    live["needs_manual_moderation"] = "BOOLEAN"
    repo = FakeMigrationRepo(initial=live)
    result = repo.ensure_shadow_schema()  # must NOT raise
    assert result["table_created"] is False
