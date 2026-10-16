"""Text extraction + OCR for the full-text index (Option B).

Two paths, best-first:
  1. Text-layer PDFs → pdfplumber (already a dependency, no OCR needed).
  2. Image-only scans → PyMuPDF rasterizes each page and pytesseract runs the
     Tesseract binary over it.

Tesseract is a SYSTEM install (not pip) and lives on the SERVER. Everything here
degrades gracefully when it — or the optional pip deps — are absent: text-layer
documents still index, image-only ones return ('', 'ocr-unavailable') so the
indexer can record "needs OCR" instead of crashing.
"""
import os
import shutil

# Where the Tesseract executable tends to live if it isn't on PATH (the UB
# Mannheim installer's default). Checked after config path and PATH.
_COMMON_TESSERACT = (
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
)

# Below this many characters of embedded text, treat a PDF as image-only and
# fall back to OCR (scanned bills usually have a stray character or two).
_MIN_TEXT_CHARS = 40
_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".gif")


def find_tesseract(configured_path: str = "") -> str:
    """Locate the Tesseract executable: explicit config path, then PATH, then the
    usual install dirs. Returns the path, or '' if not found."""
    if configured_path and os.path.exists(configured_path):
        return configured_path
    on_path = shutil.which("tesseract")
    if on_path:
        return on_path
    for cand in _COMMON_TESSERACT:
        if os.path.exists(cand):
            return cand
    return ""


def tesseract_available(configured_path: str = "") -> bool:
    return bool(find_tesseract(configured_path))


def extract_text_layer(pdf_path: str) -> str:
    """Text already embedded in the PDF (no OCR). '' if none or unreadable."""
    try:
        import pdfplumber
        parts = []
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                parts.append(page.extract_text() or "")
        return "\n".join(parts).strip()
    except Exception:
        return ""


# Per-page cap on a single Tesseract invocation. Without this, a single
# malformed/oversized page can block the OCR subprocess indefinitely — and
# since reindex() processes documents one at a time, that one page silently
# freezes the ENTIRE batch (seen 2026-07-18: an hour at 0% CPU on one file).
# pytesseract enforces this itself (kills the subprocess and raises), so a
# stuck page now just fails that one page instead of hanging everything.
_OCR_PAGE_TIMEOUT = 60


def _ocr_one_image(im, exe: str) -> str:
    """OCR a single already-open PIL image. '' if the page times out (see
    _OCR_PAGE_TIMEOUT) -- one bad page must not lose the whole document."""
    import pytesseract
    pytesseract.pytesseract.tesseract_cmd = exe
    try:
        return pytesseract.image_to_string(im, timeout=_OCR_PAGE_TIMEOUT) or ""
    except RuntimeError:
        # pytesseract raises plain RuntimeError('Tesseract process timeout')
        # and kills the subprocess itself — this page never finished, skip
        # it rather than lose the whole document (or the whole batch).
        return ""


def _ocr_images(images, exe: str) -> str:
    return "\n".join(_ocr_one_image(im, exe) for im in images).strip()


def ocr_pdf(pdf_path: str, tesseract_path: str = "") -> tuple:
    """OCR every page of an image-only PDF. Returns (text, error): error is None
    on a normal outcome (successful OCR — possibly a genuinely blank page — or
    Tesseract simply not being available), and the string 'ocr-error' when
    PyMuPDF/pytesseract itself raised (corrupt render, out-of-memory, etc.).

    2026-07-25 code review: this used to swallow that exception and return '',
    making a real OCR crash permanently indistinguishable from "nothing to find"
    -- extract_document_text reported both as method 'none', and indexer.py only
    ever retries 'ocr-unavailable', so a transient crash silently dropped that
    document from search forever (until a forced reindex). Callers now get the
    distinction and can decide what to do with it.
    Lazy-imports PyMuPDF/pytesseract/PIL so the app runs without them installed.

    2026-07-27 code review: this used to rasterize EVERY page into a PIL image
    list before OCR'ing any of them -- a 50-page scan at 200 dpi held ~1-2 GB of
    raw bitmaps in memory at once. Now streams one page at a time: render, OCR,
    discard the bitmap, move on -- also makes progress incremental rather than
    all-or-nothing alongside the existing per-page timeout."""
    exe = find_tesseract(tesseract_path)
    if not exe:
        return "", None
    try:
        import fitz  # PyMuPDF
        from PIL import Image
        parts = []
        with fitz.open(pdf_path) as doc:
            for page in doc:
                pix = page.get_pixmap(dpi=200)
                im = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
                parts.append(_ocr_one_image(im, exe))
        return "\n".join(parts).strip(), None
    except Exception:
        return "", "ocr-error"


def ocr_image(image_path: str, tesseract_path: str = "") -> tuple:
    """OCR a bare image file (a scan saved as .jpg/.png rather than a PDF).
    Returns (text, error) — same contract as ocr_pdf above."""
    exe = find_tesseract(tesseract_path)
    if not exe:
        return "", None
    try:
        from PIL import Image
        with Image.open(image_path) as im:
            return _ocr_images([im.convert("RGB")], exe), None
    except Exception:
        return "", "ocr-error"


def extract_document_text(path: str, tesseract_path: str = ""):
    """Best available text for a document, and how we got it.

    Returns (text, method) where method is one of:
      'text'            — read the embedded text layer, no OCR needed
      'ocr'             — OCR'd an image-only scan
      'none'            — a text document that genuinely yielded nothing
      'ocr-unavailable' — needs OCR but Tesseract isn't installed yet
      'ocr-error'       — OCR itself crashed (corrupt render, out-of-memory,
                          etc.) -- distinct from 'none' precisely so indexer.py
                          knows to retry it, the same way it retries
                          'ocr-unavailable' (2026-07-25 code review: before this,
                          a transient OCR crash and a truly blank page were both
                          reported as 'none' and neither was ever retried)
    """
    if not path or not os.path.exists(path):
        return "", "none"
    ext = os.path.splitext(path)[1].lower()

    if ext in _IMAGE_EXTS:
        if not tesseract_available(tesseract_path):
            return "", "ocr-unavailable"
        text, err = ocr_image(path, tesseract_path)
        return ("", "ocr-error") if err else (text, "ocr")

    # Assume PDF (the app's stored docs). Try the cheap text layer first.
    text = extract_text_layer(path)
    if len(text) >= _MIN_TEXT_CHARS:
        return text, "text"
    if not tesseract_available(tesseract_path):
        return (text, "text") if text else ("", "ocr-unavailable")
    ocr, err = ocr_pdf(path, tesseract_path)
    if err:
        return "", "ocr-error"
    if ocr:
        return ocr, "ocr"
    return (text, "text") if text else ("", "none")
