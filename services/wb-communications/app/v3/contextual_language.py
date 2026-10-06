"""Bounded subject/object, modality and customer provenance checks.

These helpers exempt a matched predicate only, never a sentence or a rule family.
No product benefit can be derived from customer testimony.
"""
import re
from app.v3.text import normalize

PRODUCT = r'крем\w*|сыворот\w*|тоник\w*|средств\w*|кислот\w*|ингредиент\w*|формул\w*|состав\w*|продукт\w*'
FEEDBACK = r'отзыв\w*|впечатлен\w*|ответ\w*|уточнен\w*|обратн\w*\s+связ\w*'
ATTRIBUTION = r'вы\s+(?:отметили|рассказали)|по\s+вашим\s+ощущениям|(?:вам|у\s+вас)[^.!?;]{0,35}(?:показал|понрав|ощущ)|(?:показал|понрав)\w*[^.!?;]{0,25}вам|рады,?\s+что[^.!?;]{0,100}(?:вам|у\s+вас)'


def clause_at(text, match):
    start = max(text.rfind(c, 0, match.start()) for c in '.!?;') + 1
    ends = [text.find(c, match.end()) for c in '.!?;']
    end = min([e for e in ends if e >= 0], default=len(text))
    return text[start:end], text[end:end+1]


def proposition_at(text, match):
    """Bind attribution to a proposition, retaining ordinary subordinate commas."""
    clause, punctuation = clause_at(text, match)
    sentence_start = max(text.rfind(c, 0, match.start()) for c in '.!?;') + 1
    independent = (r'(?:у\s+(?:' + PRODUCT + r')|(?:' + PRODUCT +
                   r')(?!\s+(?:у\s+вас|вам|ваш\w*))|он\b|она\b|оно\b|поэтому\b)')
    boundary = r',\s*(?:(?:и|но|а|при этом)\s+)?(?=' + independent + r')|\b(?:и|но|а)\s+(?=' + independent + r')'
    starts = [0] + [m.end() for m in re.finditer(boundary, clause)]
    local = match.start() - sentence_start
    start = max(x for x in starts if x <= local)
    end = min((x for x in starts if x > local), default=len(clause))
    return clause[start:end], punctuation


def feedback_help(text, match):
    if not re.fullmatch(r'помо(?:га\w*|же\w*)', match.group()):
        return False
    clause, _ = clause_at(text, match)
    prefix = text[max(0, match.start()-220):match.start()]
    local_prefix = clause[:clause.find(match.group())]
    subject = re.search(r'\b(это|он|' + FEEDBACK + r')\s*$', local_prefix)
    if not subject:
        return False
    s = subject.group(1)
    # Pronouns resolve to the nearest named noun, not any feedback noun upstream.
    if s == 'он':
        nouns = list(re.finditer(r'\b(?:' + PRODUCT + '|' + FEEDBACK + ')', prefix[:-len(s)]))
        if not nouns or not re.fullmatch(FEEDBACK, nouns[-1].group()):
            return False
    if s == 'это' and re.search(r'\b(?:' + PRODUCT + ')', local_prefix):
        return False
    tail = text[match.end():match.end()+140].split('.')[0].split(';')[0]
    understanding = re.match(r'\s+(?:нам\s+)?(?:(?:точнее|лучше)\s+)?(?:понять|разобраться\s+в)\s+(?:ваш\w*\s+)?(?:впечатлен\w*|опыт\w*|ожидан\w*)', tail)
    improving = re.match(r'\s+нам\s+(?:становиться|стать)\s+лучше\b', tail)
    return bool(understanding or (improving and s != 'это'))


def wish_frequency(text, match):
    clause, _ = clause_at(text, match)
    # Wish marker + affective/pleasant-care predicate. Application imperatives,
    # recommendations and efficacy remain assertions even after "пусть".
    return bool(re.search(r'\bпусть\b', clause) and re.search(
        r'рад\w*|приятн\w*\s+(?:част\w*|дополн\w*)[^.!?]{0,35}уход', clause) and not re.search(
        r'использ|нанос|нанес|примен|рекоменд|совету|нужно|следует|долж|увлажня|леч|защища|обеспеч|помога', clause))


FEATURES = {
    'pleasant': r'приятн\w*|нрав\w*|понрав\w*',
    'fresh': r'свеж\w*',
    'harsh': r'резк\w*',
    'airy': r'воздушн\w*',
    'fast_absorption': r'быстр\w*\s+впиты\w*',
    'good_absorption': r'хорошо\s+впиты\w*',
    'non_sticky': r'без\s+липк\w*|не\s+лип\w*|не\s+оставля\w*[^.!?]{0,35}липк\w*',
    'no_greasy_film': r'(?:без|не\s+оставля\w*)[^.!?]{0,35}жирн\w*\s+пленк\w*',
}


def reported_features(source):
    n = normalize(source)
    result = []
    for key, pattern in FEATURES.items():
        for m in re.finditer(pattern, n):
            before = n[max(0,m.start()-8):m.start()]
            if key not in {'non_sticky','no_greasy_film'} and re.search(r'\bне\s*$',before):
                continue
            clause, _ = clause_at(n,m)
            if key in {'pleasant','fresh','harsh','airy'} and not re.search(r'аромат|запах|пах',clause):
                continue
            result.append(key)
    return sorted(set(result))


def attributed_match(text, match, source, *, fragrance=False):
    clause, _ = proposition_at(text,match)
    # Clause scope cannot rescue a second, independent universal assertion.
    local = re.split(r'[,;]|\b(?:но|а|и)\s+(?:крем|средство|сыворотка|тоник)\b',clause)[-1]
    frame = re.search(ATTRIBUTION,clause)
    if not frame or re.search(r'всем|всегда|гарант|обеспеч|помога|леч|формул|состав',clause):
        return False
    if local != clause and re.search(r'\b(?:крем\w*|средств\w*|сыворот\w*|тоник\w*)\b',local) and not re.search(ATTRIBUTION,local):
        # "Рады, что Вам понравилось, крем ..." is not customer provenance.
        if 'что' not in local and not local.lstrip().startswith('крем у вас'):
            return False
    grounded = set(reported_features(source))
    content = match.group()
    if fragrance:
        keys = {k for k in ('pleasant','fresh','harsh','airy') if re.search(FEATURES[k],content)}
        return bool(keys and keys <= grounded)
    if re.search(r'быстр',content): return 'fast_absorption' in grounded
    if 'жирн' in content: return 'no_greasy_film' in grounded
    return 'non_sticky' in grounded


def false_fast_attribution(text, source):
    if 'fast_absorption' in reported_features(source):
        return False
    for m in re.finditer(r'быстр\w*\s+впиты\w*',text):
        clause,_ = clause_at(text,m)
        parts=re.split(r'[,;]|\b(?:а|при этом)\b',clause)
        local=parts[-1]
        independent=local!=clause and re.match(r'\s*крем(?: для рук)? рассчитан',local)
        if re.search(ATTRIBUTION,clause) and not independent:
            return True
    return False
