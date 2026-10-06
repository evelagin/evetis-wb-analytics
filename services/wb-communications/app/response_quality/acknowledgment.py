"""Actual mixed-feedback experience, rather than commentary on a property's value."""
import re
from app.v3.text import normalize


def positive_texture_experience(text):
    n=normalize(text or '')
    for clause in re.split(r'[.!?;]|\b(?:но|однако|а)\b',n):
        if not re.search(r'текстур|впитыва',clause):continue
        if re.search(r'важн\w*|имеет\s+значение|мы\s+стремим',clause) and not re.search(r'вам\s+понрав|вы\s+оцен',clause):continue
        positive=re.search(r'понрав|оценил|оценит|оценили|приятн|довольн|комфорт',clause)
        attribution=re.search(r'вам|у\s+вас|вы\s+(?:отметили|оценили|довольны)|ваш\w*\s+(?:ощущени|впечатлен)',clause)
        if positive and attribution and not re.search(r'\bне\s*$',clause[max(0,positive.start()-5):positive.start()]):return True
    return False
