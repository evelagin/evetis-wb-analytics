import ro, json, hashlib, sys
h=lambda o: hashlib.sha256(json.dumps(o,sort_keys=True,ensure_ascii=False).encode()).hexdigest()[:16]
V=ro.get(ro.PROD,'/values/WB_Юнит_2025!A1:WY803',valueRenderOption='UNFORMATTED_VALUE',dateTimeRenderOption='SERIAL_NUMBER').get('values',[])
F=ro.get(ro.PROD,'/values/WB_Юнит_2025!A1:WY803',valueRenderOption='FORMULA').get('values',[])
m=ro.get(ro.PROD,ranges=['WB_Юнит_2025'],fields='sheets(merges,conditionalFormats,data(rowMetadata(pixelSize),columnMetadata(pixelSize,hiddenByUser)))')['sheets'][0]
z=ro.get(ro.PROD,'/values/ZZ_CONFIG!A1:B34',valueRenderOption='UNFORMATTED_VALUE').get('values',[])
out=dict(values=h(V),formulas=h(F),merges=h(sorted(json.dumps(x,sort_keys=True) for x in m.get('merges',[]))),cf=h(m.get('conditionalFormats',[])),geometry=h(m['data'][0]),zz=h(z))
print(json.dumps(out)); json.dump(out,open(f'fpwb_{sys.argv[1]}.json','w'))
