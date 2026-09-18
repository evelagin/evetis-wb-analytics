#!/usr/bin/env python3
"""SKU Portfolio V2 (Phase C1) — регрессия карточек против backend Phase B. Только SELECT.

Для каждого окна × выборки товаров:
  • агрегат карточек KPI (BASE+AGG → k) против строки TVF_SKU_PERFORMANCE_V2_PERIOD (портфель или SKU);
  • строки таблицы «Товары» против строк TVF (цена, Δ, заказы, выкупы, выкуп, реклама, ДРР, вклад, маржа, ₽/выкуп);
  • «Требуют внимания»: состав и точный порядок против независимой оценки правил на Python;
  • контракт малой выборки (display ≠ alert eligibility), отдельные счётчики:
      LS-1 малая выборка сохраняет фактическое Δ (KPI и таблица) и ставит нейтральную пометку;
      LS-2 флаг надёжности остаётся false (карточка = backend Phase B, порог min_reliable_units не меняется);
      LS-3 малая выборка не запускает правила внимания, требующие надёжной выборки;
      LS-4 несопоставимые окна не получают выдуманного Δ («н/с» / NULL).
SQL карточек берётся из tools/metabase_sku_v2_portfolio_build.py — тот же текст, что в Metabase.
"""
import json, os, sys
from decimal import Decimal as D
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
import metabase_sku_v2_portfolio_build as pb

WINDOWS = {'last30 (18.08–16.09)': ('2026-08-18', '2026-09-16'), 'last7 (10–16.09)': ('2026-09-10', '2026-09-16'),
           'no signals 27.04–10.05': ('2026-04-27', '2026-05-10'),
           '31.08–13.09': ('2026-08-31', '2026-09-13'), '01–14.09': ('2026-09-01', '2026-09-14'),
           'final 27.07–23.08': ('2026-07-27', '2026-08-23'), 'provisional 01–17.09': ('2026-09-01', '2026-09-17'),
           'no previous period 13–26.04': ('2026-04-13', '2026-04-26')}
SELS = {'all': None, 'Крем Руки': ['Крем Руки'], 'Сыворотка АКНЕ': ['Сыворотка АКНЕ'],
        'Крем Вишня (low)': ['Крем Вишня'], 'Набор сыв+крем АКНЕ (bundle)': ['Набор сыв+крем АКНЕ'],
        'Крем УВЛ (low)': ['Крем УВЛ'], 'Набор руки+амбра (low)': ['Набор руки+амбра']}
NM = {'Крем Руки': 252442517, 'Сыворотка АКНЕ': 305101361, 'Крем Вишня': 593111986, 'Набор сыв+крем АКНЕ': 567668635,
      'Крем УВЛ': 438775437, 'Набор руки+амбра': 930334396}
# KPI → (флаг сопоставимости TVF или None, флаги надёжности TVF, база Δ в k: (тек., пред.))
KPI = {'orders': ('orders_comparable', ('cur_orders_reliable', 'prv_orders_reliable'), ('c_orders_gross_units', 'p_orders_gross_units')),
       'buyouts': ('sales_comparable', ('cur_buyouts_reliable', 'prv_buyouts_reliable'), ('c_buyout_units', 'p_buyout_units')),
       'cohort_rate': (None, ('cur_cohort_reliable', 'prv_cohort_reliable'), ('c_rate', 'p_rate')),
       'price': ('orders_comparable', ('cur_orders_reliable', 'prv_orders_reliable'), ('c_price', 'p_price')),
       'ads': ('drr_comparable', (), ('c_ads_attributed_rub', 'p_ads_attributed_rub')),
       'drr': ('drr_comparable', ('cur_buyouts_reliable', 'prv_buyouts_reliable'), ('c_drr', 'p_drr')),
       'contribution': ('economics_comparable', ('cur_buyouts_reliable', 'prv_buyouts_reliable'),
                        ('c_contribution_after_cogs_rub', 'p_contribution_after_cogs_rub')),
       'margin': ('economics_comparable', ('cur_buyouts_reliable', 'prv_buyouts_reliable'), ('c_margin', 'p_margin'))}
