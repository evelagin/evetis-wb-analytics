from app.domain.models import make_doc_id
from app.services.pipeline import build_keyboard, parse_callback


def test_parse_new_format():
    assert parse_callback("pub:abc123") == ("pub", "abc123")
    assert parse_callback("regen:deadbeef") == ("regen", "deadbeef")


def test_parse_legacy_send_maps_to_pub():
    assert parse_callback("send_REVIEW1") == ("pub", "REVIEW1")


def test_parse_rejects_unknown_action_or_empty_id():
    assert parse_callback("nuke:abc") == (None, None)
    assert parse_callback("pub:") == (None, None)
    assert parse_callback("") == (None, None)


def test_doc_id_is_short_and_stable():
    a = make_doc_id("wb", "review", "some-long-wb-feedback-id-000000000000")
    b = make_doc_id("wb", "review", "some-long-wb-feedback-id-000000000000")
    assert a == b
    assert len(a) == 20


def test_callback_data_within_telegram_limit():
    doc_id = make_doc_id("wb", "review", "x" * 200)
    kb = build_keyboard(doc_id, show_full=True)
    for row in kb["inline_keyboard"]:
        for btn in row:
            assert len(btn["callback_data"].encode("utf-8")) <= 64
