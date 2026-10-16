"""
classifier.py – Extract and classify document data using Claude API.
Falls back to regex extraction if no API key is configured.
"""
import re, json, os, traceback
from datetime import date, datetime

import doc_types

# Marker text printed on the batch cover sheet so a searchable scan can be
# recognized without the AI (vision still catches handwritten/photo cover sheets).
COVER_SHEET_MARKER = "KLR MAIL BATCH COVER SHEET"

# Distilled, classification-relevant business rules reverse-engineered from 13 years /
# 175k documents (see T:\_OPS INDEX\KAREN_SYSTEM_SCHEMA.md + KAREN_FILENAME_SPEC.md).
# Injected into the Claude prompt so intake classification understands Karen's
# conventions, not just the bare entity/property/bill-type lists from config.json.
# Keep this focused on what helps classify ONE incoming document — folder/spreadsheet
# schema lives in the schema doc, not here.
DOMAIN_RULES = """\
KAREN'S DOMAIN RULES (apply these — derived from 13 years / 175k documents):

PROPERTY CODES encode the owning trust by prefix:
  T### -> TRUST1   TL### -> TRUST2   TB### -> TRUST3   PP## -> PP Properties
  If the document does not state an ENTITY, infer it from the property prefix.
  A bill's entity should match its property's trust; if they conflict, lower the
  confidence and explain in notes (don't silently pick one).

PAYEE -> ACCOUNT routing:
  - People (tenants, owners, employees) almost always belong to ONE fixed account;
    use the account historically associated with that person.
  - Shared utilities and credit cards (e.g. SoCal Edison, Frontier, SoCal Gas,
    AMEX / CITI / CHASE) are NOT tied to one account. Derive the entity from the
    PROPERTY the expense is for, not from the vendor.

TRANSACTION / DOCUMENT TYPES (distinguish carefully):
  - DEPOSIT = incoming money; it carries its OWN "DEPOSIT #", which is NOT a check number.
  - CHECK carries a check number. ACH usually has no check number but almost always an
    amount. A single payment can be a check that clears as ACH (both facts are valid).
  - "PAID BILLS" is a category wrapper, not a payment method — look inside for the real
    vendor and amount.
  - OWNER DRAW (an equity distribution to an owner) is NOT a TRANSFER (an inter-account
    move, usually phrased "FROM x TO y").
  - VOID = a voided instrument; keep the record and note that it is void.
  - A bank's own periodic account statement is doc_type "Bank Statement". A running
    account statement from a vendor or service that is NOT a bank (a summary of
    activity / balance forward, not a demand for payment) is doc_type "Statement".
    Neither normally carries a transaction amount of its own.
  - Insurance paperwork (a policy, declarations page, certificate of insurance, or a
    renewal/cancellation notice) is doc_type "Insurance" — but an insurance PREMIUM
    invoice asking for payment is a "Bill".
  - Anything from a taxing authority or the tax preparer (assessments, returns,
    notices, 1099s) is doc_type "Tax" — but a property-tax bill with an amount due
    and a due date is a "Bill".
  - An audit report, audit correspondence, or an examination notice is doc_type "Audit".

AMOUNTS:
  - About 75% of real documents have NO amount — that is normal. Use null; never invent one.
  - If several $ values appear, the transaction amount is the LARGEST.
  - Never treat a bare number, account number, routing number, or a "#1234" reference as
    an amount.

LEGAL vs BOILERPLATE (doc_type "Legal" is a "needs attention" bucket — do not overfill it):
  - Use doc_type "Legal" ONLY for documents that carry real legal weight or need a
    person to act: an EXECUTED (filled-in and/or signed) lease, addendum, or agreement;
    eviction / 3-day / lease-termination / default notices; court filings, summonses,
    liens, demand or attorney letters; and rental applications under review.
  - A BLANK or UNEXECUTED template — a lease or addendum with empty fields and no
    signatures, a draft, an unfilled boilerplate insert — is doc_type "Other", NOT
    "Legal", and important=false. It is reference boilerplate, not something that needs
    attention. (An executed, signed copy of the very same form IS Legal.) When unsure
    whether a form is filled in, look for names, dates, dollar amounts, or signatures in
    its blanks; all-empty blanks means it is a template.
  - A fillable PDF's typed values are NOT in the page text — the page text shows only the
    empty printed labels ("RESIDENT 1 Last Name First Name"). Those values arrive in a
    separate "[FILLED-IN FORM FIELDS]" section at the end of the text. When that section
    is present the form IS filled in: read the names, amounts and addresses from it, and
    never call the document blank or a template.

DOCUMENT LABELS (the "vendor" field doubles as the document's display label in lists):
  - RENTAL APPLICATIONS: an application, screening authorization, credit-check consent,
    or applicant form from a PROSPECTIVE TENANT is labeled
    "Rental Application - <applicant's name>" — the person applying, NOT the screening
    company, property manager, or website that sent it. doc_type is Legal.
  - BLANK / TEMPLATE forms have no sender — give a SPECIFIC descriptive title that names
    what the form is AND that it is blank, e.g. "Blank Lease Addendum - Mold Notification",
    "Blank Crime-Free Lease Addendum", "Blank Bedbug Addendum", "Blank Lease Addendum #2",
    "Draft Lease (unsigned)". Name the addendum's specific subject when the page shows it;
    a bare "Addendum" or "(blank form)" is not enough. Never leave "vendor" null: every
    document needs a label a busy office manager can recognize at a glance.
  - A FILLED lease or agreement is named for its resident(s), taken from the filled-in
    form fields — e.g. "Residential Rental Agreement - Lola J. Roa". Never label a filled
    form "Blank ...": several executed leases labeled that way became indistinguishable
    from each other and from the real blank template.

NEVER force a guess. If the entity, property, or type cannot be determined confidently,
leave it null / set confidence to Low and explain in notes — these go to human review.
Do not silently drop information."""

