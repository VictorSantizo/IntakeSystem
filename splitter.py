"""
splitter.py – Detect document boundaries in a multi-document PDF and split it.

Uses pdfplumber to extract per-page text, sends to Claude to identify where
each document starts, then uses pypdf to write separate PDF files.
Returns a list of output PDF paths (may be just [original] if only 1 doc found).
"""
import json, os, re, shutil
import pdfplumber
from pypdf import PdfReader, PdfWriter


def _wc(text: str) -> int:
    """Count meaningful words (ignore single characters / stray marks)."""
    return len([w for w in text.split() if len(w) > 1])


def _parse_boundary_json(raw: str, num_pages: int) -> list[int]:
    """Parse Claude's boundary answer (a JSON array of 1-based page numbers,
    possibly fenced) into sorted 0-based start indices. Page 0 always included;
    out-of-range pages dropped."""
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    starts = sorted(set(int(p) - 1 for p in json.loads(raw)
                        if 1 <= int(p) <= num_pages))
    if not starts or starts[0] != 0:
        starts = [0] + starts
    return starts


def _set_aside(path: str, subfolder: str):
    """Move a file into `subfolder` beside it (collision-safe) instead of deleting
    it — the folder watcher and startup sweep only look at the top level, so a
    moved file leaves the intake flow but stays recoverable."""
    try:
        folder = os.path.join(os.path.dirname(path), subfolder)
        os.makedirs(folder, exist_ok=True)
        dest = os.path.join(folder, os.path.basename(path))
        root, ext = os.path.splitext(dest)
        c = 1
        while os.path.exists(dest):
            dest = f"{root} ({c}){ext}"
            c += 1
        shutil.move(path, dest)
        return dest
    except Exception as e:
        print(f"[Splitter] Could not move {os.path.basename(path or '?')} to {subfolder}: {e}")
        return None


def extract_pages_text(pdf_path: str) -> list[str]:
    """Return a list of text strings, one per page."""
    pages = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            t = page.extract_text() or ""
            pages.append(t.strip())
    return pages


def detect_boundaries_with_claude(pages_text: list[str], api_key: str) -> list[int]:
    """
    Ask Claude which pages start a new document.
    Returns a list of 0-based page indices that are the FIRST page of each document.
    e.g. [0, 3, 5] means doc1=pages 0-2, doc2=pages 3-4, doc3=pages 5+
    """
    import anthropic

    numbered = ""
    for i, text in enumerate(pages_text):
        snippet = text[:600].replace("\n", " ")
        tag = "  [NEARLY BLANK — likely a blank back of a page or a separator sheet]" if _wc(text) < 15 else ""
        numbered += f"\n--- PAGE {i+1}{tag} ---\n{snippet}\n"

    prompt = f"""You are analyzing a scanned PDF that may contain multiple separate documents batched together.

Below is the text extracted from each page. Identify where each NEW document begins.

{numbered}

Rules for where a new document starts:
- A new document starts ONLY on a page that clearly begins a distinct document — its own letterhead, a new sender/vendor, a new statement, a new invoice header, etc.
- Do NOT start a new document on a blank or nearly-blank page, a separator/divider sheet, a page that contains only a footer / page number / legal boilerplate, or a continuation page of the previous document.
- A blank or near-blank page belongs to the document BEFORE it — never treat it as its own document.
- A letter, notice, insert, or other CORRESPONDENCE from the SAME sender/company as the page before it is part of that SAME document — do NOT start a new document for it. This is common with utility bills (e.g. Southern California Edison, SoCal Gas, Frontier) that include a notice, rate-change letter, or insert along with the bill. Only start a new document when the sender/company/account clearly CHANGES.

Return ONLY a JSON array of page numbers (1-based) where a new document starts.
Page 1 is always a start.
Example: [1, 4, 7] means document 1 starts at page 1, document 2 at page 4, document 3 at page 7.

Return ONLY the JSON array, nothing else."""

    client = anthropic.Anthropic(api_key=api_key)
    msg = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}],
        timeout=15.0,
    )
    starts = _parse_boundary_json(msg.content[0].text, len(pages_text))
    # Deterministic backstop: never START a new document on a blank/near-blank page.
    # If the model flags one anyway, drop that boundary so the blank page merges into
    # the document before it instead of becoming its own "document". Page 0 always kept.
    # (Only valid when the PDF has a text layer — an image-only scan has 0 words on
    # EVERY page, which is why split_if_needed routes those to the vision pass instead.)
    starts = [s for s in starts if s == 0 or _wc(pages_text[s]) >= 15]
    return starts


