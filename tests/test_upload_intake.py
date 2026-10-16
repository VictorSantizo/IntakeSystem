"""Upload-to-Review-Queue (Joshua 2026-08-03; the queue half of report #50).

POST /review/upload saves files under <scan_folder>\\Uploads and feeds them
through the SAME enqueue_scan path as a physical scan, so an upload gets the
full split/classify/dedup/auto-confirm treatment and can't bypass any guard.
"""
import io
import os
import sys

sys.path.insert(0, os.getcwd())
import app as klr
from conftest import make_pdf


def _pdf_bytes(tmp_path, name="upload.pdf", text=None):
    p = tmp_path / name
    if text is None:
        make_pdf(p)
    else:
        make_pdf(p, text)
    return p.read_bytes()


def test_upload_queues_document_with_upload_source(staff_client, env, tmp_path):
    c, headers = staff_client
    data = {"documents": (io.BytesIO(_pdf_bytes(tmp_path)), "Acme Bill.pdf")}
    r = c.post("/review/upload", data=data, headers=headers,
               content_type="multipart/form-data")
    assert r.status_code == 302
    assert "/review" in r.headers["Location"]
    assert "1 in the queue" in r.headers["Location"].replace("+", " ")
    items = [d for _, d in klr._queue_items()]
    assert len(items) == 1
    d = items[0]
    assert d["mail_type"] == "Upload"
    # Saved under the watched scan folder's Uploads subfolder (then possibly
    # renamed by the stager) — either way the queued file must live under it.
    assert d["pdf_path"].startswith(os.path.join(env.scan_folder, "Uploads"))
    assert os.path.exists(d["pdf_path"])
    # Uploads join today's batch so the redirect can land on it.
    assert d.get("batch_number")
    assert f"batch={d['batch_number']}" in r.headers["Location"]


def test_upload_duplicate_bytes_not_double_queued(staff_client, env, tmp_path):
    c, headers = staff_client
    raw = _pdf_bytes(tmp_path)
    for name in ("first.pdf", "second copy.pdf"):
        c.post("/review/upload",
               data={"documents": (io.BytesIO(raw), name)},
               headers=headers, content_type="multipart/form-data")
    # Hash dedup: identical bytes must not produce a second queue item.
    assert len(klr._queue_items()) == 1


def test_upload_rejects_unsupported_extension(staff_client, env, tmp_path):
    c, headers = staff_client
    r = c.post("/review/upload",
               data={"documents": (io.BytesIO(b"not a pdf"), "notes.docx")},
               headers=headers, content_type="multipart/form-data")
    assert r.status_code == 302
    assert "unsupported" in r.headers["Location"].lower()
    assert len(klr._queue_items()) == 0


def test_upload_requires_login(client, env, tmp_path):
    r = client.post("/review/upload",
                    data={"documents": (io.BytesIO(_pdf_bytes(tmp_path)), "a.pdf")},
                    content_type="multipart/form-data")
    # Anonymous callers bounce to login; nothing is saved or queued.
    assert r.status_code in (302, 401, 403)
    assert len(klr._queue_items()) == 0
    up_dir = os.path.join(env.scan_folder, "Uploads")
    assert not os.path.isdir(up_dir) or not os.listdir(up_dir)


def test_upload_multiple_files_one_batch(staff_client, env, tmp_path):
    c, headers = staff_client
    data = {"documents": [
        (io.BytesIO(_pdf_bytes(tmp_path, "a.pdf",
                    "INVOICE Alpha Plumbing $101.00 due 07/15/2026")), "alpha.pdf"),
        (io.BytesIO(_pdf_bytes(tmp_path, "b.pdf",
                    "INVOICE Beta Electric $202.00 due 07/20/2026")), "beta.pdf"),
    ]}
    r = c.post("/review/upload", data=data, headers=headers,
               content_type="multipart/form-data")
    assert r.status_code == 302
    items = [d for _, d in klr._queue_items()]
    assert len(items) == 2
    batches = {d.get("batch_number") for d in items}
    assert len(batches) == 1 and None not in batches
