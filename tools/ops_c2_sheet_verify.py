#!/usr/bin/env python3
"""EVETIS OPERATIONS · C2 — проверка живой книги по экспорту .xlsx (только чтение).

  verify  AFTER.xlsx [--before C12.xlsx]   контракт раскладки, оформление, значения против BigQuery, решения владельца
  same    A.xlsx B.xlsx                    две выгрузки совпадают (значения, объединения, заметки) — «рабочая книга не менялась»

Экспорт книги: Drive API files.export (xlsx). Нужен openpyxl.
Проверки verify:
  L  раскладка: маркеры системных колонок (@L … @END), колонки скрыты, скрытые листы DATA и _OPS_STATE
  F  оформление C1.2 сохранено: ширины сетки, закрепление, шапки (текст, заливка, заметки), полосы, стиль колонки владельца
  V  значения сгенерированных блоков = V_OPS_SHEET_* сейчас (01 отгрузка/Usend, 03 итог, 05 WB/Ozon, DATA)
  O  «Одобрено владельцем» = состояние _OPS_STATE по каноническому ключу строки
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import openpyxl  # noqa: E402

SYS = {'01_SUPPLY_PLAN': 9, '02_SHIPMENTS': 18, '03_FF_STOCK': 15, '04_SETTINGS': 14, '05_РАСЧЁТ': 26, 'DATA': 1}
GRID = {'01_SUPPLY_PLAN': 8, '02_SHIPMENTS': 17, '03_FF_STOCK': 14, '04_SETTINGS': 13, '05_РАСЧЁТ': 25}
FREEZE = {'01_SUPPLY_PLAN': 'A4', '02_SHIPMENTS': 'C6', '03_FF_STOCK': 'C8', '04_SETTINGS': None, '05_РАСЧЁТ': 'C6'}
REPLACED_NOTES = {  # заметки, заменённые установкой C2 (снимочные утверждения C1.2)
    ('01_SUPPLY_PLAN', 'WB_SHIP', 4), ('01_SUPPLY_PLAN', 'OZON_SHIP', 4), ('01_SUPPLY_PLAN', 'TASK_MOVE', 4),
    ('05_РАСЧЁТ', 'CALC_WB', 9), ('05_РАСЧЁТ', 'CALC_WB', 13), ('05_РАСЧЁТ', 'CALC_WB', 14), ('05_РАСЧЁТ', 'CALC_WB', 21),
    ('05_РАСЧЁТ', 'CALC_OZON', 9), ('05_РАСЧЁТ', 'CALC_OZON', 13), ('05_РАСЧЁТ', 'CALC_OZON', 14), ('05_РАСЧЁТ', 'CALC_OZON', 21),
    ('05_РАСЧЁТ', 'CALC_BUNDLES', 10), ('05_РАСЧЁТ', 'CALC_PICK', 11)}
RESULTS = []


def check(code, name, ok, detail=''):
    RESULTS.append((code, name, bool(ok), detail))
    print(f'{"PASS" if ok else "FAIL"} {code} {name}' + (f' · {detail}' if detail else ''))


def fill(c):
    f = c.fill
    return '' if f is None or f.fill_type is None or f.fgColor is None or f.fgColor.type != 'rgb' else str(f.fgColor.rgb)[-6:].upper()


def qty(x):
    """Количество из выгрузки: 777.0 → 777 (openpyxl читает целые числа листа как float)."""
    return int(x) if isinstance(x, float) and x.is_integer() else x


def markers(ws, col):
    out = []
    for r in range(1, ws.max_row + 1):
        v = ws.cell(r, col).value
        out.append('' if v is None else str(v))
        if v == '@END':
            break
    return out


def headers_by_block(ws, marks):
    """{block: row} для @H и @B; {row: marker}."""
    h, b = {}, {}
    for i, m in enumerate(marks, start=1):
        if m.startswith('@H:'):
            h[m[3:]] = i
        elif m.startswith('@B:'):
            b[m[3:]] = i
    return h, b


def rows_of(marks, block):
    return [(i, m.split(':', 2)[2] if m.count(':') >= 2 else '') for i, m in enumerate(marks, start=1)
            if m.startswith('@R:' + block + ':') or m == '@E:' + block]


def c12_header_rows(ws):
    """Строки шапок/полос C1.2 по тексту колонки A (для сравнения с установленной книгой)."""
    out = []
    for r in range(1, ws.max_row + 1):
        c = ws.cell(r, 1)
        if fill(c) in ('434343', '4A148C', '0D47A1', 'A61C00', '274E13', '7F6000'):
            out.append(r)
    return out


def verify(after_path, before_path=None):
    wb = openpyxl.load_workbook(after_path)
    # ── L: раскладка
    check('L1', 'листы на месте, DATA и _OPS_STATE скрыты, остальные видимы',
          [ws.title for ws in wb.worksheets][:6] == list(SYS)[:5] + ['DATA'] and wb['DATA'].sheet_state == 'hidden'
          and '_OPS_STATE' in wb.sheetnames and wb['_OPS_STATE'].sheet_state == 'hidden'
          and all(wb[s].sheet_state == 'visible' for s in GRID),
          ', '.join(f'{ws.title}:{ws.sheet_state}' for ws in wb.worksheets))
    M = {}
    for s, col in SYS.items():
        ws = wb[s]
        M[s] = markers(ws, col)
        letter = openpyxl.utils.get_column_letter(col)
        check('L2', f'{s}: маркеры @L:C2L1 … @END, служебная колонка {letter} скрыта',
              M[s] and M[s][0] == '@L:C2L1' and M[s][-1] == '@END' and ws.column_dimensions[letter].hidden,
              f'{len(M[s]) - 1} строк раскладки')
        unmarked = [i for i, m in enumerate(M[s], start=1) if m == '']
        check('L3', f'{s}: у каждой строки раскладки есть маркер', not unmarked, str(unmarked[:5]))

    # ── F: оформление
    for s, freeze in FREEZE.items():
        check('F1', f'{s}: закрепление {freeze}', wb[s].freeze_panes == freeze, str(wb[s].freeze_panes))
    if before_path:
        bw = openpyxl.load_workbook(before_path)
        for s, n in GRID.items():
            wa = [round(wb[s].column_dimensions[openpyxl.utils.get_column_letter(c)].width or 0, 1) for c in range(1, n + 1)]
            wbf = [round(bw[s].column_dimensions[openpyxl.utils.get_column_letter(c)].width or 0, 1) for c in range(1, n + 1)]
            check('F2', f'{s}: ширины колонок сетки как в C1.2', wa == wbf, '' if wa == wbf else f'{wa} ≠ {wbf}')
            # шапки и полосы: одинаковая последовательность (текст A, заливки сетки, заметки кроме заменённых)
            ha, hb = [], []
            h_after, b_after = headers_by_block(wb[s], M[s])
            block_of_row = {r: blk for blk, r in h_after.items()}
            for r in c12_header_rows(wb[s]):
                cells = [wb[s].cell(r, c) for c in range(1, n + 1)]
                blk = block_of_row.get(r)
                notes = tuple('<заменено>' if (s, blk, c) in REPLACED_NOTES else (cells[c - 1].comment.text if cells[c - 1].comment else '')
                              for c in range(1, n + 1))
                text = '' if r in b_after.values() else cells[0].value   # текст полос генерируется
                ha.append((text, tuple(fill(x) for x in cells), notes if blk else ()))
            for r in c12_header_rows(bw[s]):
                cells = [bw[s].cell(r, c) for c in range(1, n + 1)]
                ha_blk = None
                hb.append((cells[0].value, tuple(fill(x) for x in cells), None))
            # сравнение заливок и текстов шапок (полосы — только заливки)
            fa = [(x[1]) for x in ha]
            fb = [(x[1]) for x in hb]
            extra = len(fa) - len(fb)
            check('F3', f'{s}: заливки шапок и полос C1.2 сохранены (+{extra} новых полос C2)',
                  extra in (0, 1) and all(x in fa for x in fb), '')
        ws, bws = wb['01_SUPPLY_PLAN'], bw['01_SUPPLY_PLAN']
        owner_style = set()
        for blk in ('WB_SHIP', 'OZON_SHIP'):
            for r, _ in rows_of(M['01_SUPPLY_PLAN'], blk):
                c = ws.cell(r, 4)
                owner_style.add((fill(c), c.border.left.style, c.border.right.style))
        # эталон — ячейки владельца в выгрузке C1.2 (Google экспортирует пунктир C1.2 как 'dotted')
        want_style = {(fill(bws.cell(r, 4)), bws.cell(r, 4).border.left.style, bws.cell(r, 4).border.right.style)
                      for r in range(1, bws.max_row + 1) if fill(bws.cell(r, 4)) == 'FFF9E6'}
        check('F4', '01: колонка «Одобрено владельцем» сохранила стиль владельца C1.2 (в том числе новые строки)',
              owner_style == want_style and len(want_style) == 1, f'{owner_style} / эталон {want_style}')
        hb_notes = {k: v for k, v in ((c.coordinate, c.comment.text) for row in bws.iter_rows() for c in row if c.comment)}
        ha_notes = {k: v for k, v in ((c.coordinate, c.comment.text) for row in ws.iter_rows() for c in row if c.comment)}
        check('F5', '01: число заметок не уменьшилось', len(ha_notes) >= len(hb_notes), f'{len(hb_notes)} → {len(ha_notes)}')
        # тексты интерфейса: заметки, шапки, полосы, статичные и сгенерированные строки-тексты; строки данных из BigQuery
        # (@R блоков CONFIG/LOGISTICS/… и DATA) не проверяются — это исходные данные, а не утверждения C1.2
        def ui_cells(s):
            for i, m in enumerate(M[s], start=1):
                if m.startswith('@R:') and m.split(':')[1] not in ('USEND_TOP', 'USEND_TASK', 'RULES', 'REFRESH_STATUS', 'FRESHNESS'):
                    continue
                for c in range(1, GRID[s] + 1):
                    yield wb[s].cell(i, c)
        text_blob = ' '.join(str(c.value) for s in GRID for c in ui_cells(s) if isinstance(c.value, str))
        text_blob += ' '.join(c.comment.text for s in GRID for row in wb[s].iter_rows() for c in row if c.comment)
        stale = [t for t in ('07.09', 'ТОЛЬКО ЧТЕНИЕ', '22.07.2026', 'на 09.09', 'очищается', 'WB 7, Ozon 10', 'WB 66, Ozon 69')
                 if t in text_blob]
        check('F6', 'снимочные тексты C1.2 убраны', not stale, str(stale))

    # ── V: значения против BigQuery
    import ops_sheet_build as b  # noqa: E402  (запросы BigQuery)
    plan = b.query(f'SELECT * FROM `{b.P}.evetis_ops.V_OPS_SHEET_PLAN`')
    ws = wb['01_SUPPLY_PLAN']
    for blk in ('WB_SHIP', 'OZON_SHIP', 'HOLD'):
        want = sorted([r for r in plan if r['sheet_block'] == blk], key=lambda r: r['block_order'])
        got = rows_of(M['01_SUPPLY_PLAN'], blk)
        keys_got = [k.split('#')[0] for _, k in got if k]
        check('V1', f'01 {blk}: ключи и порядок строк = V_OPS_SHEET_PLAN', keys_got == [r['business_key'] for r in want],
              f'{len(keys_got)} / {len(want)}')
        if blk != 'HOLD' and len(got) == len(want):
            bad = [r for (row, _), w in zip(got, want) for r in [row]
                   if (ws.cell(row, 1).value, ws.cell(row, 2).value, ws.cell(row, 3).value, ws.cell(row, 7).value)
                   != (w['product_name'], w['rec_final'], w['rec_physical_units'], w['risk_label'])]
            check('V2', f'01 {blk}: товар, рекомендация, физ. единицы, риск = BigQuery', not bad, str(bad[:3]))
    p0 = plan[0]
    usend = rows_of(M['01_SUPPLY_PLAN'], 'USEND_TOP')
    check('V3', '01 Usend: WB и Ozon = итоги контракта',
          (ws.cell(usend[2][0], 2).value, ws.cell(usend[2][0], 3).value, ws.cell(usend[3][0], 2).value) ==
          (p0['wb_ship_positions'], p0['wb_ship_physical'], p0['ozon_ship_positions']))
    ff = b.query(f'SELECT ANY_VALUE(t_total_physical) t FROM `{b.P}.evetis_ops.V_OPS_SHEET_FF_STOCK`')[0]['t']
    t_row = next(i for i, m in enumerate(M['03_FF_STOCK'], start=1) if m == '@T:FF_STOCK')
    check('V4', '03: ИТОГО физически на ФФ = BigQuery', wb['03_FF_STOCK'].cell(t_row, 11).value == ff, str(ff))
    for blk, ch in (('CALC_WB', 'WB'), ('CALC_OZON', 'OZON')):
        n = len([r for r in plan if r['channel'] == ch])
        check('V5', f'05 {blk}: строк = плану канала', len(rows_of(M['05_РАСЧЁТ'], blk)) == n, str(n))
    check('V6', 'DATA: строк плана = BigQuery', len(rows_of(M['DATA'], 'D_V_OPS_SHEET_PLAN')) == len(plan), str(len(plan)))

    # ── O: решения владельца
    st = wb['_OPS_STATE']
    head = [st.cell(1, c).value for c in range(1, 15)]
    state = {}
    for r in range(2, st.max_row + 1):
        if st.cell(r, 1).value:
            state[st.cell(r, 1).value] = dict(zip(head, [st.cell(r, c).value for c in range(1, 15)]))
    bad = []
    for blk in ('WB_SHIP', 'OZON_SHIP'):
        for row, k in rows_of(M['01_SUPPLY_PLAN'], blk):
            key = k.split('#')[0]
            s = state.get(key, {})
            shown = ws.cell(row, 4).value
            want = (s.get('approved_qty') if s.get('approval_status') == 'APPROVED'
                    else f'ПЕРЕСОГЛ. {qty(s.get("previous_approved_qty"))}' if s.get('approval_status') == 'REAPPROVAL_REQUIRED'
                    else None)
            if (shown or None) != (want if want not in ('', None) else None) and not (isinstance(want, (int, float)) and shown == want):
                bad.append((row, key, shown, want))
    check('O1', '01: «Одобрено владельцем» = _OPS_STATE по каноническому ключу', not bad, str(bad[:3]))
    fails = [x for x in RESULTS if not x[2]]
    print(f'\n{len(RESULTS) - len(fails)} PASS / {len(fails)} FAIL')
    return 1 if fails else 0


def same(a_path, b_path):
    a, bb = openpyxl.load_workbook(a_path), openpyxl.load_workbook(b_path)
    diffs = []
    if a.sheetnames != bb.sheetnames:
        diffs.append(('sheets', a.sheetnames, bb.sheetnames))
    for s in a.sheetnames:
        wa, wbb = a[s], bb[s]
        for row in wa.iter_rows(max_row=max(wa.max_row, wbb.max_row), max_col=max(wa.max_column, wbb.max_column)):
            for c in row:
                o = wbb[c.coordinate]
                if c.value != o.value or (c.comment.text if c.comment else None) != (o.comment.text if o.comment else None):
                    diffs.append((s, c.coordinate, c.value, o.value))
        if sorted(map(str, wa.merged_cells.ranges)) != sorted(map(str, wbb.merged_cells.ranges)):
            diffs.append((s, 'merges'))
        if wa.sheet_state != wbb.sheet_state or wa.freeze_panes != wbb.freeze_panes:
            diffs.append((s, 'state/freeze'))
    print(f'{"PASS" if not diffs else "FAIL"} выгрузки совпадают · расхождений {len(diffs)}', diffs[:5])
    return 0 if not diffs else 1


if __name__ == '__main__':
    if len(sys.argv) >= 3 and sys.argv[1] == 'same':
        sys.exit(same(sys.argv[2], sys.argv[3]))
    if len(sys.argv) >= 3 and sys.argv[1] == 'verify':
        before = sys.argv[sys.argv.index('--before') + 1] if '--before' in sys.argv else None
        sys.exit(verify(sys.argv[2], before))
    print(__doc__)
    sys.exit(2)
