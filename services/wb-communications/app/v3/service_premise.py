"""Customer evidence for service events, independent of classifier/model labels.

An approved procedure does not prove that its triggering event happened.
Only asserted customer clauses can supply SERVICE_PREMISE=CUSTOMER_REPORTED.
"""
import re
from app.v3.text import normalize, clauses

SERVICE_CODES = {'ORDER.wrong_product', 'ORDER.incomplete_bundle',
                 'PACKAGING.empty_package', 'PACKAGING.leakage',
                 'PACKAGING.broken_package', 'PACKAGING.dispenser_failure'}
ARRIVAL = r'получ\w*|приш[её]л\w*|пришла|пришло|прислал\w*|доставил\w*|привез\w*|привезл\w*'
COMPONENT = r'упаков\w*|короб\w*|крыш\w*|флакон\w*|бан\w*|пакет\w*|товар\w*'
DEFECTS = {
 'PACKAGING.broken_package': (COMPONENT, r'разбит\w*|слом\w*|трес\w*|трещин\w*|лопну\w*|смят\w*|помят\w*|поврежд\w*|раскол\w*'),
 'PACKAGING.dispenser_failure': (r'дозатор\w*|помп\w*|пипет\w*|распылител\w*', r'сломан\w*|сломал\w*|не\s+(?:работ\w*|нажима\w*|качает|выдав\w*|выход\w*|пшика\w*)|заел\w*|отвал\w*|засор\w*'),
 'PACKAGING.leakage': (COMPONENT+r'|тоник\w*|крем\w*|сыворот\w*|средств\w*', r'прот[её]к\w*|вытек\w*|подтек\w*|теч[её]т|разлил\w*'),
 'PACKAGING.empty_package': (COMPONENT, r'пуст(?:ой|ая|ое|ую|ым)|недолив\w*|не\s+долит\w*'),
}

def _asserted(clause):
    # A hypothetical/question about defects is not a report of a defect.
    return not re.search(r'\bесли\b|что\s+будет|может\s+ли|возможн\w*\s+ли|кажется|предполож', clause)

def premises(source, policy):
    found = {}
    for clause in clauses(source):
        if not _asserted(clause):
            continue
        if re.search(ARRIVAL, clause) and re.search(r'не\s+тот|не\s+та|друг\w*\s+(?:товар|крем|флакон|средств)|вместо|перепут', clause):
            found['ORDER.wrong_product'] = clause
        missing = re.search(r'непол\w*\s+(?:набор|комплект)|(?:набор|комплект)\w*[^.!?]{0,25}непол\w*|не\s+хватает|недолож\w*|не\s+положил\w*', clause)
        if missing and (re.search(ARRIVAL, clause) or re.search(r'набор|комплект|заказ',clause)):
            found['ORDER.incomplete_bundle'] = clause
        for code, (subject, predicate) in DEFECTS.items():
            if not (re.search(subject, clause) and re.search(predicate, clause)):
                continue
            # Preserve explicit no-defect reports; "не работает" is a defect.
            if re.search(r'без\s+(?:поврежд|трещин|недолив)|не\s+(?:протек|прот[её]к|слом|поврежд|смят|помят|разбит|лопну|тресну|вытек)|никак\w*\s+(?:поврежд|трещин)',clause):
                continue
            found[code] = clause
        # Preserve policy-defined assertion forms, but never a bare "вместо".
        for code in SERVICE_CODES - {'ORDER.wrong_product'}:
            for pattern in policy['situations'].get(code, []):
                hit = re.search(pattern, clause)
                if not hit:
                    continue
                prefix=clause[max(0,hit.start()-30):hit.start()]
                if re.search(r'\b(?:нет|без)\s*(?:\w+\s*)?$|\bне\s*(?:\w+\s*)?$',prefix):
                    continue
                if code not in DEFECTS or re.search(DEFECTS[code][0],clause):
                    found[code] = clause
    return [{'code':code,'evidence_span':span,'source':'rules',
             'provenance':'CUSTOMER_REPORTED','service_premise':'CUSTOMER_REPORTED'}
            for code,span in sorted(found.items())]

EVENT_FAMILIES = {
 'ORDER.wrong_product': 'WRONG_PRODUCT',
 'ORDER.incomplete_bundle': 'MISSING_ITEM',
 'PACKAGING.empty_package': 'EMPTY_CONTAINER',
 'PACKAGING.leakage': 'LEAK',
 'PACKAGING.broken_package': 'DAMAGE',
 'PACKAGING.dispenser_failure': 'BROKEN_DISPENSER',
}


def response_events(text):
    """Typed triggering propositions, checked before approved procedure stripping."""
    n = normalize(text)
    events = {EVENT_FAMILIES[p['code']] for p in premises(n, {'situations': {}})}
    if re.search(r'вы\s+получили\s+не\s+тот|вам\s+(?:прислали|доставили)\s+(?:друг|не\s+тот)', n):
        events.add('WRONG_PRODUCT')
    if re.search(r'не\s+оказалось[^.!?]{0,55}(?:позици|крем|сыворот|тоник|средств)|(?:неполн\w*|некомплект\w*)\s+(?:набор|заказ)', n):
        events.add('MISSING_ITEM')
    if re.search(r'с\s+(?:поврежден\w*|лопнув\w*|сломан\w*)\s+(?:крышк|упаковк|дозатор)', n):
        events.add('BROKEN_DISPENSER' if 'дозатор' in n else 'DAMAGE')
    if re.search(r'приш\w*\s+с\s+повреждени', n):
        events.add('DAMAGE')
    return events


def unsupported_statement(text, source, policy=None):
    """Check the triggering premise even inside a fixed approved response."""
    evidence = premises(source, policy or {'situations': {}})
    reported = {EVENT_FAMILIES[p['code']] for p in evidence}
    required = response_events(text)
    for event in sorted(required - reported):
        return event
    n = normalize(text)
    # The two unchanged ODR-08 templates acknowledge an unspecified reported
    # state. They cannot introduce a specific substitution/damage/missing item.
    generic = bool(re.search(r'(?:товар|покупка|заказ)\s+приш\w*\s+в\s+таком\s+(?:состоянии|виде)', n))
    if generic and not reported:
        return 'unreported_service_state'
    return None
