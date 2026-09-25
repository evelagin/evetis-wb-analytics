import ro, json, hashlib, sys
def h(o): return hashlib.sha256(json.dumps(o,sort_keys=True,ensure_ascii=False).encode()).hexdigest()[:16]
def snap(sid, sheet, tag, owner_tail_first=563):
    V=ro.get(sid,'/values/'+sheet+'!A1:VB650',valueRenderOption='UNFORMATTED_VALUE',dateTimeRenderOption='SERIAL_NUMBER').get('values',[])
    F=ro.get(sid,'/values/'+sheet+'!A1:VB650',valueRenderOption='FORMULA').get('values',[])
    m=ro.get(sid,ranges=[sheet],fields='namedRanges,sheets(properties(sheetId,gridProperties),merges,conditionalFormats,columnGroups,rowGroups,data(rowMetadata(pixelSize,hiddenByUser),columnMetadata(pixelSize,hiddenByUser)))')
    s=m['sheets'][0]; sidn=s['properties']['sheetId']
    merges=sorted((x['startRowIndex'],x['endRowIndex'],x['startColumnIndex'],x['endColumnIndex']) for x in s.get('merges',[]))
    cf=s.get('conditionalFormats',[])
    cf_logic=[{'ranges':r['ranges'],'cond':(r.get('booleanRule') or {}).get('condition'),'grad':'gradientRule' in r} for r in cf]
    cf_style=[(r.get('booleanRule') or {}).get('format') or r.get('gradientRule') for r in cf]
    nr=sorted((n['name'],json.dumps(n['range'],sort_keys=True)) for n in m.get('namedRanges',[]) if n['range'].get('sheetId')==sidn)
    tail=[row[owner_tail_first-1:] if len(row)>=owner_tail_first else [] for row in F]
    d=s['data'][0]
    out=dict(values=h(V),formulas=h(F),merges=h(merges),named_ranges=h(nr),cf_logic=h(cf_logic),cf_style=h(cf_style),cf_count=len(cf),
             merges_count=len(merges),owner_tail=h(tail),row_heights=h([r.get('pixelSize') for r in d['rowMetadata']]),
             col_widths=h([c.get('pixelSize') for c in d['columnMetadata']]),hidden_cols=h([c.get('hiddenByUser',False) for c in d['columnMetadata']]),
             groups=h([s.get('columnGroups',[]),s.get('rowGroups',[])]),grid=s['properties']['gridProperties'])
    json.dump({'V':V,'F':F,'merges':merges,'cf':cf,'nr':nr},open(f'snap_{tag}.json','w'),ensure_ascii=False)
    return out
if __name__=='__main__':
    tag=sys.argv[1]; which=sys.argv[2]
    sid,sheet=(ro.PROD,'OZON_Юнит_2025') if which=='prod' else (ro.TEST, sys.argv[3] if len(sys.argv)>3 else 'OZON_VISUAL_REHEARSAL')
    r=snap(sid,sheet,tag); print(json.dumps(r,ensure_ascii=False)); json.dump(r,open(f'fp_{tag}.json','w'))
