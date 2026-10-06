"""Independent, bounded provenance of assertions; model labels are not evidence.

A typed proposition has a source, subject, feature and strength. Expertise can
support a brand assertion but cannot upgrade a customer's personal testimony.
No rule here waives medical/numeric/safety/use checks or grants new knowledge.
"""
from dataclasses import dataclass,asdict
import re
from app.v3.text import normalize,find_literal_spans
from app.v3.contextual_language import FEATURES,PRODUCT,FEEDBACK,reported_features,clause_at,proposition_at

CUSTOMER_REPORTED='CUSTOMER_REPORTED'
VERIFIED_PRODUCT_FACT='VERIFIED_PRODUCT_FACT'
APPROVED_EXPERTISE='APPROVED_EXPERTISE'
APPROVED_SERVICE='APPROVED_SERVICE'
FREE_BRAND_VOICE='FREE_BRAND_VOICE'
UNSUPPORTED_BRAND_ASSERTION='UNSUPPORTED_BRAND_ASSERTION'
# Independent dimensions: good absorption provides no evidence about speed.
FEATURE_CONTRACT={k:{'dimension': 'absorption_speed' if k=='fast_absorption' else 'absorption_quality' if k=='good_absorption' else k,
                     'requires_customer_evidence':k} for k in FEATURES}
SECOND_PERSON=r'\b(?:вы|вам|вас|ваш\w*)\b'
SPEECH=r'вы\s+(?:отмет\w*|рассказ\w*|оцен\w*|почувств\w*)|по\s+ваш\w*\s+(?:ощущен\w*|опыт\w*)'
REACTION=r'рад\w*|приятн\w*|жаль|понима\w*'
UNIVERSAL=r'\bвсем\b|\bвсегда\b|гарант\w*|у\s+каждого\s+покупател\w*'
PRODUCT_RESET=r'(?:,\s*|\b(?:а|но|при этом)\s+)(?:'+PRODUCT+r')\b(?!\s+(?:у\s+вас|вам|ваш\w*))'
EQUIVALENCE=r'(?:это|оно|так\w*|описан\w*|именно\s+так)[^.!?;]{0,55}(?:совпал\w*|соответств\w*|ощу\w*|почувств\w*|отмет\w*)|(?:совпал\w*|соответств\w*)[^.!?;]{0,45}(?:опыт\w*|ощущен\w*)'

@dataclass
class Proposition:
    span:str
    provenance:str
    feature:str|None=None
    subject:str|None=None
    grounded:bool=False
    basis:str=''
    polarity:str='positive'
    start:int=0
    end:int=0


def customer_frame(clause):
    """Attribution binds a reported/subordinate observation, never a new universal."""
    if re.search(UNIVERSAL,clause):return False
    if re.search(SPEECH,clause):return True
    reaction=bool(re.search(r'(?:'+REACTION+r')[^.!?;]{0,30}\bчто\b',clause))
    perception=bool(re.search(r'показал\w*|оказал\w*|понрав\w*|ощущ\w*|остав\w*[^.!?;]{0,45}(?:впечатлен\w*|ощущен\w*)',clause))
    return bool((re.search(SECOND_PERSON,clause) and (reaction or perception))
                or (reaction and perception))



def feature_polarity(text,match,key):
    if key in {'non_sticky','no_greasy_film'}:return 'positive'
    return 'negative' if re.search(r'\bне\s*$',text[max(0,match.start()-8):match.start()]) else 'positive'


def evidence_features(source):
    n=normalize(source);evidence=set()
    for key,rx in FEATURES.items():
        if key=='pleasant':continue
        for m in re.finditer(rx,n):
            evidence.add((key,feature_polarity(n,m,key)))
    return evidence


