from grid import *
import json,sys
W=load('wb_fmt.json'); O=load('oz_fmt.json')
SECT={'wb_sep':('wb',W,735,30,24),'wb_oct':('wb',W,769,31,25),'oz_sep':('oz',O,605,30,22),'oz_aug':('oz',O,570,31,22)}
def rows(top,days): return {'title':[top],'header':[top+1],'day':list(range(top+2,top+2+days)),'mtd':[top+2+days]}
def modesig(p,g,rs,cols):
    c=Counter()
    for r in rs:
        for cc in cols: c[json.dumps(sig(g.get((r,cc))),sort_keys=True,ensure_ascii=False)]+=1
    return c
def roles_cols(p,nb,role): return [blockcol(p,b,role) for b in range(nb) if not (role=='WEEKDAY' and b==nb-1)]
def report(a,b,roleset=None,rowroles=('title','header','day','mtd'),full=False):
    pa,ga,ta,da,na=SECT[a]; pb,gb,tb,db,nb=SECT[b]
    out=[]
    for rr in rowroles:
        for role in PLAT['oz']['roles']:
            if roleset and role not in roleset: continue
            if role not in PLAT[pa]['roles']: continue
            sa=modesig(pa,ga,rows(ta,da)[rr],roles_cols(pa,na,role)); sb=modesig(pb,gb,rows(tb,db)[rr],roles_cols(pb,nb,role))
            ma=json.loads(sa.most_common(1)[0][0]); mb=json.loads(sb.most_common(1)[0][0])
            diff={k:(ma[k],mb[k]) for k in ma if ma[k]!=mb[k]}
            if diff or full: out.append((rr,role,diff,len(sa),len(sb)))
    return out
if __name__=='__main__':
    a,b=sys.argv[1],sys.argv[2]
    for rr,role,diff,na,nb in report(a,b):
        print(f'{rr:6} {role:22} var{na}/{nb} ', json.dumps(diff,ensure_ascii=False))
