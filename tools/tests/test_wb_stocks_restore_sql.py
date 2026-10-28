"""WB остатки · восстановление снимка из shadow: границы записи и отката (статически, по тексту SQL).

Живые предусловия и симуляция потребителей — в docs PR #386; здесь — то, что не должно уехать
при правке скрипта: список дат с OWNER ACK (в shadow есть и 03.09, предусловия его не отсекают),
запись и удаление только трёх таблиц остатков, удаление только строк восстановления.
"""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]
RESTORE = (ROOT / "sql" / "recovery" / "wb_stocks_restore_from_shadow_v1.sql").read_text(encoding="utf-8")
ROLLBACK = (ROOT / "sql" / "recovery" / "wb_stocks_restore_from_shadow_v1_rollback.sql").read_text(encoding="utf-8")
TABLES = {"RAW_WB_STOCKS", "RAW_WB_STOCKS_T5", "WB_STOCKS_SNAPSHOTS"}


def code(sql):
    return "\n".join(l for l in sql.splitlines() if not l.lstrip().startswith("--"))


def allowed_dates(sql):
    m = re.search(r"ASSERT p_date IN \(([^)]*)\)", code(sql))
    assert m, "нет списка дат с OWNER ACK"
    return set(re.findall(r"DATE '(\d{4}-\d{2}-\d{2})'", m.group(1)))


def test_only_acked_date_and_never_0309():
    for sql in (RESTORE, ROLLBACK):
        assert allowed_dates(sql) == {"2026-10-28"}
        assert "2026-09-03" not in code(sql)


def test_ack_guard_runs_before_any_write():
    for sql, verb in ((RESTORE, "INSERT INTO"), (ROLLBACK, "DELETE FROM")):
        body = code(sql)
        assert body.index("ASSERT p_date IN") < body.index(verb)


def test_writes_only_stock_tables_inside_one_transaction():
    for sql, verb in ((RESTORE, "INSERT INTO"), (ROLLBACK, "DELETE FROM")):
        body = code(sql)
        targets = set(re.findall(verb + r" `[^`]*\.wb_raw\.([A-Z0-9_]+)`", body))
        assert targets == TABLES
        assert not re.search(r"\b(UPDATE|MERGE|TRUNCATE|DROP|CREATE)\b", body)
        assert body.index("BEGIN TRANSACTION") < body.index(verb) < body.index("COMMIT TRANSACTION")
        assert "ROLLBACK TRANSACTION" in body


def test_restore_reads_shadow_only_and_keeps_shadow_timestamps():
    body = code(RESTORE)
    for m in re.finditer(r"INSERT INTO `[^`]*\.wb_raw\.([A-Z0-9_]+)`.*?FROM `[^`]*\.wb_raw\.([A-Z0-9_]+)`", body, re.S):
        assert m.group(2) == m.group(1) + "__CR"
    # completed_at/snapshot_ts берутся из shadow: «последний COMPLETE» по completed_at не обгонит следующий боевой снимок.
    assert "CURRENT_TIMESTAMP()" not in body.split("INSERT INTO")[3].split("error_message")[0]


def test_rollback_deletes_only_restore_rows():
    body = code(ROLLBACK)
    for m in re.finditer(r"DELETE FROM `[^`]*\.wb_raw\.([A-Z0-9_]+)`\s*WHERE([^;]*);", body):
        where = m.group(2)
        if m.group(1) == "WB_STOCKS_SNAPSHOTS":
            assert "snapshot_id = p_snapshot_id" in where and "RESTORED_FROM_SHADOW" in where
        else:
            assert "_snapshot_date = p_date" in where and "load_id = p_load_id" in where
