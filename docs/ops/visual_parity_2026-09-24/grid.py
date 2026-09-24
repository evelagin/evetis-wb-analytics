import json
from collections import Counter, defaultdict
def L(i):  # 1-based
    s=''
    while i: i,r=divmod(i-1,26); s=chr(65+r)+s
    return s
def load(fn):
    g={}
    for ch in json.load(open(fn)):
        d=ch['data']; r0=d.get('startRow',0); c0=d.get('startColumn',0)
        for i,row in enumerate(d.get('rowData',[])):
            for j,v in enumerate(row.get('values',[])):
                g[(r0+i+1,c0+j+1)]=v
    return g
def col(c):
    if not c: return None
    if 'rgbColor' in c: c=c['rgbColor']
    if 'themeColor' in c: return 'theme:'+c['themeColor']
    return '#%02x%02x%02x'%tuple(round(c.get(k,0)*255) for k in ('red','green','blue'))
def sig(v, eff=True):
    u=(v or {}).get('userEnteredFormat',{}) ; e=(v or {}).get('effectiveFormat',{})
    tf=(e if eff else u).get('textFormat',{})
    b=u.get('borders',{})
    bs={k:(b[k].get('style'), col(b[k].get('colorStyle') or b[k].get('color'))) for k in ('top','bottom','left','right') if k in b}
    return dict(font=tf.get('fontFamily'), size=tf.get('fontSize'), bold=tf.get('bold',False), italic=tf.get('italic',False),
        fg=col(tf.get('foregroundColorStyle') or tf.get('foregroundColor')),
        bg=col(u.get('backgroundColorStyle') or u.get('backgroundColor')),
        ha=(e if eff else u).get('horizontalAlignment'), va=(e if eff else u).get('verticalAlignment'),
        wrap=(e if eff else u).get('wrapStrategy'), rot=json.dumps((e if eff else u).get('textRotation')) if (e if eff else u).get('textRotation') else None,
        pad=json.dumps(e.get('padding')) if e.get('padding') else None,
        nf=(u.get('numberFormat') or {}).get('pattern'), nft=(u.get('numberFormat') or {}).get('type'),
        borders=bs)
WB_OFF=['DATE','MANUAL_EXTERNAL','IMPRESSIONS','CLICKS','ORDERS','CART','CANCELLATIONS','STOCK','TURNOVER','UNIT_PROFIT','TOTAL_PROFIT','INTERNAL_ADS','EXTERNAL_ADS','DRR','SELLER_PRICE','DISCOUNT','BUYER_PRICE','COMMISSION','NET_AFTER_COMMISSION','LOGISTICS','STORAGE','TAX_RESERVE','FINAL_UNIT_PROFIT','WEEKDAY']
OZ_OFF=WB_OFF[:21]+['OTHER_DIRECT']+WB_OFF[21:]
PLAT={'wb':dict(first=13,width=24,roles=WB_OFF,summary={1:'WEEKDAY',2:'DATE',3:'MANUAL_EXTERNAL',4:'IMPRESSIONS',5:'CLICKS',6:'ORDERS',7:'CART',8:'CANCELLATIONS',9:'TOTAL_PROFIT',10:'INTERNAL_ADS',11:'DRR',12:'WEEKDAY_SEP'}),
      'oz':dict(first=12,width=25,roles=OZ_OFF,summary={1:'WEEKDAY',2:'DATE',3:'MANUAL_EXTERNAL',4:'IMPRESSIONS',5:'CLICKS',6:'ORDERS',7:'CART',8:'CANCELLATIONS',9:'TOTAL_PROFIT',10:'INTERNAL_ADS',11:'GAP'})}
def blockcol(p,b,role): P=PLAT[p]; return P['first']+P['width']*b+P['roles'].index(role)
