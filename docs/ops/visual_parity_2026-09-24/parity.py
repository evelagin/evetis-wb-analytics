import json, sys
from collections import Counter
import cmp
from grid import load, sig
R=load(sys.argv[1])
cmp.SECT['reh_sep']=('oz',R,605,30,22); cmp.SECT['reh_may']=('oz',R,466,31,21)
BLACK={'theme:TEXT','#000000'}
def norm(d):
    # тема TEXT == #000000 (тема книги: TEXT = rgb{}), различие не видно глазу
    out={}
    for k,(a,b) in d.items():
        if k=='borders':
            na={s:(v[0],'#000000' if v[1] in BLACK else v[1]) for s,v in a.items()}
            nb={s:(v[0],'#000000' if v[1] in BLACK else v[1]) for s,v in b.items()}
            if na!=nb: out[k]=(na,nb)
        elif k=='fg' and {a,b}<=BLACK: continue
        elif k=='bg' and {a,b}<={None,'#ffffff'}: continue
        elif k=='nft' and a=='TEXT' and b is None: continue
        else: out[k]=(a,b)
    return out
for other in sys.argv[2:]:
    res=[(rr,role,norm(d)) for rr,role,d,_,_ in cmp.report('wb_sep',other)]
    res=[x for x in res if x[2]]
    print('==',other,'roles with visible difference vs WB Sept:',len(res))
    for rr,role,d in res: print(' ',rr,role,json.dumps(d,ensure_ascii=False)[:230])
