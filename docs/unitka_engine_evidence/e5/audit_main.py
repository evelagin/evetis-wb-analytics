"""E5 — read-only аудит формул книги «Юнитка_Evetis Cosmetics» (экспорт xlsx 13.09.2026).
Ничего не пишет в книгу. Результаты — JSON/CSV рядом + печать сводки."""
import openpyxl, re, json, collections, csv, sys, datetime
from openpyxl.utils import get_column_letter as L, column_index_from_string as CI

BOOK = 'book_2026-09-13.xlsx'
wb = openpyxl.load_workbook(BOOK)

# ---------- helpers ----------
DUMMY = re.compile(r'__xludf\.DUMMYFUNCTION\("(.*)"\)\s*,', re.S)
def real_formula(v):
    """Восстанавливает исходную формулу Google из экспорта xlsx."""
    if hasattr(v, 'text'):
        return '=' + str(v.text), True
    if isinstance(v, str) and v.startswith('='):
        if '__xludf.DUMMYFUNCTION' in v:
            m = DUMMY.search(v)
            if m:
                inner = m.group(1).replace('""', '"')
                inner = re.sub(r'"&"', '', inner)
                return '=' + inner, False
        return v, False
    return None, False

REF = re.compile(r"(?<![A-Za-z_\d!'])(\$?)([A-Z]{1,3})(\$?)(\d{1,7})(?![\d(])")
COLREF = re.compile(r"(?<![A-Za-z_\d!'$])(\$?)([A-Z]{1,3}):(\$?)([A-Z]{1,3})(?![A-Za-z\d(])")
def normalize(formula, r, c):
    """A1 → относительная запись R[dr]C[dc] (абсолютные — R$n / C$n)."""
    def rep(m):
        cabs, col, rabs, row = m.groups()
        dc = f"C${col}" if cabs else f"C[{CI(col)-c}]"
        dr = f"R${row}" if rabs else f"R[{int(row)-r}]"
        return dr + dc
    f = REF.sub(rep, formula)
    return f

FUNC = re.compile(r'\b([A-Z][A-Z0-9_.]+)\s*\(')
VOLATILE = {'NOW', 'TODAY', 'RAND', 'RANDBETWEEN', 'INDIRECT', 'OFFSET'}
HEAVY = {'FILTER', 'SUMPRODUCT', 'QUERY', 'ARRAYFORMULA', 'VLOOKUP', 'HLOOKUP', 'XLOOKUP', 'MATCH', 'INDEX',
         'SUMIF', 'SUMIFS', 'COUNTIF', 'COUNTIFS', 'AVERAGEIF', 'AVERAGEIFS', 'IMPORTRANGE', 'UNIQUE', 'SORT',
         'REGEXMATCH', 'REGEXEXTRACT', 'MAXIFS', 'MINIFS', 'TRANSPOSE', 'MMULT', 'SEQUENCE', 'BYROW', 'MAP', 'LAMBDA', 'GOOGLEFINANCE'}
FULLCOL = re.compile(r"(?<![A-Za-z\d$!])\$?[A-Z]{1,3}:\$?[A-Z]{1,3}(?![A-Za-z\d])")
XSHEET = re.compile(r"'([^']+)'!|(?<![A-Za-z_])([A-Za-z_][\w]*)!")

out = {}
if __name__ != "__main__":
    raise SystemExit

# ---------- 1. Перепись функций по всем листам ----------
census = {}
for ws in wb.worksheets:
    cnt = collections.Counter(); vol = 0; heavy = 0; fullcol = 0; xsheet = collections.Counter(); n_f = 0; arr = 0
    dummy = 0; iferror = 0; longest = (0, '')
    for row in ws.iter_rows():
        for cell in row:
            f, is_arr = real_formula(cell.value)
            if f is None: continue
            n_f += 1; arr += is_arr
            if isinstance(cell.value, str) and '__xludf' in cell.value: dummy += 1
            fs = set(FUNC.findall(f))
            for fn in fs: cnt[fn] += 1
            if fs & VOLATILE: vol += 1
            if fs & HEAVY: heavy += 1
            if 'IFERROR' in fs: iferror += 1
            if FULLCOL.search(f): fullcol += 1
            for m in XSHEET.finditer(f):
                xsheet[m.group(1) or m.group(2)] += 1
            if len(f) > longest[0]: longest = (len(f), f"{cell.coordinate}")
    census[ws.title] = dict(formulas=n_f, array=arr, google_only=dummy, volatile_cells=vol, heavy_cells=heavy,
                            iferror_cells=iferror, fullcol_cells=fullcol, cross_sheet=dict(xsheet),
                            funcs=dict(cnt.most_common(40)), longest=longest)