def _iso_date_or_none(raw):
    """Normalize a date string to ISO 'YYYY-MM-DD', or None if unparseable.
    Dates are stored as strings and compared lexically all over the app
    (overdue flags, due-date ordering), so a raw '6/15/26' silently never
    sorts or flags — every date that gets stored must be ISO."""
    raw = (str(raw) if raw is not None else "").strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw[:10]).isoformat()
    except ValueError:
        pass
    m = re.match(r'^(\d{1,2})[\/\-\.](\d{1,2})[\/\-\.](\d{2,4})$', raw)
    if m:
        mo, d, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if y < 100:
            y += 2000
        try:
            return date(y, mo, d).isoformat()
        except ValueError:
            return None
    for fmt in ("%B %d, %Y", "%B %d %Y", "%b %d, %Y", "%b %d %Y"):
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    return None

def _cover_sheet_date_from_text(text: str):
    """Best-effort: pull the received date written near 'RECEIVED DATE' on a
    cover sheet whose text layer was OCR'd. Returns 'YYYY-MM-DD' or None."""
    m = re.search(r'RECEIVED\s*DATE\D{0,15}(\d{1,2})[\/\-\.](\d{1,2})[\/\-\.](\d{2,4})',
                  text, re.IGNORECASE)
    if not m:
        return None
    mo, d, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if y < 100:
        y += 2000
    try:
        return date(y, mo, d).isoformat()
    except ValueError:
        return None

def load_config():
    cfg_path = os.path.join(os.path.dirname(__file__), "config.json")
    # Explicit encoding — never depend on the Windows locale default (cp1252).
    with open(cfg_path, encoding="utf-8") as f:
        return json.load(f)

def extract_text_from_pdf(pdf_path: str) -> str:
    """Extract text from a PDF using pdfplumber (handles ScanSnap searchable PDFs)."""
    try:
        import pdfplumber
        text_parts = []
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                t = page.extract_text()
                if t:
                    text_parts.append(t)
        return "\n".join(text_parts)
    except Exception as e:
        return f"[PDF extraction error: {e}]"

_FORM_FIELD_LIMIT = 120       # a long lease fills ~95; the cap is for runaway forms

