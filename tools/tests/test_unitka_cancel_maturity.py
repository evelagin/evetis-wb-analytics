"""Юнитка WB · классы отмен: зрелость финотчёта — исполнение настоящих вью на SQLite-фикстурах.

Аудит PR #394 (BLOCKER 1): «последний FINAL _rr_date» не доказывает покрытия. Зрелость суток заказа d — только
непрерывное покрытие [d, d + 46] окончательными недельными отчётами:
  интервал = загрузка WEEKLY типа 1 (COMPLETE, обе даты), чья неделя окончательна по типам 1 И 2;
  неделя, разрезанная границей месяца, — два интервала, нужны оба.
Пропуск, NULL, неизвестный статус или тип, половина разрезанной недели → не зрело (в классе — IMMATURE).

Из файла берутся тексты вью V_UNITKA_FINANCE_FINAL_INTERVALS и V_UNITKA_CANCEL_MATURITY; заменяются только
идентификаторы таблиц.
"""
from datetime import date, timedelta
from pathlib import Path
import re
import sqlite3

import sqlglot

ROOT = Path(__file__).resolve().parents[2]
SQL = (ROOT / "sql" / "unitka" / "cancel_class_v1.sql").read_text(encoding="utf-8")
P = "project-fa311fc0-4d87-4781-986"


def views():
    code = "\n".join(line.split("--")[0] for line in SQL.split("\n"))
    return dict(re.findall(r"CREATE OR REPLACE VIEW `[^`]+\.(\w+)` AS\n(.*?);\s*(?=\n|$)", code, re.S))


V = views()


def lite(body):
    body = (body.replace(f"`{P}.wb_raw.FINANCE_REPORT_LOADS`", "loads")
                .replace(f"`{P}.wb_raw.FINANCE_WEEK_STATUS`", "week_status")
                .replace(f"`{P}.wb_mart.FACT_ORDERS`", "fact_orders")
                .replace(f"`{P}.wb_mart.V_UNITKA_FINANCE_FINAL_INTERVALS`", "final_intervals"))
    return sqlglot.transpile(body, read="bigquery", write="sqlite")[0]


def iso(d):
    return d.isoformat()


def monday(d):
    return d - timedelta(days=d.weekday())


def weeks(first, last):
    """Понедельники недель, пересекающих [first, last]."""
    w = monday(first)
    while w <= last:
        yield w
        w += timedelta(days=7)


def registry(first, last, split_month=True):
    """Штатный реестр: каждая неделя загружена (тип 1 и 2) и окончательна; неделя через границу месяца — двумя отчётами."""
    loads, status = [], []
    for w in weeks(first, last):
        end = w + timedelta(days=6)
        parts = [(w, end)]
        if split_month and w.month != end.month:
            m = date(end.year, end.month, 1)
            parts = [(w, m - timedelta(days=1)), (m, end)]
        for f, t in parts:
            loads.append(("WEEKLY", 1, iso(f), iso(t), "COMPLETE", f"r1-{f}"))
            loads.append(("WEEKLY", 2, iso(f), iso(t), "COMPLETE", f"r2-{f}"))
        status.append((iso(w), 1, 1))
        status.append((iso(w), 2, 1))
    return loads, status


def maturity(loads, status, order_dates):
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE loads (report_period TEXT, report_type INTEGER, date_from TEXT, date_to TEXT, status TEXT, report_id TEXT)")
    db.execute("CREATE TABLE week_status (week_start TEXT, report_type INTEGER, weekly_final INTEGER)")
    db.execute("CREATE TABLE fact_orders (order_date TEXT, is_cancel INTEGER)")
    db.executemany("INSERT INTO loads VALUES (?, ?, ?, ?, ?, ?)", loads)
    db.executemany("INSERT INTO week_status VALUES (?, ?, ?)", status)
    db.executemany("INSERT INTO fact_orders VALUES (?, 1)", [(iso(d),) for d in order_dates])
    db.execute(f"CREATE VIEW final_intervals AS {lite(V['V_UNITKA_FINANCE_FINAL_INTERVALS'])}")
    rows = db.execute(f"SELECT order_date, start_covered, end_covered, no_gap FROM ({lite(V['V_UNITKA_CANCEL_MATURITY'])})").fetchall()
    out = {d: False for d in map(iso, order_dates)}
    out.update({r[0]: bool(r[1] and r[2] and r[3]) for r in rows})        # нет строки = не зрело (LEFT JOIN → IMMATURE)
    return out


D1810, D2410 = date(2026, 10, 18), date(2026, 10, 24)
FAR = date(2027, 1, 31)


