"""Verified fragrance wording keeps its approval in any grammatical case (regression vs main)."""
import pytest
from tests.quality.test_v31e_routing_relevance import ST,msg
from app.services.publication_policy import validate_for_publication

HAND,CHERRY,AMBER,SET='252442517','593111986','593111985','930334396'

@pytest.mark.parametrize('answer,nm',[
 ('Крем для рук с древесно-удовым ароматом.',HAND),('У крема древесно-удовый аромат.',HAND),
 ('Крем с вишнёвым ароматом.',CHERRY),('Нотки вишнёвого аромата.',CHERRY),
 ('В наборе крем для рук с древесно-удовым ароматом.',SET),
])
def test_inflected_verified_fragrance_passes(answer,nm):
 assert validate_for_publication(answer,msg('Отзыв',nm=nm),ST)['verdict']=='PASS'

@pytest.mark.parametrize('answer,nm',[
 ('Крем с вишнёвым ароматом.',HAND),             # another product's verified descriptor
 ('Крем с медовым ароматом.',CHERRY),             # unverified descriptor
 ('Крем с древесным ароматом.',HAND),             # shortened descriptor is not the approved wording
 ('Крем с насыщенным вишнёвым ароматом.',CHERRY), # extended descriptor
 ('Крем с ароматом Lost Cherry.',CHERRY),         # trademark variant stays blocked (ODR-02)
])
def test_unverified_or_altered_fragrance_still_blocked(answer,nm):
 assert validate_for_publication(answer,msg('Отзыв',nm=nm),ST)['verdict']=='BLOCK'



def test_known_gap_other_case_endings_documented():
 # KNOWN GAP (pre-existing in the v3.1E descriptor rule, absent from main): endings
 # outside ый|ий|ой|ая|ое|ые|ую|ым are not recognised as descriptors. Widening it
 # collides with -ие nouns («восприятие аромата») and A1 wording; separate owner task.
 assert validate_for_publication('Крем со сладким ароматом.',msg('Отзыв',nm=CHERRY),ST)['verdict']=='PASS'