def extract_pdf_form_values(pdf_path: str) -> str:
    """The values a person typed into a fillable PDF's fields.

    A page's text layer holds the form's printed LABELS; the typed values live
    in the AcroForm fields and are drawn from there by the viewer. So a filled
    lease extracts identically to its own blank template, and the office's
    executed leases were all classified "Blank Residential Rental Agreement" —
    tenant, rent and owner lost — while the browser showed them plainly.

    Returns "" for anything that isn't a filled form, and never raises: this
    rides on every PDF classification, and a malformed form must cost us the
    field values, not the document."""
    try:
        from pypdf import PdfReader
        fields = PdfReader(pdf_path).get_fields() or {}
    except Exception:
        return ""
    lines = []
    for name, field in fields.items():
        try:
            value = field.get("/V")
        except Exception:
            continue
        if value is None:
            continue
        value = str(value).strip()
        if not value or value == "/Off":
            continue          # an unchecked box carries no signal
        lines.append(f"{name}: {value[:200]}")
        if len(lines) >= _FORM_FIELD_LIMIT:
            break
    if not lines:
        return ""
    return ("\n\n[FILLED-IN FORM FIELDS — a person typed these into this document's "
            "form fields, so it is NOT a blank template]\n" + "\n".join(lines))

def classify_with_claude(text: str, config: dict, vendor_hint: str = "",
                         image_path: str = None, image_media: str = None,
                         image_paths: list = None, form_values: str = "") -> dict:
    """Classify a document via Claude. Pass `text` for text-based docs (PDF/Word),
    or `image_path` + `image_media` for a photo/scan image (uses Claude vision).
    `image_paths` sends multiple images in one call — used for image-only PDFs
    where the first page carries the document and the last page carries the
    office's RECEIVED date stamp (Karen stamps the back of the last sheet).
    `form_values` is the separate "[FILLED-IN FORM FIELDS]" block (see
    extract_pdf_form_values) — kept as its own argument, NOT pre-concatenated
    onto `text` by the caller, so it can be appended AFTER `text` is truncated
    below instead of being truncated away along with it."""
    try:
        import anthropic
    except ImportError as e:
        # anthropic is a hard requirement (requirements.txt), but a broken
        # install must still degrade to keyword extraction, not crash intake.
        print(f"[Classifier] anthropic package unavailable, falling back to regex: {e}")
        result = classify_with_regex(text + form_values, config)
        result["notes"] = f"[AI unavailable: {e}] " + result.get("notes", "")
        return result

    api_key = config.get("claude_api_key", "")
    if not api_key or api_key == "YOUR_API_KEY_HERE":
        return classify_with_regex(text + form_values, config)

    # The SDK's whole error hierarchy (connection, timeout, rate-limit, auth,
    # overloaded, server errors...) inherits from AnthropicError, so this one
    # catch covers all of them. A couple of tests substitute a bare-bones fake
    # `anthropic` module that doesn't define it — getattr keeps that a no-op
    # instead of an AttributeError while resolving the except clause below.
    _AnthropicError = getattr(anthropic, "AnthropicError", None)
    api_error_types = (_AnthropicError, json.JSONDecodeError) if _AnthropicError \
        else (json.JSONDecodeError,)

    try:
        entities_str = "\n".join(
            f"  {e['code']}: {e['name']}" for e in config["entities"])
        props_str = "\n".join(
            f"  {p['code']}: {p['address']} (Entity: {p['entity_code']})"
            for p in config["properties"])
        bill_types_str = "\n".join(
            f"  {b['code']}: {b['description']} [{b['category']}]"
            for b in config["bill_types"])

        # Pull learned corrections for this vendor
        hints_section = ""
        try:
            from database import get_vendor_hints
            hints = get_vendor_hints(vendor_hint)
            if hints:
                lines = []
                for h in hints:
                    lines.append(f"  Vendor '{h['vendor_key']}' → entity={h['entity_code']}, "
                                 f"properties={h['property_codes']}, doc_type={h['doc_type']}, "
                                 f"bill_type={h['bill_type_code']}")
                hints_section = "\nPREVIOUSLY LEARNED (user-confirmed corrections — use these as strong hints):\n" + "\n".join(lines) + "\n"
        except Exception:
            pass

        img_list = image_paths or ([image_path] if image_path else [])
        if len(img_list) > 1:
            doc_section = ("The document is provided as TWO attached images: its FIRST page, "
                           "and its LAST page (or a zoomed-in crop of the stamped area of the "
                           "last page). The middle pages of this multi-page document are not "
                           "shown. Extract the fields from the first page. The second image "
                           "is included because the office stamps the RECEIVED date on the "
                           "back of the document's final sheet — examine it for a date stamp.")
        elif img_list:
            doc_section = "The document is the attached image. Read it carefully to extract the fields."
        else:
            # Truncate the document's OWN text before appending form_values.
            # Appending form_values first and truncating the combined string
            # (the original bug here) cut the [FILLED-IN FORM FIELDS] block
            # off entirely on any document whose own text was already
            # >=4000 chars — exactly the long executed leases that block
            # exists to surface, silently reintroducing "executed lease
            # classified as Blank" (see extract_pdf_form_values).
            doc_section = f"Document text:\n---\n{text[:4000]}{form_values}\n---"

        prompt = f"""You are an intake classifier for a property management company.
Analyze the document and extract the following information.
Return ONLY a JSON object with these exact keys.

{hints_section}
ENTITIES on file:
{entities_str}

PROPERTIES on file:
{props_str}

BILL TYPES on file:
{bill_types_str}

{DOMAIN_RULES}

{doc_section}

Return a JSON object:
{{
  "is_cover_sheet": true or false (true ONLY if this page is the decorated "KLR MAIL BATCH COVER SHEET" used to separate scanning batches — it has a big RECEIVED DATE area and peace/star/lightning decorations),
  "received_date": "YYYY-MM-DD or null — if this IS a cover sheet, the mail-received date written or printed on it (read handwriting if needed)",
  "stamped_received_date": "YYYY-MM-DD or null — if the document itself carries a RECEIVED-DATE STAMP (a date stamp, often red/blue ink, or text like 'RECEIVED <date>' / 'REC'D <date>' hand-stamped on the page, or the same thing written by hand in pen, e.g. 'Received 7/22' or 'Received 7-22-26' — slashes, dashes, or dots all count), the date it shows; else null. This is the date the office received the bill, not the bill's own date or due date. Transcribe the stamp EXACTLY as inked — read the month, the day, and every digit of the year individually (stamps are often faint, rotated, or unevenly inked); never infer or round a digit you cannot see.",
  "entity_code": "best matching entity code or null",
  "entity_name": "entity name or null",
  "property_codes": ["list of matching property codes, or empty list"],
  "property_addresses": ["corresponding addresses, or empty list"],
  "doc_type": "{" | ".join(doc_types.DOC_TYPES)}",
  "bill_type_code": "best matching bill type code or null",
  "vendor": "sender or vendor name — this is the document's display label, so it must never be null or empty; follow the DOCUMENT LABELS rules for applications, blank forms, and other documents without a sender",
  "amount": numeric amount or null,
  "due_date": "YYYY-MM-DD or null",
  "document_date": "YYYY-MM-DD or null — the date the document ITSELF is dated (the bill date, invoice date, statement date, or letter date printed on it). NOT the due date, NOT a received stamp, NOT a service-period date. Null when the document shows no date of its own.",
  "invoice_number": "invoice or statement number (the identifier unique to THIS bill), or null",
  "account_number": "account or customer number (the ongoing account this bill belongs to), or null",
  "notes": "brief notes about this document — AND if it includes an accompanying letter/notice/insert from the same sender (e.g. a rate change, shutoff warning, or account update along with a utility bill), summarize that message here so it isn't lost",
  "important": true or false — see the IMPORTANT-flag guidance below,
  "important_reason": "if important: ONE short plain-English sentence a busy office manager would understand, saying why this needs attention; else empty string",
  "notice_summary": "if the PRIMARY document (e.g. a utility bill or statement) ALSO carries an accompanying NOTICE the office should read or act on — a rate increase, a shutoff / disconnection warning, an insurance cancellation or non-renewal, a legal or regulatory notice, or a significant account / policy change bundled in with the bill — a ONE-line plain-English summary of that notice; else null. Do NOT fill this for the document's own main content, for a standalone notice (that is just its doc_type), or for routine marketing, promotional inserts, or generic 'ways to pay' boilerplate. When you set notice_summary for a consequential notice, also set important=true.",
  "confidence": "High | Medium | Low"
}}

Bill vs Receipt: a BILL is a request for payment still owed (shows an amount DUE, a due
date, "please pay", a balance). A RECEIPT is proof a payment was ALREADY made ("paid",
"payment received", "thank you for your payment", a store/credit-card sales slip) — set
doc_type "Receipt", NOT "Bill", so it isn't counted as money owed. If a document shows
both an invoice and a paid stamp/zero balance, it's a Receipt.

For invoice_number vs account_number: the INVOICE/statement number is unique to this
one bill (it changes every billing cycle); the ACCOUNT/customer number stays the same
across every bill from that account. If only one number is present, decide which it is
from its label; if unsure, put it in invoice_number. Use null when not found.

IMPORTANT flag: set "important" true ONLY when the document carries consequences
beyond routine processing for a property-management office — legal action, court
dates or filings, eviction / unlawful-detainer papers, government or tax notices,
utility shutoff or service-termination warnings, insurance cancellation or
non-renewal, collections or fraud alerts, permit or code violations, or a genuinely
unusual demand (an abnormally large or clearly disputed amount). Marketing urgency
("FINAL NOTICE" sales, expiring offers, newsletters) and ordinary recurring bills are
NOT important, even when their wording sounds urgent. Most documents are not important.

If you cannot confidently match an entity or property, set those to null.
Return ONLY the JSON object, no other text."""

        content = []
        if img_list:
            import base64
            labels = [None] * len(img_list)
            if len(img_list) > 1:
                labels = ["FIRST PAGE of the document:",
                          "LAST PAGE of the document (may be zoomed to the stamped area — "
                          "check for a RECEIVED date stamp, often red or blue ink, sometimes "
                          "rotated or faint, OR a handwritten 'Received <date>' note; "
                          "transcribe its date exactly):"]
            for lbl, p in zip(labels, img_list):
                if lbl:
                    content.append({"type": "text", "text": lbl})
                with open(p, "rb") as f:
                    b64 = base64.standard_b64encode(f.read()).decode("utf-8")
                content.append({"type": "image", "source": {
                    "type": "base64", "media_type": image_media or "image/jpeg", "data": b64}})
        content.append({"type": "text", "text": prompt})

        client = anthropic.Anthropic(api_key=api_key)
        # 1024, not 512: long notes on a dense document can push the JSON past
        # 512 tokens and truncate it mid-string, failing the whole classify.
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            messages=[{"role": "user", "content": content}],
            timeout=30.0,
        )
        raw = msg.content[0].text.strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        result = json.loads(raw)
        result["ai_confidence"] = result.pop("confidence", "Medium")
        # Case/spacing variants land on the real type ("insurance" -> Insurance);
        # anything genuinely unknown passes through for a person to see and fix.
        result["doc_type"] = doc_types.normalize(result.get("doc_type"))
        # Normalize the importance flag: the model occasionally returns strings
        # ("true"/"false") instead of JSON booleans, and the reason must be a
        # string and only ever accompany a set flag.
        imp = result.get("important")
        result["important"] = imp if isinstance(imp, bool) else str(imp).strip().lower() == "true"
        if not isinstance(result.get("important_reason"), str):
            result["important_reason"] = ""
        if not result["important"]:
            result["important_reason"] = ""
        # notice_summary: a bill/statement can BUNDLE a notice worth reading. Keep
        # it only as a non-empty string; a bundled notice worth surfacing is also
        # important, so make sure the flag is set even if the model forgot.
        ns = result.get("notice_summary")
        result["notice_summary"] = ns.strip() if isinstance(ns, str) and ns.strip() else None
        if result["notice_summary"] and not result["important"]:
            result["important"] = True
            if not result.get("important_reason"):
                result["important_reason"] = result["notice_summary"]
        result.setdefault("is_cover_sheet", False)
        result.setdefault("received_date", None)
        result.setdefault("stamped_received_date", None)
        result.setdefault("document_date", None)
        # The prompt asks for ISO dates but the model occasionally echoes the
        # document's own format — normalize (or drop) every stored date.
        for k in ("due_date", "received_date", "stamped_received_date",
                  "document_date"):
            if result.get(k):
                result[k] = _iso_date_or_none(result[k])
        # Normalize property fields to lists
        result = _normalize_properties(result)
        return result
    except api_error_types as e:
        # A live Anthropic outage, rate limit, timeout, or a response body
        # that didn't parse as JSON — the API/network misbehaved, not a bug
        # in this code. During a real outage this bucket is exactly the mail
        # this app exists to catch (eviction notices, shutoff warnings, legal
        # filings) and it silently defaults to important=false below, so a
        # note buried in one record isn't enough visibility — print it loud.
        print(f"[Classifier] Anthropic classification failed "
              f"({type(e).__name__}), falling back to regex: {e}")
        result = classify_with_regex(text + form_values, config)
        result["notes"] = f"[AI unavailable: {e}] " + result.get("notes","")
        # The comment above says why this bucket needs loud visibility — this
        # is that: force the document into mandatory manual review instead of
        # letting the regex guess's important=False blend it into the normal
        # queue, where a legal/urgent notice could sit unnoticed until the API
        # is fixed and someone thinks to re-run classification on it.
        result["important"] = True
        result["important_reason"] = "AI classification failed — needs manual review"
        return result
    except Exception as e:
        # Anything else reaching here is a bug in OUR code (bad config shape,
        # a logic error above), not an API hiccup. Still must not crash
        # ingestion, but this needs the traceback — not just str(e) — to
        # actually debug, since it's not a known/expected failure mode.
        print(f"[Classifier] Unexpected error during AI classification, "
              f"falling back to regex:\n{traceback.format_exc()}")
        result = classify_with_regex(text + form_values, config)
        result["notes"] = f"[AI unavailable: {e}] " + result.get("notes","")
        result["important"] = True
        result["important_reason"] = "AI classification failed — needs manual review"
        return result