def feature_observations(text,source):
    n=normalize(text);known=evidence_features(source);rows=[]
    for key,rx in FEATURES.items():
        # Positive affect (liked/pleasant) is acknowledgment, not a measurable
        # product property or an intensity upgrade. Existing fact/scope rules
        # still check aroma descriptors in the original text.
        if key=='pleasant':continue
        for m in re.finditer(rx,n):
            clause,_=proposition_at(n,m)
            # Product assertion after a separate conjunction is not rescued by
            # an earlier acknowledgment. A complement "... что крем у Вас ..."
            # stays attributed. Object identity/ingredient checks remain outside.
            attributed=customer_frame(clause) and not re.search(PRODUCT_RESET,clause)
            # An anaphoric equivalence inherits the adjacent assertion's actual
            # feature, not the customer's weaker adjective. This includes an
            # approved manufacturer proposition immediately before the link.
            end=min((x for x in (n.find('.',m.end()),n.find('!',m.end()),n.find('?',m.end())) if x>=0),default=len(n))
            # Bind an anaphoric link within the proposition or its immediately
            # following sentence, never arbitrary earlier product descriptions.
            next_end=min((x for x in (n.find('.',end+1),n.find('!',end+1),n.find('?',end+1)) if x>=0),default=len(n))
            tail=n[m.end():next_end]
            if re.search(EQUIVALENCE,tail) and re.search(SECOND_PERSON,tail):attributed=True
            if attributed:
                polarity=feature_polarity(n,m,key)
                supported=(key,polarity) in known
                rows.append(Proposition(m.group(),CUSTOMER_REPORTED,key,'customer experience',supported,'same feature/polarity in customer source' if supported else 'missing customer evidence for this dimension/strength',polarity,m.start(),m.end()))
    return rows


def testimony_violations(text,source):
    return [r for r in feature_observations(text,source) if not r.grounded]


def reported_match(text,match,source):
    """Only a supported matched feature receives the claim-family exception."""
    n=normalize(text)
    candidates=feature_observations(n,source)
    return any(r.grounded and r.start==match.start() and (r.span.startswith(match.group()) or match.group().startswith(r.span)) for r in candidates)


def brand_observations(text,ctx):
    n=normalize(text);reported=feature_observations(n,ctx.customer_experience);rows=[]
    supports=[]
    for source,wordings in [(VERIFIED_PRODUCT_FACT,ctx.verified_product_fact_texts),
                           (APPROVED_EXPERTISE,ctx.approved_explanation_texts+ctx.approved_guidance_texts),
                           (APPROVED_SERVICE,ctx.templates)]:
        for wording in wordings:
            supports.extend((a,b,source) for a,b in find_literal_spans(n,normalize(wording)))
    for key,rx in FEATURES.items():
        if key=='pleasant':continue
        for m in re.finditer(rx,n):
            if any(r.feature==key and r.start==m.start() for r in reported):continue
            clause,punctuation=clause_at(n,m)
            # Elicitation of a personal sensation asserts no brand property.
            if punctuation=='?' and re.search(SECOND_PERSON,clause):continue
            support=next((source for a,b,source in supports if a<=m.start() and m.end()<=b),None)
            rows.append(Proposition(m.group(),support or UNSUPPORTED_BRAND_ASSERTION,key,'product property',bool(support),'scoped source span' if support else 'brand assertion has no scoped supporting source',feature_polarity(n,m,key),m.start(),m.end()))
    return rows


def feedback_process(text,match):
    if not re.fullmatch(r'помо(?:га\w*|же\w*)',match.group()):return False
    prefix=text[max(0,match.start()-220):match.start()]
    subject=re.search(r'\b(это|он|она|оно|'+FEEDBACK+r')\s*$',prefix)
    if not subject:return False
    s=subject.group(1)
    if s in {'это','он','она','оно'}:
        antecedents=list(re.finditer(r'\b(?:'+PRODUCT+'|'+FEEDBACK+')',prefix[:subject.start()]))
        if antecedents:
            if not re.fullmatch(FEEDBACK,antecedents[-1].group()):return False
        elif s!='это':return False
    tail=text[match.end():match.end()+160].split('.')[0].split(';')[0]
    # Communicative recipient + a cognitive/brand process, no customer remedy.
    cognitive=re.match(r'\s+(?:нам\s+)?(?:(?:лучше|точнее)\s+)?(?:понять|разобраться\s+в)\s+(?:ваш\w*\s+)?(?:опыт\w*|ожидан\w*|впечатлен\w*)',tail)
    brand_process=re.match(r'\s+нам\s+(?:(?:становиться|стать)\s+лучше|быть\s+(?:внимательн\w*|точн\w*|лучше))\b',tail)
    return bool(cognitive or brand_process)


