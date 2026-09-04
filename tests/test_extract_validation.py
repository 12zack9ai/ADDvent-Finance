"""Guards on the extraction payload.

These exist because of a real failure, not a hypothetical one. With
`"strict": True` on the record_document tool, a real New Castle quote came back
with every value shifted one key late and fragments of the tool-call format
inside the strings - and `lines` empty. Nothing raised. An invoice with no lines
compares as clean against any quote, so the system would have reported a
perfectly-priced bill rather than a broken read.

The tool is no longer strict. These tests make sure that if anything ever
produces that shape again, it fails loudly instead.
"""
import pytest

from app.extract import ExtractionError, validate_payload

MARKUP = "</" + "antml:parameter>\n<param" + "eter name=\"currency\">"


def _good(**over):
    payload = {
        "doc_type": "quote",
        "vendor": "New Castle Building Products",
        "document_number": "07RM0002885432",
        "lines": [{"sku": "GAFT3PG", "qty": "80", "uom": "SQ",
                   "unit_price": "120.50", "price_uom": "SQ", "extended": "9640.00"}],
    }
    payload.update(over)
    return payload


def test_clean_payload_passes():
    assert validate_payload(_good())["vendor"] == "New Castle Building Products"


def test_markup_leak_in_a_value_is_rejected():
    with pytest.raises(ExtractionError, match="malformed"):
        validate_payload(_good(total=MARKUP))


def test_markup_leak_nested_in_a_line_is_rejected():
    """The leak showed up inside line items too, not only at the top level."""
    bad = _good()
    bad["lines"][0]["extended"] = MARKUP
    with pytest.raises(ExtractionError, match="malformed"):
        validate_payload(bad)


@pytest.mark.parametrize("doc_type", ["quote", "invoice", "QUOTE", " Invoice "])
def test_priced_document_with_no_lines_is_rejected(doc_type):
    """The dangerous case: no lines means nothing to compare, which reads clean."""
    with pytest.raises(ExtractionError, match="no line items"):
        validate_payload(_good(doc_type=doc_type, lines=[]))


def test_unpriced_document_with_no_lines_is_allowed():
    """A statement or covering letter legitimately has no line items."""
    validate_payload(_good(doc_type="other", lines=[]))


def test_lines_of_the_wrong_shape_are_rejected():
    with pytest.raises(ExtractionError, match="malformed"):
        validate_payload(_good(lines="GAFT3PG 80 SQ"))


def test_non_dict_payload_is_rejected():
    with pytest.raises(ExtractionError, match="not a document"):
        validate_payload(["doc_type", "quote"])


def test_the_actual_mangled_payload_shape_is_rejected():
    """Reproduces the observed failure verbatim: values one key late, lines empty."""
    observed = {
        "doc_type": "quote",
        "vendor": "New Castle Building Products",
        "document_number": "07RM0002885432",
        "document_date": "2026-09-02",
        "due_date": "</" + "antml:parameter>\n<param" + "eter name=\"currency\">",
        "lines": [],
        "total": "</" + "antml:parameter>\n<param" + "eter name=\"job_number_hint\">63 winding ridge",
    }
    with pytest.raises(ExtractionError):
        validate_payload(observed)