def _normalize_properties(result: dict) -> dict:
    """Ensure property_codes/property_addresses are always lists."""
    # Handle Claude returning singular or plural forms
    if "property_codes" not in result:
        pc = result.pop("property_code", None)
        result["property_codes"] = [pc] if pc else []
    if "property_addresses" not in result:
        pa = result.pop("property_address", None)
        result["property_addresses"] = [pa] if pa else []
    # Filter out nulls
    result["property_codes"] = [p for p in result["property_codes"] if p]
    result["property_addresses"] = [p for p in result["property_addresses"] if p]
    return result

def classify_with_regex(text: str, config: dict) -> dict:
    """Fallback: extract basic fields using regex when no API key is set."""
    result = {
        "is_cover_sheet": False, "received_date": None,
        "entity_code": None, "entity_name": None,
        "property_codes": [], "property_addresses": [],
        "doc_type": "Other", "bill_type_code": None,
        "vendor": None, "amount": None, "due_date": None,
        "invoice_number": None, "account_number": None,
        "important": False, "important_reason": "",   # never guessed by keywords
        "notice_summary": None,                        # bundled-notice detection needs the AI
        "notes": "Manually review — AI classification not configured.",
        "ai_confidence": "None"
    }
    amounts = re.findall(r'\$\s*([\d,]+\.?\d{0,2})', text)
    if amounts:
        biggest = max(float(a.replace(",","")) for a in amounts)
        result["amount"] = biggest
    date_patterns = [
        r'due\s+(?:date|on|by)[:\s]+(\d{1,2}[\/\-]\d{1,2}[\/\-]\d{2,4})',
        r'due[:\s]+(\w+ \d{1,2},?\s*\d{4})',
        r'pay(?:ment)? by[:\s]+(\d{1,2}[\/\-]\d{1,2}[\/\-]\d{2,4})',
    ]
    for pat in date_patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            iso = _iso_date_or_none(m.group(1))
            if iso:
                result["due_date"] = iso
            else:
                result["notes"] = (f"Due date seen as '{m.group(1)}' (could not parse). "
                                   + result["notes"])
            break
    for prop in config.get("properties", []):
        addr_parts = prop["address"].split(",")[0].lower()
        if addr_parts in text.lower():
            result["property_codes"].append(prop["code"])
            result["property_addresses"].append(prop["address"])
            if not result["entity_code"]:
                result["entity_code"] = prop["entity_code"]
                for ent in config.get("entities", []):
                    if ent["code"] == prop["entity_code"]:
                        result["entity_name"] = ent["name"]
    text_lower = text.lower()
    if any(w in text_lower for w in ["rent","lease","tenancy"]):
        result["doc_type"] = "Income"
        result["bill_type_code"] = "RENT"
    elif any(w in text_lower for w in ["invoice","bill","amount due","payment due"]):
        result["doc_type"] = "Bill"
    elif any(w in text_lower for w in ["statement","account summary","balance"]):
        result["doc_type"] = "Bank Statement"
        result["bill_type_code"] = "BANK"
    elif any(w in text_lower for w in ["notice","legal","court","lien"]):
        result["doc_type"] = "Legal"
    return result