def audit(text,ctx):
    """Inspectable source classification; unmatched prose is not an approval."""
    n=normalize(text);rows=feature_observations(n,ctx.customer_experience)+brand_observations(n,ctx)
    for provenance,wordings in [(APPROVED_SERVICE,ctx.templates),(APPROVED_EXPERTISE,ctx.approved_explanation_texts+ctx.approved_guidance_texts)]:
        for wording in wordings:
            for a,b in find_literal_spans(n,normalize(wording)):
                rows.append(Proposition(n[a:b],provenance,subject='approved scoped proposition',grounded=True,basis='exact scoped source span'))
    for m in re.finditer(r'помо(?:га\w*|же\w*)',n):
        if feedback_process(n,m):rows.append(Proposition(m.group(),FREE_BRAND_VOICE,subject='feedback / brand process',grounded=True,basis='communicative recipient, no product benefit'))
    for wording in ctx.verified_product_fact_texts:
        for a,b in find_literal_spans(n,normalize(wording)):
            rows.append(Proposition(n[a:b],VERIFIED_PRODUCT_FACT,grounded=True,basis='verified public product wording'))
    for m in re.finditer(r'[^.!?;]+',n):
        if free_brand_voice(m.group()):
            rows.append(Proposition(m.group().strip(),FREE_BRAND_VOICE,subject='affective wish / acknowledgment',grounded=True,basis='non-factual emotional payload; no exemption of other rules',start=m.start(),end=m.end()))
    return [asdict(r) for r in rows]


# Source strength is relational: explicit intensification or removed hedging.
# No global sentiment score and no ordering of unrelated Russian adjectives.
LOW_DEGREE = r'\b(?:немного|слегка|чуть|довольно|несколько)\b'
HIGH_DEGREE = r'\b(?:очень|сильно|чрезвычайно|невыносимо|невероятно)\b'
ADJECTIVE_END = r'(?:ыми|ими|ого|его|ому|ему|ых|их|ым|им|ую|юю|ая|яя|ое|ее|ые|ие|ый|ий|ой)'


def adjective_root(word):
    base = re.sub(ADJECTIVE_END+'$', '', word)
    return re.sub(r'(?:оват|еват|еньк)$', '', base)


def strength_findings(text, source):
    """Confident new degree -> BLOCK; lost hedge alone -> diagnostic WARNING.

    Only related sensory predicates are compared. A different adjective alone
    is not proof of strengthening. Positive high excitement is separately bound
    to explicit customer experience, never inferred from rating.
    """
    n, src = normalize(text), normalize(source)
    rows = []
    for m in re.finditer(r'\b[а-я]+?'+ADJECTIVE_END+r'\b', n):
        root = adjective_root(m.group())
        if len(root) < 3:continue
        source_words = [x for x in re.finditer(r'\b[а-я]+\b', src) if adjective_root(x.group()) == root]
        if not source_words:continue
        clause, _ = clause_at(n, m)
        # Bind degree locally to the shared property, not an unrelated warm opener.
        before = n[max(0, m.start()-24):m.start()]
        high = bool(re.search(HIGH_DEGREE+r'\s*$', before))
        supported_high = any(re.search(HIGH_DEGREE+r'\s*$', src[max(0,x.start()-24):x.start()]) for x in source_words)
        hedged = any(re.search(r'(?:оват|еват|еньк)', x.group()) or re.search(LOW_DEGREE+r'\s*$', src[max(0,x.start()-24):x.start()]) for x in source_words)
        still_hedged = bool(re.search(r'(?:оват|еват|еньк)',m.group()) or re.search(LOW_DEGREE+r'\s*$',before))
        if high and not supported_high:
            rows.append(dict(severity='BLOCK',span=m.group(),basis='explicit degree exceeds customer evidence'))
        elif hedged and not still_hedged and customer_frame(clause):
            rows.append(dict(severity='WARNING',span=m.group(),basis='customer hedge not preserved; exact degree uncertain'))
    for m in re.finditer(r'\b(?:вы\s+(?:остал\w*\s+)?|остал\w*\s+)(?:в\s+(?:полном\s+)?восторге|безумно\s+довольн\w*)',n):
        if not re.search(r'\b(?:восторг\w*|обожа\w*|безумно\s+(?:понрав\w*|довольн\w*))',src):
            rows.append(dict(severity='BLOCK',span=m.group(),basis='strong customer excitement has no source testimony'))
    return rows


def free_brand_voice(clause):
    """Recognize bounded affect, never erase substantive payload from verifier.

    Every other rule checks original text. This boolean can only distinguish a
    generic pleasant-use wish from an application instruction, not grant facts.
    """
    n = normalize(clause)
    frame = re.search(r'\b(?:пусть|желаем|надеемся|будем рады|спасибо|благодарим)\b', n)
    affect = re.search(r'радовать|раду\w*|приятн\w*[^.!?;]{0,35}(?:ощущен\w*|эмоц\w*|впечатлен\w*|использован\w*)|удовольств\w*|видеть вас|возвращаетесь|поделились', n)
    substantive = re.search(r'леч\w*|воспален\w*|акне|барьер\w*|\bпор\w*|кислот\w*|состав\w*|содерж\w*|\b(?:кож\w*|нанос\w*|нанес\w*|втира\w*)|увлаж\w*|восстанов\w*|защит\w*|очист\w*|очища\w*|совмест\w*|подход\w*|\b(?:ежеднев\w*|дважды|утром|вечером|доз\w*|количеств\w*)|\d|%|держаться|избав\w*|уменьш\w*|гарант\w*', n)
    return bool(frame and affect and not substantive)


