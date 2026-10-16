"""Vision-based splitting + last-page stamp reading for image-only batches.

Covers the two 2026-07-07 fixes:
1. splitter: image-only batches (no text layer on any page) route to the
   contact-sheet vision boundary pass instead of the text pass, whose blank-page
   backstop collapsed every boundary to page 1; the blank-part filter is skipped
   for them (every image-only part has 0 extractable words).
2. classifier: multi-page vision-fallback documents send TWO images (first page
   + last page) so the RECEIVED stamp on the back of the last sheet is visible;
   a saturated-ink stamp region on the last page is cropped and zoomed.

The real 23-page test batch (Test Scans/06272026.pdf) contains real financial
data and stays out of the repo — these tests use synthetic PDFs and a fake
Anthropic client, like the rest of the suite.
"""
import os
import sys
import types

import pytest
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import classifier   # noqa: E402
import splitter     # noqa: E402


# ---------------------------------------------------------------- PDF builders

def _write_pdf(path, objs):
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs)+1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {len(objs)+1} /Root 1 0 R >>\n"
            f"startxref\n{xref}\n%%EOF").encode()
    with open(path, "wb") as f:
        f.write(bytes(out))
    return str(path)


def make_blank_pdf(path, pages=3):
    """A multi-page PDF with NO content streams — pdfplumber extracts no text
    from any page, exactly like a pure image scan with no OCR layer."""
    kids = " ".join(f"{3+i} 0 R" for i in range(pages))
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{kids}] /Count {pages} >>".encode(),
    ]
    for _ in range(pages):
        objs.append(b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] >>")
    return _write_pdf(path, objs)


def make_text_pdf(path, page_texts):
    """A multi-page PDF with a real text layer on each page."""
    n = len(page_texts)
    kids = " ".join(f"{4+i*2} 0 R" for i in range(n))
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{kids}] /Count {n} >>".encode(),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    for i, text in enumerate(page_texts):
        lines = []
        for j in range(0, len(text), 70):
            chunk = text[j:j+70].encode("latin-1", "replace")
            lines.append(b"(" + chunk.replace(b"(", b"[").replace(b")", b"]") + b") Tj T*")
        stream = b"BT /F1 10 Tf 40 750 Td 12 TL\n" + b"\n".join(lines) + b"\nET"
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                    f"/Resources << /Font << /F1 3 0 R >> >> "
                    f"/Contents {5+i*2} 0 R >>".encode())
        objs.append(b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n"
                    + stream + b"\nendstream")
    return _write_pdf(path, objs)


WORDY = ("INVOICE from ACME Utilities account 12-3456 amount due $147.48 "
         "due date 07/15/2026 service address 123 Example Ave please remit "
         "payment promptly thank you for your business today")


def _fake_anthropic(monkeypatch, reply_text, with_thinking=False):
    """Install a fake `anthropic` module whose messages.create returns
    reply_text (optionally preceded by a thinking block, like sonnet)."""
    calls = {}

    content = []
    if with_thinking:
        content.append(types.SimpleNamespace(type="thinking", thinking="..."))
    content.append(types.SimpleNamespace(type="text", text=reply_text))

    class _Messages:
        def create(self, **kw):
            calls["kwargs"] = kw
            return types.SimpleNamespace(content=list(content))

    class _Client:
        def __init__(self, api_key=None):
            self.messages = _Messages()

    fake = types.ModuleType("anthropic")
    fake.Anthropic = _Client
    monkeypatch.setitem(sys.modules, "anthropic", fake)
    return calls


# ------------------------------------------------------------- boundary parse

def test_parse_boundary_json_plain_and_fenced():
    assert splitter._parse_boundary_json("[1, 4, 7]", 10) == [0, 3, 6]
    assert splitter._parse_boundary_json('```json\n[1, 4, 7]\n```', 10) == [0, 3, 6]


def test_parse_boundary_json_clamps_and_ensures_page_zero():
    # out-of-range pages dropped, page 0 always present, duplicates removed
    assert splitter._parse_boundary_json("[4, 4, 99, 0]", 10) == [0, 3]
    assert splitter._parse_boundary_json("[]", 10) == [0]


def test_text_backstop_drops_blank_page_starts(monkeypatch):
    """The text pass still refuses to start a document on a near-blank page."""
    _fake_anthropic(monkeypatch, "[1, 2, 3]")
    pages = [WORDY, "", WORDY]     # model claims page 2 (blank) starts a doc
    starts = splitter.detect_boundaries_with_claude(pages, "key")
    assert starts == [0, 2]


# ------------------------------------------------------------- contact sheets

def test_build_contact_sheets_counts_and_cleanup(tmp_path):
    pdf = make_blank_pdf(tmp_path / "b.pdf", pages=3)
    sheets = splitter.build_contact_sheets(pdf)
    try:
        assert len(sheets) == 1
        assert Image.open(sheets[0]).size[0] > 100
    finally:
        for s in sheets:
            os.remove(s)

    pdf = make_blank_pdf(tmp_path / "c.pdf", pages=26)   # 12 per sheet -> 3
    sheets = splitter.build_contact_sheets(pdf)
    try:
        assert len(sheets) == 3
    finally:
        for s in sheets:
            os.remove(s)