def build_contact_sheets(pdf_path: str, cols: int = 4, per_sheet: int = 12,
                         thumb_w: int = 240) -> list[str]:
    """Render every page as a small labeled thumbnail and paste them into one or
    more grid "contact sheet" PNGs, so a whole batch can be shown to Claude vision
    in a single image (or a few) instead of one full-resolution image per page.
    Returns a list of temp PNG paths — caller must delete them."""
    import tempfile
    import pypdfium2 as pdfium
    from PIL import Image, ImageDraw, ImageFont

    try:
        font = ImageFont.truetype("arial.ttf", 16)
    except Exception:
        font = ImageFont.load_default()

    # Explicit close: a lingering pdfium handle keeps the file locked on Windows,
    # which would make the _Split Originals archival below fail (WinError 32).
    pdf = pdfium.PdfDocument(pdf_path)
    try:
        n = len(pdf)
        thumbs = []
        for i in range(n):
            page = pdf[i]
            w_pt = page.get_size()[0] or 612
            pil = page.render(scale=thumb_w / w_pt).to_pil().convert("RGB")
            if pil.width != thumb_w:
                pil = pil.resize((thumb_w, max(1, round(pil.height * thumb_w / pil.width))))
            thumbs.append(pil)
    finally:
        pdf.close()

    pad, label_h = 8, 22
    sheet_paths = []
    for s0 in range(0, n, per_sheet):
        batch = thumbs[s0:s0 + per_sheet]
        rows = (len(batch) + cols - 1) // cols
        cell_h = max(t.height for t in batch) + label_h
        width = cols * thumb_w + (cols + 1) * pad
        height = rows * cell_h + (rows + 1) * pad
        sheet = Image.new("RGB", (width, height), "white")
        draw = ImageDraw.Draw(sheet)
        for j, t in enumerate(batch):
            r, c = divmod(j, cols)
            x = pad + c * (thumb_w + pad)
            y = pad + r * (cell_h + pad)
            draw.rectangle([x, y, x + thumb_w, y + label_h], fill="black")
            draw.text((x + 6, y + 3), f"PAGE {s0 + j + 1}", fill="white", font=font)
            sheet.paste(t, (x, y + label_h))
            # A white scan on a white canvas has no visible edge — outline each
            # cell so the model can tell where one page thumbnail ends.
            draw.rectangle([x, y, x + thumb_w, y + label_h + t.height],
                           outline="red", width=2)
        fd, tmp = tempfile.mkstemp(suffix=".png")
        os.close(fd)
        sheet.save(tmp)
        sheet_paths.append(tmp)
    return sheet_paths


def detect_boundaries_with_vision(pdf_path: str, num_pages: int, api_key: str) -> list[int]:
    """Boundary detection for image-only batches (no text layer on any page),
    where the text-based pass has nothing to read. Shows Claude a labeled
    contact sheet of every page and asks the same question the text pass asks.
    One API call per batch regardless of page count — cost control."""
    import anthropic, base64

    sheets = build_contact_sheets(pdf_path)
    try:
        content = []
        for p in sheets:
            with open(p, "rb") as f:
                b64 = base64.standard_b64encode(f.read()).decode("utf-8")
            content.append({"type": "image", "source": {
                "type": "base64", "media_type": "image/png", "data": b64}})

        prompt = f"""The attached contact sheet image(s) show a labeled thumbnail of every page of a scanned PDF, in order (black label bars read "PAGE 1", "PAGE 2", ...). The PDF is a batch of separate paper documents scanned in one pass. There are {num_pages} pages total.

Identify which pages BEGIN a new document.

Rules for where a new document starts:
- Page 1 is always a start.
- A decorated batch cover sheet ("KLR MAIL BATCH COVER SHEET" with a big RECEIVED DATE area) is its own document.
- A new document starts ONLY on a page that clearly begins a distinct document — its own letterhead/logo, a new sender/company, a new statement or invoice header. Before marking a start, compare the page with the page BEFORE it: if the sender/company looks the same, it is a continuation, not a start.
- A blank or nearly-blank page is the scanned BACK of a sheet — it belongs to the document BEFORE it. Never treat it as a start.
- A page that is blank except for a date stamp (e.g. a red "RECEIVED" stamp) is the back of the previous document's last sheet — it belongs to that document, never a start.
- A letter, notice, or insert from the SAME sender/company as the page before it is part of that SAME document (utility bills often include a rate notice or insert). Only start a new document when the sender/company clearly changes.
- An insurance packet is ONE document: a certificate of insurance and ALL the endorsement / policy-wording / terms pages mailed with it stay together. Dense boilerplate pages are continuations.
- Forms, codes-explanation pages, and terms printed on the back of a statement belong to that statement.
- When unsure whether a page starts a new document or continues the previous one, treat it as a CONTINUATION. A missed split is easy to fix later; the received-date stamp on a document's last page must stay with its document.

Return ONLY a JSON array of 1-based page numbers where a new document starts.
Example: [1, 4, 7] means document 1 starts at page 1, document 2 at page 4, document 3 at page 7.
Return ONLY the JSON array, nothing else."""
        content.append({"type": "text", "text": prompt})

        # Sonnet with (default) adaptive thinking, not haiku: on a real 23-page
        # test batch haiku returned different, wrong boundaries on every run,
        # while sonnet matched the human-read ground truth. This is ONE call per
        # image-only batch (~$0.02-0.10 with thinking) — boundary quality gates
        # everything downstream, so the stamp lands on the right bill.
        # max_tokens must leave room for thinking; the answer itself is tiny.
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model="claude-sonnet-5",
            max_tokens=8000,
            messages=[{"role": "user", "content": content}],
            timeout=90.0,
        )
        # Thinking models return a thinking block before the text block.
        raw = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        return _parse_boundary_json(raw, num_pages)
    finally:
        for p in sheets:
            try:
                os.remove(p)
            except OSError:
                pass