def test_full_final_coverage_including_month_split_weeks_is_mature():
    loads, status = registry(date(2026, 9, 1), FAR)
    # контроль фикстуры: недели 28.09–04.10, 26.10–01.11 и 30.11–06.12 разрезаны границей месяца
    assert ("WEEKLY", 1, "2026-09-28", "2026-09-30", "COMPLETE", "r1-2026-09-28") in loads
    assert ("WEEKLY", 1, "2026-10-01", "2026-10-04", "COMPLETE", "r1-2026-10-01") in loads
    assert maturity(loads, status, [D1810, D2410]) == {"2026-10-18": True, "2026-10-24": True}


def test_horizon_not_yet_covered_is_immature():
    # окончательный финотчёт только по 30.11: горизонт 18.10 (до 03.12) не закрыт
    loads, status = registry(date(2026, 9, 1), date(2026, 11, 29))
    assert maturity(loads, status, [D1810]) == {"2026-10-18": False}


def test_missing_week_in_the_middle_is_immature_even_if_later_weeks_are_final():
    loads, status = registry(date(2026, 9, 1), FAR)
    gap = "2026-11-09"
    loads = [r for r in loads if not (r[2] >= gap and r[2] <= "2026-11-15")]     # недели 09–15.11 нет вовсе
    assert maturity(loads, status, [D1810, date(2026, 11, 20)]) == {"2026-10-18": False, "2026-11-20": True}


def test_week_status_not_final_for_one_report_type_is_immature():
    loads, status = registry(date(2026, 9, 1), FAR)
    status = [s for s in status if not (s[0] == "2026-11-16" and s[1] == 2)]      # тип 2 недели 16.11 не окончателен
    assert maturity(loads, status, [D1810]) == {"2026-10-18": False}


def test_null_weekly_final_and_null_dates_are_never_final():
    loads, status = registry(date(2026, 9, 1), FAR)
    status_null = [(w, t, None if w == "2026-11-02" and t == 1 else f) for w, t, f in status]
    assert maturity(loads, status_null, [D1810]) == {"2026-10-18": False}
    loads_null = [(p, t, f, None if f == "2026-11-02" and t == 1 else to, s, r) for p, t, f, to, s, r in loads]
    assert maturity(loads_null, status, [D1810]) == {"2026-10-18": False}


def test_unknown_status_or_daily_layer_does_not_cover():
    loads, status = registry(date(2026, 9, 1), FAR)
    discovered = [(p, t, f, to, "DISCOVERED" if f == "2026-11-02" else s, r) for p, t, f, to, s, r in loads]
    assert maturity(discovered, status, [D1810]) == {"2026-10-18": False}
    daily = [("DAILY" if f == "2026-11-02" else p, t, f, to, s, r) for p, t, f, to, s, r in loads]
    assert maturity(daily, status, [D1810]) == {"2026-10-18": False}


def test_month_split_week_with_one_half_missing_is_immature():
    loads, status = registry(date(2026, 9, 1), FAR)
    # неделя 30.11–06.12 разрезана: 30.11 и 01–06.12. Нет загрузки 01–06.12 — горизонт 24.10 (до 09.12) не покрыт,
    # хотя статус недели окончателен и предыдущая половина загружена
    no_half = [r for r in loads if not (r[2] == "2026-12-01")]
    assert maturity(no_half, status, [D1810, D2410]) == {"2026-10-18": False, "2026-10-24": False}
    # горизонт 18.10 (до 03.12) тоже заходит во вторую половину; с полной неделей оба зрелы
    assert maturity(loads, status, [D1810, D2410]) == {"2026-10-18": True, "2026-10-24": True}


def test_empty_registry_yields_no_maturity():
    assert maturity([], [], [D1810]) == {"2026-10-18": False}


def test_class_view_treats_missing_or_null_maturity_as_immature():
    d = re.sub(r"\s+", " ", V["V_UNITKA_CANCEL_CLASS_DAILY"])
    assert "IFNULL(start_covered AND end_covered AND no_gap, FALSE) AS mature" in d
    assert "LEFT JOIN mt ON mt.d = a.d" in d
    assert "WHEN NOT IFNULL(mt.mature, FALSE) THEN 'IMMATURE'" in d
    assert "_rr_date" not in d                                                    # прежнее правило удалено
    assert "DATE_ADD(order_date, INTERVAL 46 DAY)" in re.sub(r"\s+", " ", V["V_UNITKA_CANCEL_MATURITY"])