def test_detect_boundaries_with_vision_reads_text_block(tmp_path, monkeypatch):
    """End-to-end with a fake client: sheets built, thinking block skipped,
    JSON answer parsed, temp sheets deleted."""
    pdf = make_blank_pdf(tmp_path / "v.pdf", pages=4)
    calls = _fake_anthropic(monkeypatch, "[1, 3]", with_thinking=True)
    starts = splitter.detect_boundaries_with_vision(pdf, 4, "key")
    assert starts == [0, 2]
    content = calls["kwargs"]["messages"][0]["content"]
    assert content[0]["type"] == "image"        # the contact sheet
    assert content[-1]["type"] == "text"        # the prompt


# ----------------------------------------------------------- split_if_needed

def test_image_only_batch_uses_vision_and_keeps_all_parts(tmp_path, monkeypatch):
    pdf = make_blank_pdf(tmp_path / "batch.pdf", pages=4)
    monkeypatch.setattr(splitter, "detect_boundaries_with_vision",
                        lambda p, n, k: [0, 2])
    monkeypatch.setattr(splitter, "detect_boundaries_with_claude",
                        lambda *a, **k: pytest.fail("text pass used for image-only batch"))
    parts = splitter.split_if_needed(pdf, "key")
    # split happened, and the 0-word parts were NOT set aside as blank
    assert len(parts) == 2
    assert all(os.path.exists(p) for p in parts)
    # original archived out of the watched folder
    assert not os.path.exists(pdf)
    assert os.path.exists(tmp_path / "_Split Originals" / "batch.pdf")


def test_text_batch_still_uses_text_pass_and_blank_filter(tmp_path, monkeypatch):
    pdf = make_text_pdf(tmp_path / "t.pdf", [WORDY, WORDY, "x"])
    monkeypatch.setattr(splitter, "detect_boundaries_with_claude",
                        lambda pages, key: [0, 1, 2])
    monkeypatch.setattr(splitter, "detect_boundaries_with_vision",
                        lambda *a, **k: pytest.fail("vision pass used for text batch"))
    parts = splitter.split_if_needed(pdf, "key")
    # part 3 ("x") is near-blank and gets set aside; the two wordy parts remain
    assert len(parts) == 2
    assert os.path.exists(tmp_path / "_Blank" / "t_part3.pdf")


# ------------------------------------------------- classifier two-image path

def test_classify_multipage_image_only_sends_first_and_last(tmp_path, monkeypatch):
    pdf = make_blank_pdf(tmp_path / "doc.pdf", pages=3)
    seen = {}

    def fake_classify(text, config, vendor_hint="", image_path=None,
                      image_media=None, image_paths=None):
        seen["image_paths"] = list(image_paths or [])
        assert all(os.path.exists(p) for p in seen["image_paths"])
        return {"doc_type": "Bill", "ai_confidence": "High"}

    monkeypatch.setattr(classifier, "classify_with_claude", fake_classify)
    result = classifier.classify_document(pdf)
    assert len(seen["image_paths"]) == 2
    assert result["extracted_text"] == "[scanned PDF – read via vision]"
    # temp images cleaned up afterwards
    assert not any(os.path.exists(p) for p in seen["image_paths"])


def test_classify_single_page_image_only_sends_one_image(tmp_path, monkeypatch):
    pdf = make_blank_pdf(tmp_path / "one.pdf", pages=1)
    seen = {}

    def fake_classify(text, config, vendor_hint="", image_path=None,
                      image_media=None, image_paths=None):
        seen["image_paths"] = list(image_paths or [])
        return {"doc_type": "Bill", "ai_confidence": "High"}

    monkeypatch.setattr(classifier, "classify_with_claude", fake_classify)
    classifier.classify_document(pdf)
    assert len(seen["image_paths"]) == 1


# --------------------------------------------------------- stamp ink detection

def _page_image(stamp=None, text_lines=20):
    """White 'page' with neutral gray text lines and optionally a red stamp."""
    img = Image.new("RGB", (1200, 1600), "white")
    px = img.load()
    for line in range(text_lines):                     # bleed-through-ish gray
        y = 60 + line * 70
        for x in range(100, 1100):
            px[x, y] = (140, 140, 140)
    if stamp:
        x0, y0, x1, y1 = stamp
        for x in range(x0, x1):
            for y in range(y0, y1):
                px[x, y] = (200, 40, 40)               # red ink
    return img


def test_stamp_ink_bbox_finds_red_ignores_gray():
    bbox, count = classifier._stamp_ink_bbox(_page_image(stamp=(700, 1000, 950, 1100)))
    assert bbox is not None and count > 400
    x0, y0, x1, y1 = bbox
    assert 690 <= x0 <= 710 and 990 <= y0 <= 1010
    assert 940 <= x1 <= 960 and 1090 <= y1 <= 1110


def test_stamp_ink_bbox_none_without_colored_ink():
    bbox, count = classifier._stamp_ink_bbox(_page_image(stamp=None))
    assert bbox is None and count == 0


def test_render_last_page_image_full_page_without_stamp(tmp_path):
    pdf = make_text_pdf(tmp_path / "plain.pdf", [WORDY, WORDY])
    img = classifier.render_last_page_image(pdf, 1)
    try:
        assert img is not None
        w, h = Image.open(img).size
        assert max(w, h) <= 1600          # whole page, clamped for upload
        assert h > w                       # still page-shaped, not a crop
    finally:
        os.remove(img)
