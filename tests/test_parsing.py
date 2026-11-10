"""Tests for app/parsing_v2.py"""
import pytest
from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

from app.rag.ingest.parsing import (
    DOC_ID_RE,
    NUMBERED_RE,
    chunk_metadata,
    clean_text,
    guess_doc_type,
    is_list_item,
    normalize_date,
    validate,
)

# The document code we expect from each raw file (file name starts with its number).
EXPECTED_DOC_ID_BY_FILE_NUMBER = {
    "00": "HR-GOV-000",
    "01": "HR-LEAVE-001",
    "02": "HR-COMP-002",
    "03": "HR-EXIT-003",
    "04": "HR-WORK-004",
    "05": "HR-LIFE-005",
    "06": "HR-ETHICS-006",
    "07": "HR-POSH-007",
    "08": "IT-SEC-008",
    "09": "HR-PAY-009",
    "10": "HR-TALENT-010",
    "11": "FIN-ADMIN-011",
    "12": "HR-FORM-012",
    "13": "ORG-CORP-001",
}


# ---------------------------------------------------------------- small functions
def test_normalize_date_reads_day_first():
    # 01/02/2026 is 1 February in India, not 2 January.
    assert normalize_date("01/02/2026") == "2026-02-01"


@pytest.mark.parametrize("raw_text, expected", [
    ("2026-01-01", "2026-01-01"),        # already ISO
    ("1 January 2026", "2026-01-01"),
    ("15 Mar 2026", "2026-03-15"),
    ("  05-11-2026  ", "2026-11-05"),    # spaces around the date are ignored
])
def test_normalize_date_accepts_known_formats(raw_text, expected):
    assert normalize_date(raw_text) == expected


def test_normalize_date_returns_unknown_text_unchanged():
    assert normalize_date("to be announced") == "to be announced"


def test_clean_text_collapses_spaces_and_line_breaks():
    assert clean_text("Casual   leave\nis   10 days") == "Casual leave is 10 days"


@pytest.mark.parametrize("doc_id, expected_type", [
    ("HR-FORM-012", "form"),
    ("HR-GOV-000", "guide"),
    ("ORG-CORP-001", "reference"),
    ("HR-LEAVE-001", "policy"),
])
def test_guess_doc_type(doc_id, expected_type):
    assert guess_doc_type(doc_id) == expected_type


@pytest.mark.parametrize("text", ["HR-LEAVE-001", "IT-SEC-008", "FIN-ADMIN-011", "ORG-CORP-001"])
def test_doc_id_pattern_matches_all_code_styles(text):
    assert DOC_ID_RE.search(text)


@pytest.mark.parametrize("text, should_match", [
    ("1. First step", True),
    ("2) Second step", True),
    ("a) Option", True),
    ("(b) Option", True),
    ("2026 was a good year", False),     # a number with no dot is not a list
    ("Plain sentence.", False),
])
def test_numbered_pattern(text, should_match):
    assert bool(NUMBERED_RE.match(text)) == should_match


# ---------------------------------------------------------------- is_list_item
def make_paragraph(text, style_name="Normal", numbering_id=None):
    """Build a real python-docx paragraph in memory so is_list_item can be tested."""
    document = Document()
    paragraph = document.add_paragraph(text, style=style_name)
    if numbering_id is not None:
        # This is the XML Word writes for an automatic bullet/number.
        paragraph_properties = paragraph._p.get_or_add_pPr()
        numbering_properties = OxmlElement("w:numPr")
        numbering_id_element = OxmlElement("w:numId")
        numbering_id_element.set(qn("w:val"), str(numbering_id))
        numbering_properties.append(numbering_id_element)
        paragraph_properties.append(numbering_properties)
    return paragraph


def test_list_style_paragraph_is_list_item():
    paragraph = make_paragraph("Apply early", style_name="List Bullet")
    assert is_list_item(paragraph, "Apply early")


def test_typed_bullet_is_list_item():
    paragraph = make_paragraph("• Apply early")
    assert is_list_item(paragraph, "• Apply early")


def test_typed_number_is_list_item():
    paragraph = make_paragraph("1. Apply early")
    assert is_list_item(paragraph, "1. Apply early")


def test_automatic_numbering_is_list_item():
    paragraph = make_paragraph("Apply early", numbering_id=5)
    assert is_list_item(paragraph, "Apply early")


def test_numbering_id_zero_means_numbering_off():
    # Word uses numId 0 to say "no list here"; it must NOT count as a list item.
    paragraph = make_paragraph("Apply early", numbering_id=0)
    assert not is_list_item(paragraph, "Apply early")


def test_plain_paragraph_is_not_list_item():
    paragraph = make_paragraph("Employees must apply early.")
    assert not is_list_item(paragraph, "Employees must apply early.")


# ---------------------------------------------------------------- the 14 raw files
def test_all_fourteen_files_are_parsed(parsed_documents):
    assert len(parsed_documents) == 14


def test_each_file_has_the_expected_doc_id(parsed_documents):
    for document in parsed_documents:
        file_number = document["source"][:2]
        assert document["doc_id"] == EXPECTED_DOC_ID_BY_FILE_NUMBER[file_number], document["source"]


def test_doc_ids_are_unique(parsed_documents):
    all_doc_ids = [document["doc_id"] for document in parsed_documents]
    assert len(all_doc_ids) == len(set(all_doc_ids))


def test_policy_documents_are_approved_with_version_and_date(parsed_documents):
    for document in parsed_documents:
        if document["doc_type"] == "reference":
            continue                                   # the company profile has no status
        assert document["status"] == "Approved", document["doc_id"]
        assert document["version"], document["doc_id"]
        assert document["effective_date"] == "2026-01-01", document["doc_id"]
        assert document["owner"], document["doc_id"]


def test_company_profile_is_a_reference_document(parsed_documents):
    company_profile = next(d for d in parsed_documents if d["doc_id"] == "ORG-CORP-001")
    assert company_profile["doc_type"] == "reference"


def test_every_block_has_a_heading_path_and_valid_type(parsed_documents):
    allowed_block_types = {"heading", "text", "list_item", "table", "callout"}
    for document in parsed_documents:
        for block in document["blocks"]:
            assert block["type"] in allowed_block_types, document["doc_id"]
            assert block["heading_path"], f"{document['doc_id']} has a block with no heading path"


def test_every_table_has_one_sentence_per_row(parsed_documents):
    for document in parsed_documents:
        for block in document["blocks"]:
            if block["type"] == "table":
                assert len(block["lines"]) == len(block["rows"]), document["doc_id"]


def test_no_text_block_is_empty(parsed_documents):
    for document in parsed_documents:
        for block in document["blocks"]:
            if block["type"] == "table":
                continue                               # tables have rows/lines, not a "text" field
            assert block["text"].strip(), document["doc_id"]


def test_validate_reports_no_errors_for_any_document(parsed_documents):
    for document in parsed_documents:
        error_messages = [m for m in validate(document) if m.startswith("ERROR")]
        assert error_messages == [], f"{document['doc_id']}: {error_messages}"


def test_only_the_forms_file_has_placeholders(parsed_documents):
    # File 12 is a template, so blanks are expected there. All other files must be filled in.
    for document in parsed_documents:
        if document["doc_id"] == "HR-FORM-012":
            continue
        assert document["placeholders"] == [], f"{document['doc_id']}: {document['placeholders']}"


# ---------------------------------------------------------------- chunk_metadata
def test_chunk_metadata_never_contains_none_values(parsed_documents):
    # Pinecone rejects null metadata values.
    for document in parsed_documents:
        assert None not in chunk_metadata(document).values(), document["doc_id"]