# ─── Детектор V_UNITKA_CANCEL_SHEET_DRIFT (аудит PR #394, п. 2) ──────────────────────────────────────────────────
def drift(snapshot, classes, window_from="2026-11-09"):
    body = (V["V_UNITKA_CANCEL_SHEET_DRIFT"]
            .replace(f"`{P}.wb_ops.UNITKA_SKU_COMPONENTS_DAILY`", "snap_tbl")
            .replace(f"`{P}.wb_mart.V_UNITKA_RECON_WINDOW`", "recon_window")
            .replace(f"`{P}.wb_mart.V_UNITKA_CANCEL_CLASS_DAILY`", "class_tbl"))
    sql = sqlglot.transpile(body, read="bigquery", write="sqlite")[0]
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE snap_tbl (snapshot_id TEXT, lcd TEXT, date_msk TEXT, nm_id INTEGER, cancels REAL, void_cancels INTEGER)")
    db.execute("CREATE TABLE recon_window (window_from TEXT)")
    db.execute("CREATE TABLE class_tbl (nm_id INTEGER, date_msk TEXT, cancel_class_status TEXT, legacy_cancels INTEGER,"
               " effective_cancels INTEGER, effective_void_qty INTEGER, sheet_change_required INTEGER)")
    db.executemany("INSERT INTO snap_tbl VALUES ('s2', '2026-12-13', ?, ?, ?, ?)", snapshot)
    db.execute("INSERT INTO recon_window VALUES (?)", (window_from,))
    db.executemany("INSERT INTO class_tbl VALUES (?, ?, ?, ?, ?, ?, ?)", classes)
    cur = db.execute(f"SELECT nm_id, date_msk, drift_status FROM ({sql}) ORDER BY date_msk, nm_id")
    return {(r[0], r[1]): r[2] for r in cur.fetchall()}


A, B, C = 535580776, 252442517, 952068582


def test_drift_detector_pending_ok_and_late_logistics_after_restore():
    classes = [
        (A, "2026-10-18", "FINAL", 0, 1, 1, 1),               # 18.10: отмена без логистики в Q — S 1, V 1
        (B, "2026-10-24", "FINAL", 1, 0, 0, 1),               # 24.10: отмена вне Q — S 0
        (C, "2026-10-27", "IMMATURE", 1, 1, 0, 0),            # кандидат: незрел, лист прежний
    ]
    before = [("2026-10-18", A, 0, 0), ("2026-10-24", B, 1, 0), ("2026-10-27", C, 1, 0)]
    assert drift(before, classes) == {(A, "2026-10-18"): "PENDING_RESTORE", (B, "2026-10-24"): "PENDING_RESTORE", (C, "2026-10-27"): "OK"}
    restored = [("2026-10-18", A, 1, 1), ("2026-10-24", B, 0, 0), ("2026-10-27", C, 1, 0)]
    assert set(drift(restored, classes).values()) == {"OK"}
    # поздняя логистика: 18.10 теперь «с логистикой в Q» (S 1, V 0); 24.10 получил плечи вне Q — ANOMALY и
    # sheet_change_required = FALSE; восстановленный лист с обоими расходится — детектор их видит
    late = [(A, "2026-10-18", "FINAL", 0, 1, 0, 1), (B, "2026-10-24", "ANOMALY_ABSENT_WITH_LOGISTICS", 1, 1, 0, 0), classes[2]]
    assert drift(restored, late) == {(A, "2026-10-18"): "RESTORED_DRIFT", (B, "2026-10-24"): "RESTORED_DRIFT", (C, "2026-10-27"): "OK"}


def test_drift_detector_missing_snapshot_row_maturity_regression_orphan_v_and_window():
    restored = [("2026-10-18", A, 1, 1)]
    # реестр финотчёта откатился: класс снова IMMATURE (прежний S 0, V 0), а лист восстановлен — расхождение
    assert drift(restored, [(A, "2026-10-18", "IMMATURE", 0, 0, 0, 0)]) == {(A, "2026-10-18"): "RESTORED_DRIFT"}
    # нет строки снимка (все величины суток нулевые) = на листе S 0, V 0: против ANOMALY с прежним S 1 — расхождение
    other = [("2026-10-25", B, 0, 0)]
    assert drift(other, [(B, "2026-10-24", "ANOMALY_ABSENT_WITH_LOGISTICS", 1, 1, 0, 0)])[(B, "2026-10-24")] == "RESTORED_DRIFT"
    # снимка нет вовсе — fail-closed: сутки класса всё равно судятся (лист 0/0), молчаливого «расхождений нет» не бывает
    assert drift([], [(B, "2026-10-24", "ANOMALY_ABSENT_WITH_LOGISTICS", 1, 1, 0, 0)]) == {(B, "2026-10-24"): "RESTORED_DRIFT"}
    # поправка V на листе без строки класса — расхождение
    assert drift([("2026-10-20", 909951444, 1, 1)], []) == {(909951444, "2026-10-20"): "RESTORED_DRIFT"}
    # сутки окна сверки ведёт писатель: детектор их не судит
    assert drift([("2026-10-18", A, 2, 0)], [(A, "2026-10-18", "IMMATURE", 0, 0, 0, 0)], window_from="2026-10-10") == {}
