"""Text extraction + OCR helper (Option B). The OCR path needs the Tesseract
binary (a server install), so here we test the text-layer path and the graceful
degradation when Tesseract isn't present."""
import ocr

from conftest import make_pdf


def test_extract_text_layer_pdf(tmp_path):
    pdf = make_pdf(str(tmp_path / "bill.pdf"))
    text, method = ocr.extract_document_text(pdf)
    assert method == "text" and "INVOICE" in text.upper()


def test_degrades_without_tesseract(tmp_path, monkeypatch):
    monkeypatch.setattr(ocr, "find_tesseract", lambda p="": "")   # force "not installed"
    assert ocr.tesseract_available() is False
    # an image-only scan can't be read yet, but nothing crashes
    img = tmp_path / "scan.jpg"
    img.write_bytes(b"\xff\xd8\xff\xe0not-a-real-jpeg")
    assert ocr.extract_document_text(str(img)) == ("", "ocr-unavailable")
    assert ocr.ocr_pdf(make_pdf(str(tmp_path / "p.pdf"))) == ("", None)
    # a text-layer PDF still indexes fine without OCR
    text, method = ocr.extract_document_text(make_pdf(str(tmp_path / "t.pdf")))
    assert method == "text" and text


def test_find_tesseract_prefers_config_path(tmp_path):
    fake = tmp_path / "tesseract.exe"
    fake.write_text("x")
    assert ocr.find_tesseract(str(fake)) == str(fake)
    # a configured-but-missing path is ignored, never returned
    assert ocr.find_tesseract(str(tmp_path / "missing.exe")) != str(tmp_path / "missing.exe")


def test_missing_file_is_none(tmp_path):
    assert ocr.extract_document_text(str(tmp_path / "nope.pdf")) == ("", "none")


def test_ocr_pdf_streams_pages_instead_of_holding_all_in_memory(tmp_path, monkeypatch):
    """2026-07-27 code review: ocr_pdf used to rasterize every page into a PIL
    image list before OCR'ing any of them -- a 50-page scan at 200 dpi held
    ~1-2 GB of bitmaps in memory at once. Must now render-then-OCR-then-discard
    one page at a time: a render is immediately followed by its own OCR call,
    not by the next page's render."""
    import fitz

    doc = fitz.open()
    doc.new_page()
    doc.new_page()
    pdf_path = str(tmp_path / "multi.pdf")
    doc.save(pdf_path)
    doc.close()

    monkeypatch.setattr(ocr, "find_tesseract", lambda p="": "/fake/tesseract")

    calls = []
    real_get_pixmap = fitz.Page.get_pixmap
    def tracking_get_pixmap(self, *a, **k):
        calls.append("render")
        return real_get_pixmap(self, *a, **k)
    monkeypatch.setattr(fitz.Page, "get_pixmap", tracking_get_pixmap)

    def fake_ocr_one(im, exe):
        calls.append("ocr")
        return ""
    monkeypatch.setattr(ocr, "_ocr_one_image", fake_ocr_one)

    ocr.ocr_pdf(pdf_path)
    assert calls == ["render", "ocr", "render", "ocr"]


def test_ocr_crash_is_distinct_from_a_blank_page(tmp_path, monkeypatch):
    """2026-07-25 code review: ocr_pdf/ocr_image used to catch any exception and
    return '' -- identical to a genuinely blank page. A crash must now be
    reported distinctly ('ocr-error') so indexer.py can retry it, instead of
    treating it as a successfully-read (if empty) document forever."""
    monkeypatch.setattr(ocr, "find_tesseract", lambda p="": "/fake/tesseract")

    def boom(*a, **k):
        raise RuntimeError("corrupt render")
    monkeypatch.setattr(ocr, "_ocr_images", boom)

    assert ocr.ocr_pdf(make_pdf(str(tmp_path / "p.pdf"))) == ("", "ocr-error")

    img = tmp_path / "scan.jpg"
    img.write_bytes(b"\xff\xd8\xff\xe0not-a-real-jpeg")
    # Image.open on a bogus JPEG raises before ever reaching _ocr_images --
    # still must surface as ocr-error, not a silent blank string.
    assert ocr.ocr_image(str(img)) == ("", "ocr-error")

    ext, method = ocr.extract_document_text(str(img))
    assert (ext, method) == ("", "ocr-error")