out['census'] = census

# ---------- 2. Структура WB_Юнит_2025: секции месяцев, блоки, паттерны ----------
ws = wb['WB_Юнит_2025']
MONTHS = ['январ', 'феврал', 'март', 'апрел', 'май', 'июн', 'июл', 'август', 'сентябр', 'октябр', 'ноябр', 'декабр']
titles = []
for r in range(1, ws.max_row + 1):
    a = ws.cell(r, 1).value
    if isinstance(a, str) and len(a) < 40 and any(m in a.lower() for m in MONTHS) and ws.cell(r + 1, 2).value == 'Дата':
        titles.append((r, a.strip()))
sections = []
for i, (r0, title) in enumerate(titles):
    end = titles[i + 1][0] - 1 if i + 1 < len(titles) else ws.max_row
    hdr = r0 + 1
    # дни: строки с датой в B
    days = [r for r in range(hdr + 1, end + 1) if isinstance(ws.cell(r, 2).value, datetime.datetime)]
    first, last = days[0], days[-1]
    # блоки: заголовки nmID в строке r0 начиная с M шагом 24
    blocks = []
    c = 13
    while c <= ws.max_column:
        t = ws.cell(r0, c).value
        if isinstance(t, str) and re.match(r'^\d{6,}', t.strip()):
            blocks.append((c, t.strip()))
        c += 24
    sections.append(dict(title=title, top=r0, hdr=hdr, first=first, last=last, days=len(days), end=end, blocks=blocks))
out['sections'] = sections

# паттерны формул по смещениям блока (0..23) и по сводным колонкам A..L
def pattern_table(sec):
    res = {'block': {}, 'summary': {}}
    for off in range(24):
        pats = collections.Counter(); vals = 0; empt = 0; forms = 0
        for (bc, _) in sec['blocks']:
            c = bc + off
            for r in range(sec['first'], sec['last'] + 1):
                v = ws.cell(r, c).value
                f, _a = real_formula(v)
                if f is not None:
                    forms += 1; pats[normalize(f, r, bc)] += 1  # относительно начала блока по колонкам
                elif v is None or v == '': empt += 1
                else: vals += 1
        res['block'][off] = dict(formulas=forms, values=vals, empty=empt, patterns=pats.most_common(6))
    for c in range(1, 13):
        pats = collections.Counter(); vals = 0; empt = 0; forms = 0
        for r in range(sec['first'], sec['last'] + 1):
            v = ws.cell(r, c).value
            f, _a = real_formula(v)
            if f is not None:
                forms += 1; pats[normalize(f, r, c)] += 1
            elif v is None or v == '': empt += 1
            else: vals += 1
        res['summary'][L(c)] = dict(formulas=forms, values=vals, empty=empt, patterns=pats.most_common(6))
    # итоговая строка (last+1) и плановая (last+2)
    tot = {}
    for c in range(1, 13):
        f, _a = real_formula(ws.cell(sec['last'] + 1, c).value)
        if f: tot[L(c)] = normalize(f, sec['last'] + 1, c)
    res['total_row_summary'] = tot
    return res

patterns = {}
for sec in sections:
    patterns[sec['title']] = pattern_table(sec)
out['patterns'] = patterns