KPI_SQL = pb.BASE + pb.AGG + "\nSELECT k.*, " + ", ".join(
    pb.CARDS[key]['sql'].split("\nSELECT ", 1)[1].rsplit(" AS kpi FROM k", 1)[0] + f" AS txt_{key}" for key in KPI) + " FROM k"
LS = {'LS-1': [0, 0], 'LS-2': [0, 0], 'LS-3': [0, 0], 'LS-4': [0, 0]}   # [проверок, нарушений]


def ls(tag, ok, detail):
    global bad, checks
    LS[tag][0] += 1; LS[tag][1] += not ok; checks += 1; bad += not ok
    if not ok: out.append({'contract': tag, **detail, 'equal': False})
P = 'project-fa311fc0-4d87-4781-986'
PAIRS = [('c_orders_gross_units', 'cur_orders_gross_units'), ('c_buyout_units', 'cur_buyout_units'),
         ('c_rate', 'cur_cohort_buyout_rate'), ('c_price', 'cur_avg_seller_price_orders_rub'),
         ('p_price', 'prv_avg_seller_price_orders_rub'), ('c_ads_attributed_rub', 'cur_ads_attributed_rub'),
         ('c_drr', 'cur_drr'), ('c_contribution_after_cogs_rub', 'cur_contribution_after_cogs_rub'),
         ('c_margin', 'cur_contribution_margin_after_cogs'), ('c_per_buyout', 'cur_contribution_after_cogs_per_buyout_rub'),
         ('p_orders_gross_units', 'prv_orders_gross_units'), ('p_rate', 'prv_cohort_buyout_rate'),
         ('orders_cmp', 'orders_comparable'), ('econ_cmp', 'economics_comparable'), ('drr_cmp', 'drr_comparable')]
TAB = [('price', 'cur_avg_seller_price_orders_rub'), ('orders', 'cur_orders_gross_units'), ('buyouts', 'cur_buyout_units'),
       ('rate', 'cur_cohort_buyout_rate'), ('ads', 'cur_ads_attributed_rub'), ('drr', 'cur_drr'), ('contribution', 'cur_contribution_after_cogs_rub'),
       ('margin', 'cur_contribution_margin_after_cogs'), ('per_buyout', 'cur_contribution_after_cogs_per_buyout_rub')]


def eq(a, b):
    if a in (None, '') and b in (None, ''):
        return True
    if a in ('true', 'false') or b in ('true', 'false'):
        return a == b
    try:
        return abs(D(a) - D(b)) <= D('0.000001')
    except Exception:
        return a == b


