#!/usr/bin/env python3
"""EVETIS OPERATIONS (Stage C1) — сверка книги с evetis_ops по критериям приёмки владельца.

Читает собранный .xlsx и заново спрашивает BigQuery: числа в книге обязаны совпадать с журналом
и представлениями. Печатает PASS/FAIL по каждому пункту приёмки (1–9).

Запуск: .venv/bin/python tools/ops_sheet_verify.py <путь к .xlsx>
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ops_deploy as d  # noqa: E402
from ops_sheet_build import query  # noqa: E402
from openpyxl import load_workbook  # noqa: E402

P = d.P


def sheet_rows(ws, header_row, key_col=2):
    """Строки таблицы листа: от строки после заголовка до пустой строки / ИТОГО."""
    out, r = [], header_row + 1
    while True:
        first = ws.cell(r, 1).value
        if first is None or str(first).startswith('ИТОГО'):
            break
        out.append({'row': r, 'name': first, 'key': ws.cell(r, key_col).value,
                    'vals': [ws.cell(r, c).value for c in range(1, ws.max_column + 1)]})
        r += 1
    return out


def find_header(ws, first_header):
    for r in range(1, ws.max_row + 1):
        if ws.cell(r, 1).value == first_header:
            return r
    raise SystemExit(f'заголовок {first_header} не найден')


def main(path):
    wb = load_workbook(path, data_only=True)
    checks = []

    def chk(n, name, ok, detail):
        checks.append((n, name, ok, detail))

    # --- данные из BigQuery (заново) ---
    plan = {(r['channel'], r['card_sku']): r for r in query(f'SELECT * FROM `{P}.evetis_ops.V_OPS_SUPPLY_PLAN`')}
    ff_bq = {r['internal_sku']: r for r in query(f'SELECT * FROM `{P}.evetis_ops.V_OPS_FF_STOCK_SHEET`')}
    pick_bq = {r['internal_sku']: r for r in query(f'SELECT * FROM `{P}.evetis_ops.V_OPS_PICK_FROM_STORAGE`')}
    bal = query(f'''SELECT SUM(IF(on_ff, units, 0)) AS ff_total,
      SUM(IF(state = 'AVAILABLE' AND location = 'PALLET', units, 0)) AS pallet,
      SUM(IF(state = 'AVAILABLE' AND location = 'SHELF', units, 0)) AS shelf,
      SUM(IF(state = 'RESERVED' AND channel = 'OZON', units, 0)) AS res_ozon,
      SUM(IF(state = 'RESERVED' AND channel = 'WB', units, 0)) AS res_wb,
      SUM(IF(state = 'ASSEMBLED', units, 0)) AS assembled, SUM(IF(state = 'FBS_READY', units, 0)) AS fbs
      FROM `{P}.evetis_ops.V_CT_STOCK_BALANCE`''')[0]
    st = query(f'''SELECT SUM(IF(side = 'MARKETPLACE' AND channel = 'OZON', units, 0)) AS on_ozon,
      SUM(IF(side = 'PIPELINE' AND state = 'IN_ACCEPTANCE', units, 0)) AS in_acceptance
      FROM `{P}.evetis_ops.V_CT_STOCK_STATE`''')[0]

    # --- 1 и 2: ФФ в книге = журнал ---
    ws = wb['03_FF_STOCK']
    hdr = find_header(ws, 'Товар')
    cols = {ws.cell(hdr, c).value: c for c in range(1, ws.max_column + 1)}
    rows = sheet_rows(ws, hdr)
    tot_row = hdr + len(rows) + 1
    sheet_ff_total = ws.cell(tot_row, cols['TOTAL PHYSICAL']).value
    chk(1, 'Итог ФФ в книге = 32 948 физических единиц журнала',
        sheet_ff_total == bal['ff_total'] == 32948, f'книга {sheet_ff_total} · журнал {bal["ff_total"]}')
    per_sku_ok, bad = True, []
    for r in rows:
        sku = r['key']
        b = ff_bq.get(sku)
        for label, key in (('PALLET FREE', 'pallet_free'), ('SHELF FREE', 'shelf_free'),
                           ('RESERVED OZON', 'reserved_ozon'), ('RESERVED WB', 'reserved_wb'),
                           ('ASSEMBLED', 'assembled'), ('FBS READY', 'fbs_ready'),
                           ('SHIPPED\nUNCONFIRMED', 'shipped_unconfirmed'), ('TOTAL PHYSICAL', 'total_physical')):
            if (ws.cell(r['row'], cols[label]).value or 0) != (b[key] or 0):
                per_sku_ok = False
                bad.append(f'{sku}/{label}')
    totals_ok = all(ws.cell(tot_row, cols[l]).value == v for l, v in (
        ('PALLET FREE', bal['pallet']), ('SHELF FREE', bal['shelf']), ('RESERVED OZON', bal['res_ozon']),
        ('RESERVED WB', bal['res_wb']), ('ASSEMBLED', bal['assembled']), ('FBS READY', bal['fbs'])))
    chk(2, 'Состояния по SKU и итоги совпадают с evetis_ops',
        per_sku_ok and totals_ok, f'{len(rows)} SKU, расхождений {len(bad)}; итоги по состояниям '
        f'{"совпали" if totals_ok else "разошлись"}')

    # --- 3 и 4: Ozon 314 не в остатке площадки; приёмка отдельно ---
    on_ozon_sheet = ws.cell(tot_row, cols['ON_OZON']).value
    acc_sheet = ws.cell(tot_row, cols['IN_ACCEPTANCE\n(Ozon)']).value
    res_ozon_sheet = ws.cell(tot_row, cols['RESERVED OZON']).value
    chk(3, 'Резерв Ozon на ФФ (314) не посчитан как остаток площадки',
        res_ozon_sheet == bal['res_ozon'] and bal['res_ozon'] > 0 and on_ozon_sheet == st['on_ozon']
        and on_ozon_sheet != res_ozon_sheet,
        f'RESERVED OZON {res_ozon_sheet} · ON_OZON {on_ozon_sheet} (API) — разные колонки и разные числа')
    ws_plan = wb['01_SUPPLY_PLAN']
    ws_calc = wb['05_РАСЧЁТ']
    acc_col_present = any(c.value == 'На приёмке\nOzon' for row in ws_calc.iter_rows() for c in row)
    acc_separate = cols['IN_ACCEPTANCE\n(Ozon)'] != cols['ON_OZON']
    chk(4, 'IN_ACCEPTANCE показана отдельной колонкой и не входит в остаток площадки',
        acc_col_present and acc_separate and acc_sheet == st['in_acceptance'],
        f'колонка «На приёмке Ozon» есть в плане, в листе ФФ — отдельно от ON_OZON · '
        f'итог приёмки {acc_sheet} ед. = журнал {st["in_acceptance"]} '
        f'(сегодня 0: поставки 03.09 приняты Ozon ночью)')

    # --- 5: мощность наборов учитывает общие компоненты ---
    bundles = {r['bundle_sku']: r for r in query(f'SELECT * FROM `{P}.evetis_ops.V_OPS_BUNDLE_PRODUCTION`')}
    shared = [b for b in bundles.values() if b['limiting_component_now'] == 'EVT-FC-MOIST-50']
    cap_sum = sum(b['capacity_alone'] or 0 for b in shared)
    free_moist = pick_bq['EVT-FC-MOIST-50']['shelf_free'] + max(pick_bq['EVT-FC-MOIST-50']['pallet_free'], 0)
    chk(5, 'Мощность наборов не складывается: общий компонент ограничивает сумму',
        len(shared) >= 3 and cap_sum > free_moist,
        f'{len(shared)} набора с общим компонентом FC-MOIST-50: сумма мощностей {cap_sum} > свободного {free_moist}')

    # --- 6: общий пул ФФ: WB и Ozon не делят одну единицу ---
    over = query(f'''WITH dem AS (
        SELECT b.component_sku, SUM(p.rec_final * b.component_qty) AS demand
        FROM `{P}.evetis_ops.V_OPS_SUPPLY_PLAN` p JOIN `{P}.evetis_ops.V_OPS_BOM` b ON b.card_sku = p.card_sku GROUP BY 1)
      SELECT COUNTIF(d.demand > k.shelf_free + GREATEST(k.pallet_free, 0)) AS over_components,
             SUM(d.demand) AS total_demand
      FROM dem d JOIN `{P}.evetis_ops.V_OPS_PICK_FROM_STORAGE` k ON k.internal_sku = d.component_sku''')[0]
    chk(6, 'Рекомендации WB и Ozon вместе умещаются в свободный запас ФФ',
        over['over_components'] == 0,
        f'компонентов с перебором: {over["over_components"]} · всего физических единиц в плане {over["total_demand"]}')

    # --- 7: снятие с паллет сходится с рекомендациями ---
    ws_p = wb['05_РАСЧЁТ']
    hdr_pick = find_header(ws_p, 'Компонент')
    pcols = {ws_p.cell(hdr_pick, c).value: c for c in range(1, ws_p.max_column + 1)}
    prows = sheet_rows(ws_p, hdr_pick)
    bad7 = []
    for r in prows:
        b = pick_bq[r['key']]
        total = (ws_p.cell(r['row'], pcols['ИТОГО\nспрос']).value or 0)
        parts = sum(ws_p.cell(r['row'], pcols[k]).value or 0 for k in ('Соло WB', 'Соло Ozon', 'Компоненты\nнаборов', 'FBS'))
        pick = ws_p.cell(r['row'], pcols['СНЯТЬ\nС ПАЛЛЕТ']).value or 0
        shelf_after = ws_p.cell(r['row'], pcols['Полка\nпосле']).value or 0
        if total != parts or total != b['total_physical_demand'] or pick != b['to_pick_from_pallet'] \
           or shelf_after != b['shelf_after_pick']:
            bad7.append(r['key'])
    chk(7, 'Снятие с паллет = соло WB + соло Ozon + компоненты наборов + FBS',
        not bad7, f'{len(prows)} компонентов сошлись' if not bad7 else f'расхождения: {bad7}')

    # --- 8: нет отрицательных остатков ---
    neg = query(f'''SELECT COUNTIF(shelf_after_pick < 0) + COUNTIF(pallet_after_pick < 0)
      + COUNTIF(free_after_plan < 0) + COUNTIF(shortage > 0) AS bad FROM `{P}.evetis_ops.V_OPS_PICK_FROM_STORAGE`''')[0]['bad']
    neg_sheet = sum(1 for r in prows if (ws_p.cell(r['row'], pcols['Полка\nпосле']).value or 0) < 0
                    or (ws_p.cell(r['row'], pcols['Паллеты\nпосле']).value or 0) < 0)
    chk(8, 'Ни одного отрицательного остатка после плана', neg == 0 and neg_sheet == 0,
        f'BigQuery {neg} · книга {neg_sheet}')

    # --- 9: в книге нет формул ---
    formulas = [(ws.title, c.coordinate) for ws in wb.worksheets for row in ws.iter_rows()
                for c in row if isinstance(c.value, str) and c.value.startswith('=')]
    chk(9, 'В книге нет формул: все числа посчитаны в BigQuery', not formulas,
        f'формул: {len(formulas)}; листов: {len(wb.worksheets)} (DATA скрыт: '
        f'{wb["DATA"].sheet_state == "hidden"})')

    # --- строки плана: значения книги = представление (только таблицы WB и OZON) ---
    plan_headers = [r for r in range(1, ws_p.max_row + 1)
                    if ws_p.cell(r, 1).value == 'Товар' and ws_p.cell(r, 3).value == 'На площадке\nсейчас']
    cols_p = {ws_p.cell(plan_headers[0], c).value: c for c in range(1, ws_p.max_column + 1)}
    checked, mism = 0, []
    for hdr_i, hdr_r in enumerate(plan_headers):
        channel = 'WB' if hdr_i == 0 else 'OZON'
        for r in sheet_rows(ws_p, hdr_r):
            rec = plan.get((channel, r['key']))
            if rec is None:
                mism.append((channel, r['key'], 'нет в представлении'))
                continue
            for label, key in (('РЕКОМЕНДАЦИЯ,\nпозиций', 'rec_final'), ('в физических\nединицах', 'rec_physical_units'),
                               ('Потребность', 'need_math'),
                               ('Цель\nк прибытию', 'target_at_arrival'), ('Прогноз\nк прибытию', 'projected_at_arrival'),
                               ('На площадке\nсейчас', 'on_marketplace'), ('Итого\nвходящее', 'committed_inbound'),
                               ('На приёмке\nOzon', 'in_acceptance'), ('Резерв\nна ФФ', 'reserved_on_ff')):
                if (ws_p.cell(r['row'], cols_p[label]).value or 0) != (rec[key] or 0):
                    mism.append((channel, r['key'], label))
            checked += 1
    chk(10, 'Каждая строка плана в книге = строке представления (рекомендация, цель, прогноз, входящее)',
        checked == len(plan) and not mism,
        f'сверено строк: {checked} из {len(plan)}' + ('' if not mism else f'; расхождения: {mism[:6]}'))

    # --- C1.1: терминология, верхние блоки, ТЗ для ФФ, флаг агрессивного плана ---
    owner_sheets = [w for w in wb.worksheets if w.title != 'DATA']
    bad_terms = [f'{w.title}!{c.coordinate}' for w in owner_sheets for row in w.iter_rows() for c in row
                 if isinstance(c.value, str) and 'карточ' in c.value.lower()]
    chk(11, 'Слово «карточки» убрано из листов владельца (позиции / физические единицы)',
        not bad_terms, f'вхождений: {len(bad_terms)}' + ('' if not bad_terms else f' — {bad_terms[:5]}'))

    ws_o = wb['01_SUPPLY_PLAN']
    bands = [str(ws_o.cell(r, 1).value) for r in range(1, 60) if ws_o.cell(r, 1).value]
    need_blocks = ['ИТОГО СЕГОДНЯ · ГОТОВОЕ ЗАДАНИЕ USEND', 'ОТГРУЗИТЬ WB СЕЙЧАС', 'ОТГРУЗИТЬ OZON СЕЙЧАС',
                   'ПРОВЕРИТЬ / НЕ ОТГРУЖАТЬ']
    missing = [b for b in need_blocks if not any(t.startswith(b) for t in bands)]
    task_row = next((r for r in range(1, ws_o.max_row + 1)
                     if str(ws_o.cell(r, 1).value or '').startswith('ТЗ ДЛЯ ФУЛФИЛМЕНТА')), None)
    heights = [(ws_o.row_dimensions[r].height or 21) for r in range(1, 60)]
    fold = next(r for r in range(1, 60) if sum(heights[:r]) > 715)
    first_screen = [b for b in need_blocks
                    if any(str(ws_o.cell(r, 1).value or '').startswith(b) for r in range(1, fold))]
    chk(12, 'Все блоки владельца и задание Usend умещаются на первом экране 1440×900',
        not missing and len(first_screen) == len(need_blocks) and task_row,
        f'на первом экране (до строки {fold}, ~715 px): {len(first_screen)} из {len(need_blocks)} блоков · '
        f'полное ТЗ — строка {task_row} · подробный расчёт — отдельный лист 05_РАСЧЁТ')

    task_bq = {r['internal_sku']: r for r in query(f'SELECT * FROM `{P}.evetis_ops.V_OPS_FF_TASK`')}
    hdr_move = next(r for r in range(task_row, ws_o.max_row + 1)
                    if str(ws_o.cell(r, 3).value or '').startswith('Снять под новые'))
    mcols = {str(ws_o.cell(hdr_move, c).value): c for c in range(1, ws_o.max_column + 1)}
    col_total = next(k for k in mcols if k.startswith('ИТОГО снять'))
    col_res = next(k for k in mcols if k.startswith('Снять под уже'))
    move_rows = sheet_rows(ws_o, hdr_move)
    bad_task = [r['key'] for r in move_rows
                if (ws_o.cell(r['row'], mcols[col_total]).value or 0) != task_bq[r['key']]['move_pallet_to_shelf_total']
                or (ws_o.cell(r['row'], mcols[col_res]).value or 0) != task_bq[r['key']]['move_pallet_to_shelf_reserved']]
    phys_plan = query(f'SELECT SUM(rec_physical_units) AS s FROM `{P}.evetis_ops.V_OPS_SUPPLY_PLAN`')[0]['s']
    phys_pick = query(f'SELECT SUM(total_physical_demand) AS s FROM `{P}.evetis_ops.V_OPS_PICK_FROM_STORAGE`')[0]['s']
    chk(13, 'ТЗ для ФФ сходится с планом: снятие с паллет и физические единицы',
        not bad_task and phys_plan == phys_pick,
        f'строк снятия сверено: {len(move_rows)} · рекомендации {phys_plan} физ. ед. = расход компонентов {phys_pick}')

    agg = query('SELECT COUNTIF(aggressive_plan) AS flagged, COUNT(*) AS all_rows, '
                'COUNTIF(aggressive_plan AND rec_final > 0) AS flagged_ship '
                f'FROM `{P}.evetis_ops.V_OPS_SUPPLY_PLAN`')[0]
    warn_cells = sum(1 for row in ws_calc.iter_rows() for c in row
                     if isinstance(c.value, str) and c.value.startswith('АГРЕССИВНЫЙ ПЛАН'))
    chk(14, 'Агрессивный план (>2× факта) помечен, количество не изменено',
        warn_cells >= agg['flagged_ship'] and agg['flagged'] > 0,
        f'строк с флагом в BigQuery: {agg["flagged"]} из {agg["all_rows"]} · предупреждений в книге: {warn_cells}')

    plan_rows = list(plan.values())
    long_reasons = [r['card_sku'] for r in plan_rows if len(r['short_reason'] or '') > 80]
    risk_ok = all((r['risk_label'] or '') in ('HIGH CONFIDENCE', 'PLAN-DRIVEN', 'REVIEW', '—') for r in plan_rows)
    chk(15, 'Короткая причина ≤ 80 знаков, индикатор риска у каждой строки',
        not long_reasons and risk_ok,
        f'максимум {max(len(r["short_reason"] or "") for r in plan_rows)} знаков · '
        f'метки: ' + ', '.join(sorted({r['risk_label'] for r in plan_rows})))

    hdr_rows = [r for r in range(1, 60) if str(ws_o.cell(r, 4).value or '') == 'Одобрено\nвладельцем']
    appr_col, model_col = 4, 2
    # строки блоков отгрузки: их видно по метке риска в колонке G
    data_rows = [r for r in range(1, 60)
                 if str(ws_o.cell(r, 7).value or '') in ('HIGH CONFIDENCE', 'PLAN-DRIVEN', 'REVIEW', '—')]
    appr_filled = [r for r in data_rows if ws_o.cell(r, appr_col).value not in (None, '')]
    model_vals = [ws_o.cell(r, model_col).value for r in data_rows]
    chk(16, '«Рекомендация модели» и «Одобрено владельцем» — разные колонки; вторая пустая',
        len(hdr_rows) == 2 and not appr_filled and len(model_vals) >= 20,
        f'строк с рекомендацией модели: {len(model_vals)} · заполненных ячеек владельца: {len(appr_filled)} '
        f'(поле read-only placeholder, система его не читает)')

    usend_labels = [str(ws_o.cell(r, 1).value) for r in range(4, 11)]
    need_lines = ['Снять с паллет', 'Собрать наборы', 'Подготовить к отгрузке: WILDBERRIES',
                  'Подготовить к отгрузке: OZON', 'Уже зарезервировано', 'Останется свободно']
    miss_lines = [n for n in need_lines if not any(str(x).startswith(n) for x in usend_labels)]
    chk(17, 'ГОТОВОЕ ЗАДАНИЕ USEND содержит все требуемые строки', not miss_lines,
        'строки: ' + ' · '.join(x for x in usend_labels if x and x != 'Что сделать'))

    print('\n' + '=' * 108)
    for n, name, ok, detail in checks:
        print(f'{n}. {"PASS" if ok else "FAIL"}  {name}\n      {detail}')
    print('=' * 108)
    return 0 if all(c[2] for c in checks) else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else 'EVETIS_OPERATIONS.xlsx'))
