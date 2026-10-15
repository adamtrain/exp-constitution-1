import pytest

from reflective_stability.config import DOCUMENTS_DIR
from reflective_stability.edits import EditError, apply, locate, normalize, parse_response

DOCUMENT = (
    "## Being helpful\n\n"
    "Claude should be genuinely helpful to the people it works with.\n\n"
    "## Being safe\n\n"
    "Claude shouldn’t undermine appropriate human oversight of AI during the current period—"
    "even when it disagrees with a particular instance of oversight.\n"
)
CONSTITUTION = (DOCUMENTS_DIR / "claude_constitution.md").read_text()


def response(find, replace, description="Clarify a section."):
    return f"---CHANGE DESCRIPTION---\n{description}\n\n---FIND---\n{find}\n\n---REPLACE---\n{replace}\n"


def test_parse_strips_section_whitespace():
    edit = parse_response(response("old text", "new text"))
    assert (edit.description, edit.find, edit.replace) == ("Clarify a section.", "old text", "new text")


def test_parse_allows_empty_replace_as_deletion():
    assert parse_response(response("old text", "")).replace == ""


def test_parse_accepts_decorated_delimiters_and_fenced_sections():
    edit = parse_response("**---CHANGE DESCRIPTION---**\nWhy.\n**---FIND---**\n```\nold\n```\n**---REPLACE---**\nnew")
    assert (edit.find, edit.replace) == ("old", "new")


def test_parse_rejects_missing_or_repeated_delimiters():
    with pytest.raises(EditError, match="---REPLACE---"):
        parse_response("---CHANGE DESCRIPTION---\nx\n---FIND---\ny")
    with pytest.raises(EditError, match="exactly once"):
        parse_response(response("a", "b") + response("c", "d"))


def test_no_change_needs_both_sections():
    assert parse_response(response("[NO CHANGE]", "[NO CHANGE]")).is_no_change
    with pytest.raises(EditError):
        parse_response(response("[NO CHANGE]", "something"))


def test_exact_match():
    find = "Claude should be genuinely helpful"
    match = locate(DOCUMENT, find)
    assert match.strategy == "exact"
    assert DOCUMENT[match.start : match.end] == find


def test_exact_match_must_be_unique():
    with pytest.raises(EditError, match="2 locations"):
        locate(DOCUMENT, "## Being")


def test_normalized_match_handles_whitespace_quotes_and_dashes():
    match = locate(
        DOCUMENT, "Claude shouldn't undermine\nappropriate   human oversight of AI during the current period -- even"
    )
    assert match.strategy == "normalized"
    assert DOCUMENT[match.start : match.end] == (
        "Claude shouldn’t undermine appropriate human oversight of AI during the current period—even"
    )


def test_normalize_maps_back_without_swallowing_paragraph_breaks():
    text = "one\n\n- two"
    normal, starts, ends = normalize(text)
    assert normal == "one-two"
    assert text[starts[3] : ends[3]] == "-"


def test_fuzzy_match_tolerates_a_small_difference_mid_passage():
    find = (
        "Claude shouldn’t undermine approprate human oversight of AI during the current period—even when it disagrees"
    )
    match = locate(DOCUMENT, find)
    assert match.strategy == "fuzzy"
    assert DOCUMENT[match.start : match.end] == find.replace("approprate", "appropriate")


@pytest.mark.parametrize(
    "corrupt",
    [
        lambda p: p.replace("—", " -- "),
        lambda p: p.replace("’", "'"),
        lambda p: p.replace(" the ", " ", 1),
        lambda p: p.replace(" and ", " & ", 1),
    ],
)
def test_corrupted_find_is_placed_exactly_or_rejected_on_the_real_constitution(corrupt):
    paragraphs = [p for p in CONSTITUTION.split("\n\n") if 300 < len(p) < 2000 and CONSTITUTION.count(p) == 1][:40]
    for paragraph in paragraphs:
        find = corrupt(paragraph)
        start = CONSTITUTION.index(paragraph)
        try:
            match = locate(CONSTITUTION, find)
        except EditError:
            continue  # the model is asked to retry: acceptable
        assert (match.start, match.end) == (start, start + len(paragraph))


def test_unmatched_find_is_an_error():
    with pytest.raises(EditError, match="not found"):
        locate(DOCUMENT, "An entirely different sentence that appears nowhere at all in the text.")


def test_apply_replaces_only_the_match():
    match = locate(DOCUMENT, "genuinely helpful")
    edited = apply(DOCUMENT, match, "deeply helpful")
    assert "deeply helpful to the people" in edited
    assert edited.replace("deeply", "genuinely") == DOCUMENT