bad, checks, out = 0, 0, []
for wn, (d1, d2) in WINDOWS.items():
    tvf = {r['nm_id'] or 'TOTAL': r for r in pb.bq(f"SELECT * FROM `{P}.wb_mart.TVF_SKU_PERFORMANCE_V2_PERIOD`(DATE '{d1}', DATE '{d2}')")['rows']}
    for sn, sku in SELS.items():
        k = pb.bq(pb.render(KPI_SQL, d1, d2, sku))['rows'][0]
        ref = tvf['TOTAL'] if sku is None else tvf[str(NM[sku[0]])]
        for a, b in PAIRS:
            ok = eq(k.get(a), ref.get(b)); checks += 1; bad += not ok
            out.append({'window': wn, 'selection': sn, 'card': a, 'tvf': b, 'card_value': k.get(a), 'tvf_value': ref.get(b), 'equal': ok})
        # контракт малой выборки в KPI
        for key, (cmpf, relf, (cv, pv)) in KPI.items():
            txt = k.get(f'txt_{key}') or ''
            delta = txt.split('  ·  ', 1)[1] if '  ·  ' in txt else ''
            rel = all(ref.get(f) == 'true' for f in relf)
            det = {'window': wn, 'selection': sn, 'kpi': key, 'text': txt}
            if cmpf and ref.get(cmpf) != 'true':
                ls('LS-4', delta == 'н/с', det)                                   # несопоставимо → «н/с», без Δ
                continue
            if delta in ('н/с', '—'):
                continue
            ls('LS-1', delta[:1] in '↑↓→' and delta.endswith(' · мало данных') == (not rel), det)   # Δ есть; пометка ⇔ мало данных
            if relf:
                ls('LS-2', (k.get({'orders': 'orders_rel', 'price': 'orders_rel', 'cohort_rate': 'cohort_rel'}.get(key, 'buyouts_rel')) == 'true') == rel, det)
    rows = {r['nm']: r for r in pb.bq(pb.render(pb.TABLE_SQL, d1, d2))['rows']}
    for nm, r in rows.items():
        ref = tvf[nm]
        for a, b in TAB:
            ok = eq(r.get(a), ref.get(b)); checks += 1; bad += not ok
            if not ok: out.append({'window': wn, 'table_nm': nm, 'col': a, 'card_value': r.get(a), 'tvf_value': ref.get(b), 'equal': ok})
        cmp_ = ref['orders_comparable'] == 'true'
        rel = ref['cur_orders_reliable'] == 'true' and ref['prv_orders_reliable'] == 'true'
        for col, tv in (('price_d', 'avg_seller_price_orders_delta_pct'), ('orders_d', 'orders_delta_pct')):
            det = {'window': wn, 'table_nm': nm, 'col': col, 'card_value': r.get(col), 'tvf_value': ref.get(tv)}
            if not cmp_:
                ls('LS-4', r.get(col) in (None, ''), det)                          # несопоставимо → пусто
            elif not rel and ref.get(tv) not in (None, ''):
                ls('LS-1', eq(r.get(col), ref.get(tv)) and 'малая выборка' in r['note'], det)  # Δ сохранено + пометка
            else:
                ok = eq(r.get(col), ref.get(tv)); checks += 1; bad += not ok
                if not ok: out.append({**det, 'equal': ok})
        if not rel:   # флаг backend ложен ровно потому, что база < порога, а порог Phase B = 10
            ls('LS-2', D(ref['min_reliable_units']) == 10 and
               (D(ref.get('cur_orders_gross_units') or 0) < 10 or D(ref.get('prv_orders_gross_units') or 0) < 10),
               {'window': wn, 'table_nm': nm})
        exp_note = ref['low_sample'] == 'true' or (cmp_ and not rel and ref.get('orders_delta_pct') not in (None, ''))
        ok = ('малая выборка' in r['note']) == exp_note; checks += 1; bad += not ok
        if not ok: out.append({'window': wn, 'table_nm': nm, 'note': r['note'], 'equal': ok})
    # «Требуют внимания»: независимая оценка правил по строкам TVF (Python) = карточка
    from datetime import date
    today = date.today()
    exp, low_raw = [], {}
    for nm, r in tvf.items():
        if nm == 'TOTAL':
            continue
        f = lambda k: D(r[k]) if r.get(k) not in (None, '') else None
        t = lambda k: r.get(k) == 'true'
        c_m = (date.fromisoformat(r['period_to']) - today).days <= -21 or (f('cur_cohort_unresolved_orders') or 0) + (f('cur_cohort_conflict_orders') or 0) == 0
        p_m = (date.fromisoformat(r['prev_to']) - today).days <= -21 or (f('prv_cohort_unresolved_orders') or 0) + (f('prv_cohort_conflict_orders') or 0) == 0
        drr = (f('cur_ads_attributed_rub') / f('cur_buyout_seller_price_rub')) if f('cur_buyout_seller_price_rub') else None
        hits = []
        contr = f('cur_contribution_after_cogs_rub')
        raw1 = contr is not None and contr < 0
        raw3 = t('orders_comparable') and int(r['period_days']) >= 14 and f('orders_delta_pct') is not None and f('orders_delta_pct') <= D(str(pb.ORDERS_DROP))
        raw4 = t('orders_comparable') and c_m and p_m and f('cohort_buyout_rate_delta_pp') is not None and f('cohort_buyout_rate_delta_pp') <= D(str(pb.COHORT_DROP_PP))
        raw5 = drr is not None and drr >= D(str(pb.DRR_HIGH))
        if raw1 and t('cur_buyouts_reliable'): hits.append((1, -contr))
        if (f('cur_ads_attributed_rub') or 0) > 0 and f('cur_buyout_units') == 0: hits.append((2, f('cur_ads_attributed_rub')))
        if raw3 and t('cur_orders_reliable') and t('prv_orders_reliable'): hits.append((3, -f('orders_delta_pct')))
        if raw4 and t('cur_cohort_reliable') and t('prv_cohort_reliable'): hits.append((4, -f('cohort_buyout_rate_delta_pp')))
        if raw5 and t('cur_buyouts_reliable'): hits.append((5, drr))
        if hits: exp.append((hits[0][0], -hits[0][1], int(nm), nm))
        # LS-3: порог пройден на ненадёжной выборке → карточка не должна давать этот сигнал
        unrel = [rk for rk, raw, rel_ in ((1, raw1, t('cur_buyouts_reliable')),
                                          (3, raw3, t('cur_orders_reliable') and t('prv_orders_reliable')),
                                          (4, raw4, t('cur_cohort_reliable') and t('prv_cohort_reliable')),
                                          (5, raw5, t('cur_buyouts_reliable'))) if raw and not rel_]
        if unrel: low_raw[nm] = unrel
    exp.sort()
    # правила карточки по ВСЕМ SKU (без лимита 6): тот же SQL карточки до ранжирования
    rules_sql = pb.CARDS['attention']['sql'].split(",\ntop AS", 1)[0] + \
        "\nSELECT CAST(nm_id AS STRING) AS nm, ARRAY_TO_STRING(ARRAY(SELECT CAST(sev_rank AS STRING) FROM UNNEST(hits)), ',') AS ranks FROM rules"
    card_ranks = {row['nm']: set(filter(None, (row['ranks'] or '').split(','))) for row in pb.bq(pb.render(rules_sql, d1, d2))['rows']}
    for nm, rks in low_raw.items():
        for rk in rks:
            ls('LS-3', str(rk) not in card_ranks.get(nm, set()), {'window': wn, 'nm': nm, 'rule': rk})
    got = [row['nm'] for row in pb.bq(pb.render(pb.CARDS['attention']['sql'], d1, d2))['rows'] if row['nm']]
    want = [e[3] for e in exp[:6]]
    ok = got == want; checks += 1; bad += not ok
    out.append({'window': wn, 'attention_card': got, 'attention_expected_order': want, 'flagged_total': len(exp), 'equal': ok})
    print(f'{wn}: table rows {len(rows)}; attention card {len(got)} / expected {len(exp)} flagged; '
          f'low-sample threshold crossings suppressed: {sum(len(v) for v in low_raw.values())}')
RES = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'docs', 'sku_performance_v2_phase_c1_evidence',
                   'portfolio_regression_result.json')
json.dump({'checks': checks, 'mismatches': bad, 'low_sample_contract': {k: {'checks': v[0], 'violations': v[1]} for k, v in LS.items()},
           'detail': out}, open(RES, 'w'), ensure_ascii=False, indent=1)
print(f'checks={checks} mismatches={bad}')
for tag, (n, v) in LS.items():
    print(f'  {tag}: checks={n} violations={v}')
for o in out:
    if not o['equal']: print('MISMATCH', o)
sys.exit(1 if bad else 0)
