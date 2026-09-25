import json, sys
from collections import Counter
from grid import load, col
def flat(d, p=''):
    out={}
    if isinstance(d, dict):
        for k,v in d.items(): out.update(flat(v, p+'.'+k if p else k))
    else: out[p]=d
    return out
def ue(g,k): return flat(((g.get(k) or {}).get('userEnteredFormat')) or {})
def diff(a,b):
    A=load(a); B=load(b); keys=set(A)|set(B); ch=Counter(); cells=[]
    for k in keys:
        x=ue(A,k); y=ue(B,k)
        if x!=y:
            props=sorted({p.split('.')[0]+'.'+p.split('.')[1] if p.startswith('borders') else p.split('.')[0] for p in set(x)|set(y) if x.get(p)!=y.get(p)})
            for p in props: ch[p]+=1
            cells.append((k,props))
    return A,B,ch,cells
if __name__=='__main__':
    A,B,ch,cells=diff(sys.argv[1],sys.argv[2])
    print('changed cells',len(cells)); print(ch.most_common())
    rows=Counter(k[0] for k,_ in cells); print('row range',min(rows) if rows else None,max(rows) if rows else None)
    json.dump([[k[0],k[1],p] for k,p in cells],open(sys.argv[3],'w'))