IMAGE_MEDIA = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
               ".gif": "image/gif", ".webp": "image/webp"}
IMAGE_EXTS = set(IMAGE_MEDIA)

def extract_text_from_docx(path: str) -> str:
    """Pull text from a .docx without python-docx (it's just a zip of XML).
    Paragraphs become newlines; tags are stripped."""
    try:
        import zipfile, html as _html
        with zipfile.ZipFile(path) as z:
            xml = z.read("word/document.xml").decode("utf-8", "ignore")
        xml = xml.replace("</w:p>", "\n")
        return _html.unescape(re.sub(r"<[^>]+>", "", xml)).strip()
    except Exception as e:
        return f"[DOCX extraction error: {e}]"

def pdf_page_count(pdf_path: str) -> int:
    """Page count via pypdfium2, or 1 if it can't be read (callers treat the
    document as single-page and just use the first-page render)."""
    try:
        import pypdfium2 as pdfium
        pdf = pdfium.PdfDocument(pdf_path)
        try:
            return len(pdf)
        finally:
            pdf.close()
    except Exception:
        return 1


def render_pdf_page_image(pdf_path: str, page_index: int = 0, scale: float = 2.0):
    """Render one PDF page to a temporary PNG and return its path (or None).
    Used as a vision fallback for image-only/scanned PDFs that carry no text layer."""
    try:
        import pypdfium2 as pdfium, tempfile
        pdf = pdfium.PdfDocument(pdf_path)
        try:
            if len(pdf) == 0:
                return None
            page = pdf[min(page_index, len(pdf) - 1)]
            pil = page.render(scale=scale).to_pil()
        finally:
            pdf.close()
        fd, tmp = tempfile.mkstemp(suffix=".png")
        os.close(fd)
        pil.save(tmp)
        return tmp
    except Exception:
        return None


