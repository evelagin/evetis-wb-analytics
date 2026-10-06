"""Bounded decision intent, with contextual references; no rewriting or retry."""
import re
from app.v3.text import normalize

SENSATIONS=r'липк\w*|сухост\w*|стянут\w*|стягив\w*|ощущени\w*'
PRIORITY=r'важнее|главн\w*|приоритет\w*|предпочит\w*'


def sensation_reference(text, context):
    n=normalize(text)
    if re.search(SENSATIONS,n):return True
    pronoun=re.search(r'\b(оно|она|он|это ощущение)\b',n)
    if not pronoun:return False
    previous=normalize(context).rstrip('.!? ').split('.')[-1]
    # The antecedent must be an actual reported sensation, with agreement.
    anchors=list(re.finditer(r'ощущение(?:\s+(?:сухости|липкости|стянутости))?|липкость|сухость|стянутость|крем|тоник|сыворотка|средство|кожа',previous))
    gender={'оно':{'ощущение'},'это ощущение':{'ощущение'},'она':{'липкость','сухость','стянутость'},'он':set()}
    eligible=[m for m in anchors if (m.group().split()[0] in gender[pronoun.group()]) or (pronoun.group() in {'оно','это ощущение'} and m.group().startswith('ощущение'))]
    if not eligible:return False
    # A later same-gender product/skin noun makes the pronoun ambiguous.
    competing={'она':{'сыворотка','кожа'},'оно':{'средство'},'это ощущение':set(),'он':{'крем','тоник'}}
    return not any(m.start()>eligible[-1].start() and m.group() in competing[pronoun.group()] for m in anchors)


def elicits(text, unknown, *, context=''):
    n=normalize(text)
    if re.search(PRIORITY,n):return False
    if unknown=='expected_result':
        target=bool(re.search(r'результат|эффект|измен|что',n))
        expectation=bool(re.search(r'ожид|ждали|ждал|рассчитыв|хотел|хотите|нужен',n))
        return target and expectation and bool(re.search(r'како\w*|что\b|чего\b',n))
    if unknown=='specific_difficulty':
        return bool(re.search(r'что\s+именно|в\s+чем|како\w*',n) and re.search(
            r'не\s+понрав|неудоб|трудност|затрудн|разочар|сложн|меша',n))
    if unknown=='sensation_timing':
        if re.search(r'\bгде\b|на\s+каком\s+участке|на\s+какой\s+области',n):return False
        anchor=sensation_reference(n,context)
        onset=bool(re.search(r'сразу|сначала|поначалу|вначале|после\s+нанесения|появля|возника',n))
        persistence=bool(re.search(r'сохраня\w*|остал\w*|остает\w*|прошл\w*|проход\w*|исчез\w*|держ\w*',n))
        time=bool(re.search(r'когда|как\s+долго|долго|дольше|спустя|через|врем\w*|позже|сразу',n))
        alternatives=bool(re.search(r'\bили\b',n))
        # Resolved transient vs persistent is a temporal answer even without a
        # named clock/duration; location, priority and merely occurrence are not.
        temporal=(time and (onset or persistence)) or (alternatives and persistence and (onset or re.search(r'прошл|проход|исчез',n)))
        return anchor and bool(temporal)
    return False


def meaningful_question(text, opportunities, *, context=''):
    return any(elicits(text,c['unknown'],context=context) for c in opportunities)


def check_questions(text, opportunities):
    corrections=[];retained=[];context=''
    for m in re.finditer(r'[^.!?]+[.!?]?',text or ''):
        sentence=m.group().strip()
        request='?' in sentence or bool(re.search(r'\b(?:подскажите|расскажите|уточните)\b',normalize(sentence)))
        if request and not meaningful_question(sentence,opportunities,context=context):
            corrections.append({'reason':'QUESTION_DOES_NOT_ELICIT_PLANNED_UNKNOWN','removed':sentence})
        else:
            retained.append(m.group())
        context=sentence
    # A retained question is byte-identical; no rephrasing and no generated replacement.
    return (''.join(retained).strip() if corrections else text),corrections
