"""A verified fact value is data; the customer answer is a sentence around it.

Only grammatical framing chosen by fact type. The frame adds no fact, benefit,
number, instruction or marketing; a value that is already a sentence is kept.
"""
import re
from app.v3.text import normalize

TYPE_GENITIVE = {'hand_cream': 'крема', 'body_cream': 'крема', 'cream': 'крема',
                 'serum': 'сыворотки', 'tonic': 'тоника', 'powder': 'пудры'}
FRAMES = {
    'product_identity': 'Это {value}.',
    'country_of_origin': 'Страна производства — {value}.',
    'manufacturer': 'Производитель — {value}.',
    'volume': 'Объём — {value}.',
    'shelf_life': 'Срок годности — {value}.',
    'pao': 'Срок использования по документации — {value}.',
    'storage': 'Условия хранения по документации: {value}.',
    'fragrance_longevity': 'Стойкость аромата по документации — {value}.',
}
DEFAULT_FRAME = 'По документации: {value}.'


def is_sentence(value):
    v = (value or '').strip()
    return bool(v) and v[0].isupper() and v[-1] in '.!?'


def compose(fact, snapshot):
    """Customer sentence for one ResolvedFact; never alters the verified value."""
    value = (fact.customer_value_ru or '').strip()
    if not value or is_sentence(value):
        return value
    value = value.rstrip('.')
    if fact.fact_type == 'fragrance_profile':
        product = snapshot.product(fact.product_id) or {}
        genitive = TYPE_GENITIVE.get(product.get('product_type'))
        return f'У этой версии {genitive} — {value}.' if genitive else DEFAULT_FRAME.format(value=value)
    frame = FRAMES.get(fact.fact_type, DEFAULT_FRAME)
    # Combined mass/package rows are not a single volume value.
    if fact.fact_type == 'volume' and not all(i.endswith('.volume') for i in fact.fact_ids):
        frame = DEFAULT_FRAME
    return frame.format(value=value)


def fact_fragment_only(text, values=()):
    """FACT_FRAGMENT_ONLY: a bare data fragment presented as the answer.

    A sentence that starts lowercase or with a digit, or a whole answer equal to
    one fact value («Китай.», «вишнёвый аромат.», «150 мл.»).
    """
    text = (text or '').strip()
    if not text:
        return False
    for sentence in re.findall(r'[^.!?]+[.!?]?', text):
        first = next((c for c in sentence if c.isalnum()), '')
        if first and (first.islower() or first.isdigit()):
            return True
    whole = normalize(text).rstrip('.!? ')
    # A value that is itself a complete sentence is a complete answer.
    return any(whole == normalize(v).rstrip('.!? ') for v in values if v and not is_sentence(v))