# ---------- 3. Дрейф относительно сентября 2026 ----------
ref = patterns['Сентябрь 2026']
hdr736 = {off: ws.cell(736, 13 + off).value for off in range(24)}
drift = []
for sec in sections:
    p = patterns[sec['title']]
    for off in range(24):
        r = ref['block'][off]; s = p['block'][off]
        rpat = r['patterns'][0][0] if r['patterns'] else None
        spat = s['patterns'][0][0] if s['patterns'] else None
        kind = 'same' if rpat == spat else ('no-formula-in-ref' if rpat is None else ('values-instead' if spat is None and s['values'] else 'different'))
        mixed = len(s['patterns']) > 1
        drift.append(dict(section=sec['title'], off=off, col=hdr736[off], ref_pattern=rpat, sec_pattern=spat, kind=kind,
                          mixed=mixed, formulas=s['formulas'], values=s['values'], empty=s['empty'], n_patterns=len(s['patterns'])))
out['drift'] = drift

# ---------- 4. Условное форматирование ----------
cf = {}
for w in wb.worksheets:
    rules = []
    for rng in w.conditional_formatting:
        for rule in rng.rules:
            rules.append(dict(sqref=str(rng.sqref), type=rule.type, formula=[str(x) for x in (rule.formula or [])][:2], op=rule.operator))
    # размер покрытия
    cells = 0
    for rng in w.conditional_formatting:
        for part in str(rng.sqref).split():
            try:
                c1, r1, c2, r2 = openpyxl.utils.range_boundaries(part)
                cells += (c2 - c1 + 1) * (r2 - r1 + 1) * len(rng.rules)
            except Exception:
                pass
    kinds = collections.Counter(r['type'] for r in rules)
    # одинаковые формулы на разных диапазонах — кандидаты на объединение
    byformula = collections.Counter((r['type'], tuple(r['formula'])) for r in rules)
    cf[w.title] = dict(rules=len(rules), ranges=len(list(w.conditional_formatting)), cell_rule_pairs=cells, kinds=dict(kinds),
                       distinct_formulas=len(byformula), top_dups=[(k, v) for k, v in byformula.most_common(8)])
out['cf'] = cf

# ---------- 5. Размер сетки ----------
grid = {}
for w in wb.worksheets:
    grid[w.title] = dict(max_row=w.max_row, max_col=w.max_column, grid_cells=w.max_row * w.max_column,
                         merged=len(w.merged_cells.ranges), freeze=w.freeze_panes, hidden_cols=sum(1 for k, d in w.column_dimensions.items() if d.hidden),
                         hidden_rows=sum(1 for k, d in w.row_dimensions.items() if d.hidden), images=len(getattr(w, '_images', [])), charts=len(getattr(w, '_charts', [])))
out['grid'] = grid

json.dump(out, open('audit_main.json', 'w'), ensure_ascii=False, indent=1, default=str)

# ---------- печать сводки ----------
print('=== СЕТКА ===')
for k, g in grid.items(): print(f"{k:24} rows {g['max_row']:5} cols {g['max_col']:4} grid {g['grid_cells']:8} merged {g['merged']:4} freeze {g['freeze']} hiddenC {g['hidden_cols']} hiddenR {g['hidden_rows']} img {g['images']} charts {g['charts']}")
print('=== ФУНКЦИИ ===')
for k, c in census.items():
    print(f"{k:24} f={c['formulas']:6} arr={c['array']:4} google-only={c['google_only']:6} volatile={c['volatile_cells']:4} heavy={c['heavy_cells']:6} iferror={c['iferror_cells']:6} fullcol={c['fullcol_cells']:4} xsheet={c['cross_sheet']} longest={c['longest']}")
    print('   ', list(c['funcs'].items())[:18])
print('=== УФ ===')
for k, c in cf.items(): print(f"{k:24} rules={c['rules']:5} ranges={c['ranges']:5} cell×rule={c['cell_rule_pairs']:9} kinds={c['kinds']} distinct={c['distinct_formulas']}")
print('=== СЕКЦИИ ===')
for s in sections: print(f"{s['title']:16} top {s['top']:3} days {s['first']}-{s['last']} ({s['days']}) blocks {len(s['blocks'])} end {s['end']}")
