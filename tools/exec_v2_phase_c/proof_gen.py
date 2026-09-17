import json,glob,re
proposal=open('/Users/evgenelagin/Projects/evetis-wb-analytics/sql/dash/proposals/executive_v2_daily_PROPOSAL.sql').read()
body=proposal[proposal.index('WITH\nk AS'):].rstrip()
ids=[int(x) for x in open('pc/card_ids.txt').read().split()[0].split(',')]+[186]
periods=[('2026-08-31','2026-09-13'),('2026-08-17','2026-09-15'),('2026-09-01','2026-09-16')]
VIEWS=['V_DASH_KPI_DAILY','V_DASH_FINANCE_CORRECTED_DAILY','V_DASH_EXECUTIVE_ECONOMICS_DAILY','V_DASH_SETTLEMENT_DAILY','V_DASH_EXECUTIVE_BREAKDOWN_DAILY','V_DASH_BUYOUT_COHORT_DAILY']
out=["CREATE TEMP TABLE ev2 AS\n"+body+";",
     "CREATE TEMP TABLE proof (card INT64, d1 STRING, variant STRING, payload STRING);"]
for cid in ids:
    c=json.load(open(f'pc/cards/card-{cid}.json')); sql=c['dataset_query']['stages'][0]['native']
    for d1,d2 in periods:
        orig=sql.replace('{{day}}',f"day BETWEEN '{d1}' AND '{d2}'")
        new=orig.replace('`wb_mart.V_DASH_FRESHNESS_HEADER`','(SELECT * FROM ev2 LIMIT 1)')
        for v in VIEWS: new=new.replace(f'`wb_mart.{v}`','ev2')
        assert 'wb_mart.V_DASH' not in new, cid
        for variant,q in (('views',orig),('layer',new)):
            out.append(f"INSERT proof SELECT {cid}, '{d1}', '{variant}', TO_JSON_STRING(ARRAY_AGG(TO_JSON_STRING(t) ORDER BY TO_JSON_STRING(t))) FROM (\n{q}\n) t;")
out.append("""SELECT v.card, v.d1, v.payload = l.payload AS equal, IF(v.payload = l.payload, NULL, v.payload) AS views_payload, IF(v.payload = l.payload, NULL, l.payload) AS layer_payload
FROM proof v JOIN proof l ON l.card = v.card AND l.d1 = v.d1 AND l.variant = 'layer'
WHERE v.variant = 'views' ORDER BY equal, v.card, v.d1;""")
open('pc/proof.sql','w').write('\n\n'.join(out))
print(len(out),'statements')