def split_pdf(pdf_path: str, boundary_pages: list[int]) -> list[str]:
    """
    Split pdf_path at the given 0-based page boundaries.
    Writes files alongside the original: original_part1.pdf, _part2.pdf, etc.
    Returns list of output file paths.
    """
    if len(boundary_pages) <= 1:
        return [pdf_path]

    base, ext = os.path.splitext(pdf_path)
    output_paths = []

    # Own the file handle: pypdf's reader objects are cyclic, so relying on GC
    # to release the handle can leave the file locked on Windows and break the
    # _Split Originals move that follows.
    with open(pdf_path, "rb") as fh:
        reader = PdfReader(fh)
        total = len(reader.pages)
        boundaries = boundary_pages + [total]  # sentinel end
        for i, start in enumerate(boundary_pages):
            end = boundaries[i + 1]
            writer = PdfWriter()
            for p in range(start, end):
                writer.add_page(reader.pages[p])
            out_path = f"{base}_part{i+1}{ext}"
            with open(out_path, "wb") as f:
                writer.write(f)
            output_paths.append(out_path)

    return output_paths


def split_if_needed(pdf_path: str, api_key: str) -> list[str]:
    """
    Main entry point. Returns a list of PDF paths to process:
    - Single-page or single-document PDFs return [pdf_path] unchanged.
    - Multi-document PDFs are split and the list of parts is returned; the
      original is archived into a _Split Originals subfolder (kept, not deleted)
      so the watcher/sweep can never re-ingest it.
    """
    try:
        pages_text = extract_pages_text(pdf_path)
    except Exception as e:
        print(f"[Splitter] Could not read PDF: {e}")
        return [pdf_path]

    if len(pages_text) <= 1:
        return [pdf_path]

    if not api_key or api_key == "YOUR_API_KEY_HERE":
        print("[Splitter] No API key — skipping split detection.")
        return [pdf_path]

    # An image-only scan (no OCR text layer on ANY page) gives the text-based
    # pass nothing to work with: every boundary candidate looks "blank" and the
    # blank-page backstop would collapse the whole batch into one document.
    # Those batches go through a vision pass on a contact sheet instead.
    image_only = all(_wc(t) == 0 for t in pages_text)
    try:
        if image_only:
            print(f"[Splitter] No text layer on any of {len(pages_text)} pages — using vision boundary detection.")
            boundaries = detect_boundaries_with_vision(pdf_path, len(pages_text), api_key)
        else:
            boundaries = detect_boundaries_with_claude(pages_text, api_key)
        print(f"[Splitter] Detected {len(boundaries)} document(s) in {os.path.basename(pdf_path)}: starts at pages {[b+1 for b in boundaries]}")
    except Exception as e:
        print(f"[Splitter] Claude boundary detection failed: {e}")
        return [pdf_path]

    if len(boundaries) <= 1:
        return [pdf_path]

    try:
        parts = split_pdf(pdf_path, boundaries)
        if image_only:
            # Every part of an image-only batch has 0 extractable words, so the
            # word-count filter below would set aside ALL of them. The vision
            # pass already folded blank backs into their documents — keep every
            # part; each will be classified via the vision fallback.
            non_blank = parts
        else:
            # Set aside parts that are blank or nearly blank. Moved, not deleted — a
            # "blank" part can be an image-only page (a photo, a handwritten note)
            # that text extraction simply can't see.
            non_blank = []
            for p in parts:
                text = " ".join(extract_pages_text(p))
                words = [w for w in text.split() if len(w) > 1]
                if len(words) >= 15:
                    non_blank.append(p)
                else:
                    print(f"[Splitter] Setting aside blank/near-blank part ({len(words)} words): {os.path.basename(p)}")
                    _set_aside(p, "_Blank")
        print(f"[Splitter] {len(non_blank)} non-blank document(s) after filtering.")
        if non_blank:
            # The parts now carry this PDF's content, so retire the original OUT of
            # the watched folder. Leaving it in place meant every "Check for new items" and
            # every startup sweep re-split and re-queued all of its documents as
            # duplicates. Archived (recoverable), never deleted.
            _set_aside(pdf_path, "_Split Originals")
            return non_blank
        return [pdf_path]
    except Exception as e:
        print(f"[Splitter] Split failed: {e}")
        return [pdf_path]