def _stamp_ink_bbox(pil):
    """Bounding box of saturated red/blue 'stamp ink' pixels, plus how many
    there are. Black text, gray fold shadows, and bleed-through from the other
    side of the sheet are all neutral, so only colored stamp ink survives.
    Returns (bbox, pixel_count) — bbox is None when the page has no colored ink."""
    from PIL import ImageChops
    r, g, b = pil.convert("RGB").split()
    red = ImageChops.darker(ImageChops.subtract(r, g), ImageChops.subtract(r, b))
    blue = ImageChops.darker(ImageChops.subtract(b, r), ImageChops.subtract(b, g))
    mask = ImageChops.lighter(red, blue).point(lambda v: 255 if v > 40 else 0)
    w, h = mask.size
    # Ignore a thin border strip: scanner edges pick up color noise.
    mx, my = int(w * 0.02), int(h * 0.02)
    inner = mask.crop((mx, my, w - mx, h - my))
    bbox = inner.getbbox()
    count = inner.histogram()[255]
    if not bbox:
        return None, count
    return (bbox[0] + mx, bbox[1] + my, bbox[2] + mx, bbox[3] + my), count


def render_last_page_image(pdf_path: str, page_index: int):
    """Render a document's LAST page for the received-stamp check. If the page
    carries a small patch of colored stamp ink, return a zoomed crop of just
    that region — at full-page size the stamp's year digits are small enough to
    misread (2026 came back as 2025 in testing). A page with no colored ink, or
    with color spread across it (a red-styled document), is sent whole."""
    try:
        import pypdfium2 as pdfium, tempfile
        pdf = pdfium.PdfDocument(pdf_path)
        try:
            if len(pdf) == 0:
                return None
            page = pdf[min(page_index, len(pdf) - 1)]
            pil = page.render(scale=3.0).to_pil()
        finally:
            pdf.close()
        bbox, count = _stamp_ink_bbox(pil)
        if bbox and count >= 400:
            frac = ((bbox[2] - bbox[0]) * (bbox[3] - bbox[1])) / (pil.width * pil.height)
            if frac <= 0.25:
                pad = 80
                pil = pil.crop((max(0, bbox[0] - pad), max(0, bbox[1] - pad),
                                min(pil.width, bbox[2] + pad),
                                min(pil.height, bbox[3] + pad)))
                # Blow the crop up: at native crop size (~380px wide) the year
                # digits were misread (2026 came back 2025 every run); at 3x the
                # same model read the stamp correctly every run.
                from PIL import Image
                f = min(3.0, 1600 / max(pil.size))
                if f > 1:
                    pil = pil.resize((round(pil.width * f), round(pil.height * f)),
                                     Image.LANCZOS)
        if max(pil.size) > 1600:
            pil.thumbnail((1600, 1600))
        fd, tmp = tempfile.mkstemp(suffix=".png")
        os.close(fd)
        pil.save(tmp)
        return tmp
    except Exception:
        return None