def reported_fragrance(text, match, source):
    """Customer recipient can own a past perceptual outcome as well as speech."""
    clause, _ = proposition_at(text, match)
    if not customer_frame(clause) or re.search(PRODUCT_RESET, clause):
        return False
    rows = feature_observations(text, source)
    if any(r.grounded and r.feature in {'pleasant','fresh','harsh','airy'}
           and match.start() <= r.start < match.end() for r in rows):return True
    # Descriptor evidence applies only to this attributed proposition.
    def descriptors(value):
        # Inflection normalization, not a synonym whitelist or new scent fact.
        return {re.sub(r'(?:ыми|ими|ого|его|ому|ему|ая|яя|ое|ее|ые|ие|ую|юю|ый|ий|ой|ым|им)$','',w)
                for w in re.findall(r'\b[а-яё-]+(?:ыми|ими|ого|его|ому|ему|ая|яя|ое|ее|ые|ие|ую|юю|ый|ий|ой|ым|им)\b',value)}
    wanted=descriptors(match.group())
    if not wanted:return False
    for customer_clause in re.split(r'[.!?;]',normalize(source)):
        if not re.search(r'аромат|запах',customer_clause):continue
        if re.search(r'\bли\b|если|возможно|не\s+знаю',customer_clause):continue
        if wanted <= descriptors(customer_clause):
            # Attribution does not authorize a higher degree or universalization.
            degree=set(re.findall(HIGH_DEGREE,clause))
            if degree <= set(re.findall(HIGH_DEGREE,customer_clause)):return True
    return False


# Recognition normalizes inflection into predicates already denied by the same
# policy. This is not a new claim approval, safety route or language generator.
PREDICATE_FORMS = (
    (r'\bубер(?:ет|ут|ешь|ете|ем|и|ите)\b', 'убирает'),
    (r'\bсним(?:ет|ут|ешь|ете|ем|и|ите)\b', 'снимает'),
    (r'\bустран(?:ит|ят|ил\w*|ить)\b', 'устраняет'),
    (r'\bочист(?:ит|ят|ил\w*|ить)\b', 'очищает'),
    (r'\bвосстанов(?:ит(?:ся)?|ят(?:ся)?|ил\w*|ить(?:ся)?)\b', 'восстанавливает'),
    (r'\bуменьш(?:ит|ат|ил\w*|ить)\b', 'уменьшает'),
)
FRAME = r'^\s*(?:пусть\b|надеемся,?\s+что\b|желаем(?:,?\s+чтобы)?\b)\s*'
APPLICATION = r'\b(?:нанос(?:ите|ить|ил\w*|им|ят|ит\w*)|нанес(?:ите|ти|ли|ет\w*)|использ(?:уйте|овать|овал\w*|ует\w*|уют\w*)|примен(?:яйте|ять|ял\w*|яет\w*|яют\w*)|распределите|втирайте)\b'
FREQUENCY = r'\b(?:дважды|трижды|ежеднев\w*|каждый\s+(?:день|вечер)|утром|вечером)\b|\b(?:два|три|\d+)\s+раз'


