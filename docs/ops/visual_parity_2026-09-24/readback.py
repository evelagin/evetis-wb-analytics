import json, sys
from grid import load, col
def hexof(c):
    return '#%02x%02x%02x'%tuple(round(c.get(k,0)*255) for k in ('red','green','blue'))
B=load(sys.argv[1]); plan=json.load(open('migration_plan.json'))
ok=bad=0; badl=[]; hmis=0
for rq in plan['requests']:
    if 'repeatCell' in rq:
        r=rq['repeatCell']['range']; want=rq['repeatCell']['cell']['userEnteredFormat']['borders']
        for row in range(r['startRowIndex']+1,r['endRowIndex']+1):
            for c in range(r['startColumnIndex']+1,r['endColumnIndex']+1):
                got=((B.get((row,c)) or {}).get('userEnteredFormat') or {}).get('borders',{})
                for s,w in want.items():
                    g=got.get(s) or {}
                    wc=hexof(w['colorStyle']['rgbColor']) if 'colorStyle' in w else '#000000'
                    gc=col(g.get('colorStyle') or g.get('color')) or '#000000'
                    if g.get('style')==w['style'] and gc==wc: ok+=1
                    else: bad+=1; badl.append((row,c,s,w['style'],wc,g.get('style'),gc))
print('border sides verified',ok,'mismatch',bad); print(badl[:10])
