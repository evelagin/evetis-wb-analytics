import re
DUMMY = re.compile(r'__xludf\.DUMMYFUNCTION\("(.*)"\)\s*,', re.S)
def real_formula(v):
    """Восстанавливает исходную формулу Google из экспорта xlsx."""
    if hasattr(v, 'text'):
        return '=' + str(v.text), True
    if isinstance(v, str) and v.startswith('='):
        if '__xludf.DUMMYFUNCTION' in v:
            m = DUMMY.search(v)
            if m:
                inner = m.group(1).replace('""', '"')
                inner = re.sub(r'"&"', '', inner)
                return '=' + inner, False
        return v, False
    return None, False