def semantic_segments(text):
    """Frame and payload are orthogonal; retain original text and offsets.

    Minimal predicate grammar. Substantive payload is never erased just because
    a frame is emotional. Unrecognized prose is not granted factual permission.
    """
    n = normalize(text)
    rows = []
    for m in re.finditer(r'[^.!?;]+', n):
        segment = m.group(); frame = re.match(FRAME, segment)
        payload = segment[frame.end():] if frame else segment
        canonical = payload
        for rx, verb in PREDICATE_FORMS:
            canonical = re.sub(rx, verb, canonical)
        predicates = []
        action = re.search(APPLICATION, payload)
        schedule = re.search(FREQUENCY, payload)
        # Generic pleasure of use is affect; regimen or substantive payload is not.
        if action and not free_brand_voice(segment):
            predicates.append({'kind':'PRODUCT_USE_CLAIM','span':payload.strip()})
        elif schedule and re.search(r'нанесени\w*|нанос\w*|использован\w*|применени\w*',payload):
            predicates.append({'kind':'PRODUCT_USE_CLAIM','span':payload.strip()})
        if re.search(r'\b(?:достаточно|нужно|следует)\s+(?:небольш\w*|мал\w*|немного|количеств\w*)',payload):
            predicates.append({'kind':'PRODUCT_USE_CLAIM','span':payload.strip()})
        aroma = bool(re.search(r'аромат\w*|запах\w*|парфюмерн\w*|насыщенност\w*',payload))
        # Persistence/physical causality/population preference are not A1 meaning.
        performance = ((aroma or re.search(r'\bэффект\w*',payload)) and re.search(r'\b(?:держ\w*|сохран\w*|стойк\w*)',payload))
        expansion = aroma and (re.search(r'завис\w*|раскрыва\w*[^.!?;]{0,60}(?:из-за|благодаря)|(?:нрав\w*|подход\w*)[^.!?;]{0,30}(?:большинств\w*|всем)',payload))
        if performance:predicates.append({'kind':'PRODUCT_PERFORMANCE_CLAIM','span':payload.strip()})
        if expansion:predicates.append({'kind':'FACTUAL_EXPANSION','span':payload.strip()})
        # Approved absorption describes design, not any new persistence promise.
        if re.search(r'впитыв\w*|впитыва\w*',payload) and re.search(r'весь\s+день|всегда|\d+\s+час',payload):
            predicates.append({'kind':'PRODUCT_PERFORMANCE_CLAIM','span':payload.strip()})
        rows.append({'segment':segment.strip(),'start':m.start(),'end':m.end(),
                     'frame':FREE_BRAND_VOICE if frame else None,'payload':payload.strip(),
                     'non_substantive':free_brand_voice(segment) and not predicates and canonical==payload,
                     'canonical':canonical.strip(),'normalized_predicate':canonical!=payload,
                     'predicates':predicates})
    return rows


def fragrance_perception_spans(text):
    """Recognize only the owner's perception-individuality relation, not scope.

    Scope/approval is imposed by the caller's immutable registry row. Physical
    scent behavior, skin mechanisms and population preferences are not included.
    """
    n = normalize(text)
    fragrance = r'(?:аромат(?:а|ов|ы)?|запах(?:а|ов|и)?|парфюмерн\w*\s+композиц\w*)'
    modifiers = r'(?:(?:действительно|очень|тоже)\s+)*'
    individuality = r'(?:индивидуально|у\s+каждого(?:\s+человека)?\s+(?:может\s+быть\s+)?(?:свое|разным))'
    patterns = [
        r'\bвосприятие\s+'+fragrance+r'\s+'+modifiers+individuality+r'\b',
        r'\b'+fragrance+r'\s+'+modifiers+r'воспринима(?:ется|ются|ем)\s+'+modifiers+r'(?:индивидуально|по-разному|по-своему)\b',
    ]
    # A pronoun must resolve to the adjacent fragrance noun, not a cream/skin.
    pronoun=r'\bего\s+восприятие\s+'+modifiers+individuality+r'\b'
    out=[m.group() for rx in patterns for m in re.finditer(rx,n)]
    for m in re.finditer(pronoun,n):
        prefix=n[max(0,m.start()-180):m.start()]
        nouns=list(re.finditer(r'\b(?:аромат\w*|запах\w*|крем\w*|средств\w*|сыворот\w*|кож\w*|текстур\w*)',prefix))
        if nouns and re.fullmatch(r'аромат\w*|запах\w*',nouns[-1].group()) and len(re.findall(r'[.!?;]',prefix[nouns[-1].end():]))<=1:
            out.append(m.group())
    return out



def approved_fragrance_concept(text, start, end, approved_meanings):
    """An abstract perception subject is not an INCI presence assertion.

    Only the ambiguous composition noun in a scoped approved meaning is typed
    as fragrance. Surrounding composition statements and other ingredients keep
    their original checks; a meaning cannot grant composition provenance.
    """
    n = text  # already normalized/blanked by verifier; preserve offsets
    if not re.fullmatch(r'парфюм\w*\s+композиц\w*',n[start:end]):return False
    clause_start=max(n.rfind(x,0,start) for x in '.!?;')+1
    clause_end=min((p for x in '.!?;' if (p:=n.find(x,end))>=0),default=len(n))
    if re.search(r'в\s+состав|содерж\w*|\binci\b|ингредиент\w*|отдушк\w*|\bparfum\b',n[clause_start:clause_end]):return False
    return any(a<=start and end<=b for meaning in approved_meanings for a,b in find_literal_spans(n,normalize(meaning)))
