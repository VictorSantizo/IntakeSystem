"""Document labels (2026-07-07): nothing on the dashboard's legal/important
surface may read "Unlabeled".

Server side: _doc_label falls back from vendor -> cleaned source filename ->
notes when the AI found no sender (blank lease forms, templates). Classifier
side: the prompt now requires a label for every document, and rental
applications are labeled by APPLICANT name, not the screening company —
verified live against the real PlayaSummit / Blue Iris application PDFs.
"""
import database
import app as klr
import classifier


# ------------------------------------------------------------- _doc_label

def test_label_prefers_vendor():
    e = {"vendor": "Malaga Bank", "pdf_path": r"F:\x\whatever_part3.pdf"}
    assert klr._doc_label(e) == "Malaga Bank"


def test_label_falls_back_to_cleaned_filename():
    e = {"vendor": "", "doc_type": "Legal",
         "pdf_path": r"F:\Email Scans\2026 draft Lease Lola Rao 160 Evans Avenue Unit 23_part4.pdf"}
    assert klr._doc_label(e) == "2026 draft Lease Lola Rao 160 Evans Avenue Unit 23"


def test_label_strips_duplicate_counter():
    e = {"vendor": None, "doc_type": "Bill",
         "filed_path": r"F:\Filed\Courthouse Retrieval System, Inc. (6).pdf"}
    assert klr._doc_label(e) == "Courthouse Retrieval System, Inc."


def test_label_skips_junk_stem_and_uses_notes():
    # "Legal (15).pdf" -> stem "Legal" = the doc type, carries no information
    e = {"vendor": "", "doc_type": "Legal", "pdf_path": r"F:\Email Scans\Legal (15).pdf",
         "notes": "Rental Agreement Addendum regarding mold notification."}
    assert klr._doc_label(e) == "Rental Agreement Addendum regarding mold notification."


def test_label_truncates_long_notes():
    e = {"vendor": "", "doc_type": "Legal", "pdf_path": "", "notes": "x" * 200}
    label = klr._doc_label(e)
    assert len(label) == 71 and label.endswith("…")


def test_label_last_resort():
    assert klr._doc_label({"vendor": "", "doc_type": "Legal"}) == "Untitled document"


# ------------------------------------------------------ dashboard rendering

def test_dashboard_never_says_unlabeled(env, staff_client):
    c, headers = staff_client
    database.insert_entry({
        "date_received": "2026-07-06", "doc_type": "Legal", "vendor": "",
        "status": "Pending",
        "pdf_path": r"F:\Email Scans\2026 draft Lease Lola Rao 160 Evans Avenue Unit 23_part4.pdf",
        "notes": "Blank template form for Addendum to Rental Agreement #2."})
    html = c.get("/").get_data(as_text=True)
    assert "Unlabeled" not in html
    assert "2026 draft Lease Lola Rao 160 Evans Avenue Unit 23" in html


def test_dashboard_still_shows_vendor_when_present(env, staff_client):
    c, headers = staff_client
    database.insert_entry({
        "date_received": "2026-07-06", "doc_type": "Legal",
        "vendor": "Courthouse Retrieval System, Inc.", "status": "Pending"})
    html = c.get("/").get_data(as_text=True)
    assert "Courthouse Retrieval System, Inc." in html


# ----------------------------------------------------- classifier prompt rules

def test_prompt_labels_rental_applications_by_applicant():
    rules = " ".join(classifier.DOMAIN_RULES.split())   # collapse line wraps
    assert "RENTAL APPLICATIONS" in rules
    assert "Rental Application - <applicant's name>" in rules
    assert "NOT the screening company" in rules


def test_prompt_requires_a_label_for_every_document():
    import inspect
    src = inspect.getsource(classifier.classify_with_claude)
    assert "must never be null or empty" in src


def test_prompt_keeps_blank_forms_out_of_legal():
    # A blank/unexecuted template must be doc_type Other, not Legal, so it does
    # not land on the dashboard's legal/important "needs attention" surface.
    rules = " ".join(classifier.DOMAIN_RULES.split())
    assert "LEGAL vs BOILERPLATE" in rules
    assert 'is doc_type "Other", NOT' in rules
    assert "important=false" in rules
    # and blank forms get a specific descriptive label, not a bare "Addendum"
    assert "Blank Lease Addendum - Mold Notification" in rules
    assert 'Never leave "vendor" null' in classifier.DOMAIN_RULES
