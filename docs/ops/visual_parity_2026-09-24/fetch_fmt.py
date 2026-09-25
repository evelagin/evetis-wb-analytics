import ro, json, sys
F='sheets(data(startRow,startColumn,rowData(values(userEnteredFormat,effectiveFormat(textFormat,horizontalAlignment,verticalAlignment,wrapStrategy,padding,textRotation),formattedValue,userEnteredValue))))'
def fetch(sid, sheet, r0, r1, ncols, out):
    res=[]
    step=120
    for c0 in range(0, ncols, step):
        c1=min(ncols, c0+step)
        def L(i):
            s='';i+=1
            while i: i,r=divmod(i-1,26); s=chr(65+r)+s
            return s
        rng=f"{sheet}!{L(c0)}{r0}:{L(c1-1)}{r1}"
        m=ro.get(sid, ranges=[rng], includeGridData='true', fields=F)
        res.append({'c0':c0,'data':m['sheets'][0]['data'][0]})
    json.dump(res, open(out,'w'), ensure_ascii=False)
if __name__=='__main__':
    fetch(ro.PROD,'WB_Юнит_2025',735,803,623,'wb_fmt.json')
    fetch(ro.PROD,'OZON_Юнит_2025',570,650,574,'oz_fmt.json')
