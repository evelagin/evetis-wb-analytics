from app.utils.text import (
    WB_ANSWER_MAX_LEN,
    clean_answer,
    escape_html,
    is_within_wb_limit,
    truncate,
)


def test_escape_html_neutralizes_markup():
    assert escape_html("<b> & </b>") == "&lt;b&gt; &amp; &lt;/b&gt;"


def test_escape_html_handles_none_and_numbers():
    assert escape_html(None) == ""
    assert escape_html(5) == "5"


def test_truncate_adds_ellipsis():
    assert truncate("abcdef", 4) == "abc…"
    assert truncate("abc", 10) == "abc"


def test_wb_limit():
    assert is_within_wb_limit("hello")
    assert not is_within_wb_limit("")
    assert not is_within_wb_limit("x" * (WB_ANSWER_MAX_LEN + 1))


def test_clean_answer_strips_quotes_and_space():
    assert clean_answer('  "текст ответа"  ') == "текст ответа"
