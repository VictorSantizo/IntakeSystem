"""Fillable-PDF form values reach the classifier.

Production bug (2026-07-15): pdfplumber.extract_text() reads a page's content
stream, which for a fillable PDF holds only the printed LABELS. The values a
person typed live in the AcroForm fields and are drawn there by the viewer, so a
FILLED lease extracted byte-for-byte like its own blank template — the office's
executed leases were all classified and filed as "Blank Residential Rental
Agreement", losing tenant, rent and owner. classifier.extract_pdf_form_values
reads those field values and classify_document appends them to what it sends the
AI. These tests lock in that behaviour and its guards.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import classifier   # noqa: E402
from conftest import make_pdf   # a plain PDF with a real text layer, no form  # noqa: E402


# ---------------------------------------------------------------- form builder
#
# A hand-rolled PDF carrying a real AcroForm: one page whose CONTENT STREAM
# prints only labels, plus text-field widgets whose typed values live in /V.
# The whole point is the asymmetry — get_fields() sees the values, the page text
# does not — so the fixture reproduces the exact shape of the bug.

def _obj(n, body):
    return f"{n} 0 obj\n".encode() + body + b"\nendobj\n"


def make_form_pdf(path, fields, page_text="RESIDENT 1 Last Name First Name M.I."):
    """Write a single-page PDF with an AcroForm. `fields` is {name: value}; a
    value of None makes an unset field, "/Off" an unchecked box. Field values
    never appear in the page content stream — only `page_text` does."""
    field_objs, field_refs = [], []
    first_field_obj = 6
    for i, (name, value) in enumerate(fields.items()):
        onum = first_field_obj + i
        parts = [b"<< /Type /Annot /Subtype /Widget /FT /Tx",
                 b"/Rect [50 %d 300 %d]" % (700 - i * 20, 715 - i * 20),
                 b"/P 3 0 R", ("/T (%s)" % name).encode()]
        if value is not None:
            parts.append(("/V (%s)" % value).encode())
        field_objs.append(_obj(onum, b" ".join(parts) + b" >>"))
        field_refs.append(f"{onum} 0 R")

    annots = "[" + " ".join(field_refs) + "]"
    stream = ("BT /F1 10 Tf 40 750 Td (" +
              page_text.replace("(", "[").replace(")", "]") + ") Tj ET").encode()
    objs = [
        _obj(1, b"<< /Type /Catalog /Pages 2 0 R /AcroForm << /Fields ["
                + " ".join(field_refs).encode() + b"] >> >>"),
        _obj(2, b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>"),
        _obj(3, b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R "
                + ("/Annots %s >>" % annots).encode()),
        _obj(4, b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"),
        _obj(5, b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n"
                + stream + b"\nendstream"),
    ] + field_objs

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(objs, 1):
        offsets.append(len(out))
        out += obj
    xref = len(out)
    out += f"xref\n0 {len(objs)+1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {len(objs)+1} /Root 1 0 R >>\n"
            f"startxref\n{xref}\n%%EOF").encode()
    path = str(path)
    with open(path, "wb") as f:
        f.write(bytes(out))
    return path


# ---------------------------------------------------------------- fixture sanity

def test_fixture_has_the_asymmetry_that_caused_the_bug(tmp_path):
    """The fixture only proves anything if get_fields() sees the value while the
    page text does NOT — that gap is the entire bug."""
    from pypdf import PdfReader
    pdf = make_form_pdf(tmp_path / "lease.pdf", {"Residents1": "Lola J. Roa"})
    got = PdfReader(pdf).get_fields()
    assert got and got["Residents1"].get("/V") == "Lola J. Roa"
    page_text = classifier.extract_text_from_pdf(pdf)
    assert "Lola" not in page_text and "Roa" not in page_text


# ---------------------------------------------------------------- the extractor

def test_form_values_block_carries_marker_name_and_value(tmp_path):
    pdf = make_form_pdf(tmp_path / "lease.pdf",
                        {"Residents1": "Lola J. Roa", "MonthlyRent": "$2645.00"})
    block = classifier.extract_pdf_form_values(pdf)
    assert "[FILLED-IN FORM FIELDS" in block
    assert "Residents1" in block and "Lola J. Roa" in block
    assert "$2645.00" in block


def test_all_empty_form_reads_as_blank(tmp_path):
    """A genuinely blank template must still yield nothing — the case the fix
    must not break (it is what "Blank ..." is correctly for)."""
    pdf = make_form_pdf(tmp_path / "template.pdf",
                        {"Residents1": None, "MonthlyRent": None})
    assert classifier.extract_pdf_form_values(pdf) == ""


def test_plain_pdf_without_acroform_yields_nothing(tmp_path):
    pdf = make_pdf(str(tmp_path / "plain.pdf"))
    assert classifier.extract_pdf_form_values(pdf) == ""


def test_missing_and_garbage_files_never_raise(tmp_path):
    assert classifier.extract_pdf_form_values(str(tmp_path / "nope.pdf")) == ""
    junk = tmp_path / "junk.pdf"
    junk.write_bytes(b"%PDF-1.4\nnot really a pdf at all\n%%EOF")
    # Must return "", not raise — this runs on every PDF classification.
    assert classifier.extract_pdf_form_values(str(junk)) == ""


def test_off_checkboxes_excluded_real_value_kept(tmp_path):
    pdf = make_form_pdf(tmp_path / "mixed.pdf",
                        {"AgreeBox": "/Off", "Residents1": "Jane Tenant"})
    block = classifier.extract_pdf_form_values(pdf)
    assert "Jane Tenant" in block
    assert "/Off" not in block
    assert "AgreeBox" not in block


def test_field_limit_caps_runaway_forms(tmp_path):
    over = classifier._FORM_FIELD_LIMIT + 40
    fields = {f"f{i}": f"value {i}" for i in range(over)}
    block = classifier.extract_pdf_form_values(pdf := make_form_pdf(
        tmp_path / "big.pdf", fields))
    rendered = [ln for ln in block.splitlines() if ln.startswith("f")]
    assert len(rendered) <= classifier._FORM_FIELD_LIMIT


# ---------------------------------------------------------------- integration

def test_classify_document_passes_form_values_to_the_ai(tmp_path, monkeypatch):
    pdf = make_form_pdf(tmp_path / "lease.pdf",
                        {"Residents1": "Lola J. Roa", "MonthlyRent": "$2645.00"},
                        page_text="RESIDENTIAL RENTAL AGREEMENT RESIDENT 1 Last Name")
    seen = {}

    # classify_document hands `text` and `form_values` to classify_with_claude
    # as SEPARATE arguments (P1-5 fix) rather than one pre-concatenated blob,
    # so classify_with_claude can truncate `text` alone before appending
    # form_values to the prompt. The fake here mirrors that real signature.
    def fake_classify(text, config, vendor_hint="", image_path=None,
                      image_media=None, image_paths=None, form_values=""):
        seen["text"] = text
        seen["form_values"] = form_values
        return {"doc_type": "Legal", "vendor": "Residential Rental Agreement - Lola J. Roa",
                "ai_confidence": "High"}

    monkeypatch.setattr(classifier, "classify_with_claude", fake_classify)
    result = classifier.classify_document(pdf)
    # Both the page text and the typed values must reach the model.
    assert "RESIDENTIAL RENTAL AGREEMENT" in seen["text"]
    assert "Lola J. Roa" in seen["form_values"] and "$2645.00" in seen["form_values"]
    assert "Lola J. Roa" in result["extracted_text"]


def test_long_document_text_does_not_crowd_out_form_values(tmp_path, monkeypatch):
    """Regression for P1-5 (2026-07-25 code review): classify_document used to
    build `text + form_values` and hand classify_with_claude ONE combined
    string, which then truncated it to 4000 chars for the prompt. Any document
    whose own text was already >=4000 chars — exactly the long leases this
    form-values feature targets — had the whole [FILLED-IN FORM FIELDS] block
    sliced off before Claude ever saw it, silently reintroducing "executed
    lease classified as Blank". The fix passes `text` and `form_values` to
    classify_with_claude as separate arguments so it can truncate `text` alone
    and append form_values afterward. This proves that handoff: the document's
    own text here is deliberately >=4000 chars, and the form values must still
    arrive whole and unclipped."""
    long_page_text = ("RESIDENTIAL RENTAL AGREEMENT " +
                       "TERM AND CONDITIONS CLAUSE OF THE LEASE AGREEMENT " * 90)
    assert len(long_page_text) >= 4000        # sanity: the fixture is actually long
    pdf = make_form_pdf(tmp_path / "long_lease.pdf",
                        {"Residents1": "Lola J. Roa", "MonthlyRent": "$2645.00"},
                        page_text=long_page_text)

    seen = {}

    def fake_classify(text, config, vendor_hint="", image_path=None,
                      image_media=None, image_paths=None, form_values=""):
        seen["text"] = text
        seen["form_values"] = form_values
        return {"doc_type": "Legal", "vendor": "Residential Rental Agreement - Lola J. Roa",
                "ai_confidence": "High"}

    monkeypatch.setattr(classifier, "classify_with_claude", fake_classify)
    classifier.classify_document(str(pdf))

    # The document's own text is long enough to trigger the old bug...
    assert len(seen["text"]) >= 4000
    # ...yet the form-values block reaches classify_with_claude whole, as its
    # OWN argument — not appended-then-truncated away inside `text`.
    assert "[FILLED-IN FORM FIELDS" in seen["form_values"]
    assert "Lola J. Roa" in seen["form_values"]
    assert "$2645.00" in seen["form_values"]
    assert "Lola J. Roa" not in seen["text"]    # confirms it did NOT get folded into text


def test_form_values_do_not_defeat_the_image_only_gate(tmp_path, monkeypatch):
    """A scan with NO page text but WITH form fields must still take the vision
    path — the fix must not make the empty-text gate think there's a text layer.
    (A pure image scan carrying a stray AcroForm is unusual but must stay safe.)"""
    from PIL import Image

    # A form PDF whose page prints NOTHING, so extract_text is empty, but which
    # still has a filled field.
    pdf = make_form_pdf(tmp_path / "scan.pdf", {"Residents1": "Ghost Tenant"},
                        page_text="")
    assert classifier.extract_text_from_pdf(pdf).strip() == ""

    def fake_render(path, page_index):
        img = tmp_path / f"p{page_index}.png"
        Image.new("RGB", (20, 20), "white").save(img)
        return str(img)

    monkeypatch.setattr(classifier, "render_pdf_page_image", fake_render)
    monkeypatch.setattr(classifier, "render_last_page_image", fake_render)
    monkeypatch.setattr(classifier, "pdf_page_count", lambda p: 1)

    took = {}

    def fake_classify(text, config, vendor_hint="", image_path=None,
                      image_media=None, image_paths=None):
        took["via_images"] = bool(image_paths)
        return {"doc_type": "Other", "ai_confidence": "High"}

    monkeypatch.setattr(classifier, "classify_with_claude", fake_classify)
    result = classifier.classify_document(pdf)
    assert took["via_images"] is True
    assert result["extracted_text"] == "[scanned PDF – read via vision]"
