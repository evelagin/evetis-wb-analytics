#!/usr/bin/env python3
"""SKU Portfolio V2: вывод карточек через Metabase (фильтры «Период»/«Товар») = прямой BigQuery-запрос того же SQL."""
import json, os, subprocess, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
import metabase_sku_v2_portfolio_build as pb
have = pb.existing()
checks = bad = 0
for d1, d2 in [('2026-08-18', '2026-09-16'), ('2026-07-27', '2026-08-23')]:
    for sku in (None, ['Крем Руки'], ['Крем УВЛ']):   # весь ассортимент, надёжный SKU, малая выборка
        for key, c in pb.CARDS.items():
            cid = have[c['name']]
            cur = json.loads(subprocess.check_output(['mb', 'card', 'get', str(cid), '--full', '--json', '--max-bytes=0', '-p', pb.PROFILE]))
            tt = cur['dataset_query']['stages'][0]['template-tags']; tt = {t['name']: t['id'] for t in (tt if isinstance(tt, list) else tt.values())}
            params = [{"id": tt['day'], "type": "date/all-options", "value": f"{d1}~{d2}", "target": ["dimension", ["template-tag", "day"]]}]
            if sku and 'sku' in tt:
                params.append({"id": tt['sku'], "type": "string/=", "value": sku, "target": ["dimension", ["template-tag", "sku"]]})
            out = subprocess.run(['mb', 'card', 'query', str(cid), '--export-format', 'json', '--max-bytes=0', '-p', pb.PROFILE,
                                  '--parameters', json.dumps(params, ensure_ascii=False)], capture_output=True, text=True).stdout
            mbrows = json.loads(out) if out.strip().startswith('[') else out
            bqrows = pb.bq(pb.render(c['sql'], d1, d2, sku)).get('rows')
            norm = lambda rows: [{k: (None if v in (None, '') else str(v)) for k, v in r.items()} for r in rows] if isinstance(rows, list) else rows
            def canon(rows):
                res = []
                for r in norm(rows):
                    rr = {}
                    for k, v in r.items():
                        try: rr[k] = f"{float(v):.6f}"
                        except (TypeError, ValueError): rr[k] = v
                    res.append(rr)
                return res
            ok = canon(mbrows) == canon(bqrows); checks += 1; bad += not ok
            if not ok: print('DIFF', d1, d2, sku, key, str(mbrows)[:200], '||', str(bqrows)[:200])
print(f'checks={checks} mismatches={bad}')