def classify_document(file_path: str) -> dict:
    """Main entry point: classify a PDF, an image (Claude vision), or a .docx."""
    config = load_config()
    ext = os.path.splitext(file_path)[1].lower()
    vendor_hint = os.path.splitext(os.path.basename(file_path))[0]

    if ext in IMAGE_EXTS:
        result = classify_with_claude("", config, vendor_hint=vendor_hint,
                                      image_path=file_path,
                                      image_media=IMAGE_MEDIA.get(ext, "image/jpeg"))
        # Non-empty so the PDF-only blank filter never applies to an image bill.
        result["extracted_text"] = "[image document]"
        return result

    if ext == ".docx":
        text = extract_text_from_docx(file_path)
        form_values = ""
    else:
        text = extract_text_from_pdf(file_path)
        # Deliberately NOT folded into `text`: the image-only gate below keys off
        # the page text, and a form's field values must never make a scanned PDF
        # look like it has a readable text layer and skip the vision fallback.
        form_values = extract_pdf_form_values(file_path)

    # Fast path: a searchable cover sheet identifies itself by its marker text.
    if text and COVER_SHEET_MARKER.lower() in text.lower():
        return {
            "is_cover_sheet": True,
            "received_date": _cover_sheet_date_from_text(text),
            "entity_code": None, "entity_name": None,
            "property_codes": [], "property_addresses": [],
            "doc_type": "Cover Sheet", "bill_type_code": None,
            "vendor": None, "amount": None, "due_date": None,
            "invoice_number": None, "account_number": None,
            "notes": "Batch cover sheet (sets the received date for the batch).",
            "ai_confidence": "High", "extracted_text": text[:500],
        }

    if not text.strip() or text.startswith("[PDF extraction error") or text.startswith("[DOCX extraction error"):
        # No text layer (image-only scan) or extraction failed. For PDFs, fall back
        # to Claude vision: render the first page, and for multi-page documents the
        # last page too — the office stamps the RECEIVED date on the back of each
        # document's final sheet, so the stamp only ever appears on the last page.
        # Exactly two images maximum, whatever the page count (cost control).
        if ext == ".pdf":
            imgs = []
            first = render_pdf_page_image(file_path, 0)
            if first:
                imgs.append(first)
                n_pages = pdf_page_count(file_path)
                if n_pages > 1:
                    last = render_last_page_image(file_path, n_pages - 1)
                    if last:
                        imgs.append(last)
            if imgs:
                try:
                    result = classify_with_claude("", config, vendor_hint=vendor_hint,
                                                  image_paths=imgs, image_media="image/png")
                    result["extracted_text"] = "[scanned PDF – read via vision]"
                    return result
                finally:
                    for img in imgs:
                        try:
                            os.remove(img)
                        except OSError:
                            pass
        return {
            "entity_code": None, "entity_name": None,
            "property_codes": [], "property_addresses": [],
            "doc_type": "Other", "bill_type_code": None,
            "vendor": None, "amount": None, "due_date": None,
            "invoice_number": None, "account_number": None,
            "notes": f"Could not extract text: {text}",
            "ai_confidence": "None",
            "extracted_text": ""
        }
    # Passed separately (not pre-concatenated) so classify_with_claude can
    # truncate `text` alone before appending form_values to the AI prompt —
    # see the note on its `form_values` parameter.
    result = classify_with_claude(text, config, vendor_hint=vendor_hint, form_values=form_values)
    result["extracted_text"] = (text + form_values)[:2000]
    return result
