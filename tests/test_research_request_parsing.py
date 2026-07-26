"""parse_request(): split topic from format/length/extras directives.

Regression coverage for the exact production bug: "Camaro and make a 3page doc" was
searched LITERALLY because nothing ever separated the topic from the delivery directive.
"""

from __future__ import annotations

from skills.research.request_parsing import parse_request


def test_the_exact_production_bug_phrase_extracts_a_clean_topic():
    parsed = parse_request("Camaro and make a 3page doc")
    assert parsed.topic == "Camaro and"  # "and" is a stray conjunction, harmless leftover
    assert "page" not in parsed.topic.lower()
    assert "doc" not in parsed.topic.lower()
    assert parsed.target_pages == 3


def test_simple_topic_with_hyphenated_page_count():
    parsed = parse_request("Camaro, 3-page doc")
    assert parsed.topic == "Camaro"
    assert parsed.target_pages == 3
    assert parsed.length == "medium"


def test_page_count_with_a_space_instead_of_a_hyphen():
    parsed = parse_request("history of NASA 5 page report")
    assert parsed.topic == "history of NASA"
    assert parsed.target_pages == 5
    assert parsed.length == "detailed"


def test_plain_topic_with_no_directives_defaults_to_medium():
    parsed = parse_request("quantum computing")
    assert parsed.topic == "quantum computing"
    assert parsed.length == "medium"
    assert parsed.target_pages is None
    assert parsed.cover_letter_to is None


def test_brief_keyword_sets_length_and_is_stripped_from_the_topic():
    parsed = parse_request("give me a brief doc on Kenyan tax law")
    assert "brief" not in parsed.topic.lower()
    assert parsed.length == "brief"
    assert "tax law" in parsed.topic.lower()


def test_detailed_keyword_sets_length():
    parsed = parse_request("detailed report about the history of jazz")
    assert parsed.length == "detailed"
    assert "jazz" in parsed.topic.lower()


def test_explicit_page_count_overrides_a_conflicting_length_word():
    """A number is a firmer signal than a loose adjective -- 2 pages is brief even if the
    word "detailed" also appears somewhere in the same sentence."""
    parsed = parse_request("a detailed but only 2-page doc about Camaro")
    assert parsed.target_pages == 2
    assert parsed.length == "brief"


def test_cover_letter_request_is_extracted_with_its_addressee():
    parsed = parse_request("Camaro research with a cover letter to the hiring manager")
    assert parsed.cover_letter_to == "the hiring manager"
    assert "cover letter" not in parsed.topic.lower()
    assert "Camaro" in parsed.topic


def test_cover_letter_request_with_no_addressee_gets_a_sensible_default():
    parsed = parse_request("Camaro research with a cover letter")
    assert parsed.cover_letter_to == "the hiring team"


def test_no_cover_letter_requested_leaves_it_none():
    parsed = parse_request("Camaro research")
    assert parsed.cover_letter_to is None


def test_curly_quotes_are_normalized_same_as_the_intent_router():
    parsed = parse_request("what’s up with Camaro")
    assert "’" not in parsed.topic


def test_whitespace_only_input_yields_an_empty_topic():
    parsed = parse_request("   ")
    assert parsed.topic == ""


def test_prefix_framing_make_me_a_doc_about_topic():
    parsed = parse_request("make me a doc about the history of jazz")
    assert parsed.topic == "the history of jazz"


def test_prefix_framing_write_a_report_on_topic():
    parsed = parse_request("write a report on Kenyan tax law")
    assert parsed.topic == "Kenyan tax law"


def test_a_real_topic_word_matching_the_noun_survives_mid_sentence():
    """The trailing-only anchor is what protects this: "report" is real topic content
    here, not a deliverable-type directive, because it isn't at the end of the string."""
    parsed = parse_request("the Mueller report and its political fallout")
    assert "mueller report" in parsed.topic.lower()
