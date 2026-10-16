"""Settings & admin management routes — extracted from app.py in the
2026-07-29 modular split (GH #6). Covers the /settings page, entities,
properties, bill types, mail accounts, staff, and the /account password page.

Same pattern as routes_karen.py (see its docstring): registered from app.py's
bottom via register(app, appmod) under the ORIGINAL endpoint names; app-module
helpers (load_config/save_config/_sync_model/...) are reached through the
module object so test monkeypatching keeps working; decorators are applied at
registration in the same order the @-stack used (admin_required outermost).
"""
import re
import threading

from flask import jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import generate_password_hash

import email_backfill
import email_poller
import queue_state
from database import log_activity, rename_bill_type_code, rename_entity_code
from property_model import (property_reference_count, sync_entity_deactivate,
                            sync_entity_rename, sync_entity_upsert,
                            sync_property_deactivate, sync_property_upsert)

_M = None   # the app module (composition root); set by register()


def register(app, appmod):
    global _M
    _M = appmod
    adm, cm = appmod.admin_required, appmod.config_mutating
    r = app.add_url_rule
    r("/settings", "settings", adm(cm(settings)), methods=["GET", "POST"])
    r("/account", "account", account)
    r("/account/change_password", "change_password", cm(change_password), methods=["POST"])
    r("/settings/entities", "manage_entities", adm(manage_entities))
    r("/settings/entities/add", "add_entity", adm(cm(add_entity)), methods=["POST"])
    r("/settings/entities/<code>/update", "update_entity", adm(cm(update_entity)), methods=["POST"])
    r("/settings/entities/<code>/delete", "delete_entity", adm(cm(delete_entity)), methods=["POST"])
    r("/settings/properties", "manage_properties", adm(manage_properties))
    r("/settings/properties/add", "add_property", adm(cm(add_property)), methods=["POST"])
    r("/settings/properties/<code>/update", "update_property", adm(cm(update_property)), methods=["POST"])
    r("/settings/properties/<code>/delete", "delete_property", adm(cm(delete_property)), methods=["POST"])
    r("/settings/bill_types", "manage_bill_types", adm(manage_bill_types))
    r("/settings/bill_types/add", "add_bill_type", adm(cm(add_bill_type)), methods=["POST"])
    r("/api/bill_types/add", "api_add_bill_type", cm(api_add_bill_type), methods=["POST"])
    r("/settings/bill_types/<code>/update", "update_bill_type", adm(cm(update_bill_type)), methods=["POST"])
    r("/settings/bill_types/<code>/delete", "delete_bill_type", adm(cm(delete_bill_type)), methods=["POST"])
    r("/settings/email_backfill", "email_backfill_run", adm(email_backfill_run),
      methods=["POST"])
    r("/settings/mail/add", "add_mail_account", adm(cm(add_mail_account)), methods=["POST"])
    r("/settings/mail/<int:acct_id>/update", "update_mail_account", adm(cm(update_mail_account)), methods=["POST"])
    r("/settings/mail/<int:acct_id>/delete", "delete_mail_account", adm(cm(delete_mail_account)), methods=["POST"])
    r("/settings/staff/add", "add_staff", adm(cm(add_staff)), methods=["POST"])
    r("/settings/staff/password", "reset_staff_password", adm(cm(reset_staff_password)), methods=["POST"])
    r("/settings/staff/toggle_admin", "toggle_staff_admin", adm(cm(toggle_staff_admin)), methods=["POST"])
    r("/settings/staff/remove", "remove_staff", adm(cm(remove_staff)), methods=["POST"])


# ── Settings page ─────────────────────────────────────────────────────────────

def settings():
    cfg = _M.load_config()
    if request.method == "POST":
        cfg["scan_folder"]  = request.form.get("scan_folder", cfg["scan_folder"])
        cfg["email_folder"] = request.form.get("email_folder", cfg.get("email_folder",""))
        cfg["filed_folder"] = request.form.get("filed_folder", cfg["filed_folder"])
        new_key = request.form.get("claude_api_key", "").strip()
        if new_key:
            cfg["claude_api_key"] = new_key
        # blank submission keeps the existing key — the field is never
        # pre-filled with the real value (see settings.html), so an unchanged
        # save must not wipe it out.
        mode = request.form.get("auto_confirm", "").strip().lower()
        if mode in ("off", "known-vendors", "aggressive"):
            cfg["auto_confirm"] = mode
        _M.save_config(cfg)
        return redirect(url_for("settings", _anchor="tab=intake"))
    return render_template("settings.html", cfg=cfg,
                           mail_status=email_poller.get_poll_status(),
                           mail_lookback_default=email_poller.DEFAULT_LOOKBACK_NEW,
                           running_code=_M._RUNNING_CODE, app_started=_M._APP_STARTED,
                           disk_code=_M._code_version() or "unknown",
                           backfill_running=queue_state.job_running("email_backfill"),
                           msg=request.args.get("msg"), err=request.args.get("err"))

def email_backfill_run():
    """Reports #55/#8/#26 (Joshua's go-ahead 8/6) — retro-attach source emails
    to email-less records and queue items by re-reading the office mailboxes
    and matching attachments by exact file hash. Runs IN the app on a
    background thread: queue items live in the app's memory, so only the app
    itself may patch them. Mailboxes are only ever read (PEEK/TOP)."""
    if not queue_state.try_start_job("email_backfill"):
        return redirect(url_for("settings", _anchor="tab=app",
                                msg="The email backfill is already running."))

    def worker():
        try:
            email_backfill.run(_M.load_config(), apply=True,
                               queue_items=queue_state.queue_items())
        except Exception as e:
            # Loud and complete: the 2026-08-07 apply's fate was undiagnosable
            # from a bare one-line print. run() has already written the
            # artifact (marked "stopped early") by the time this fires.
            import traceback
            print(f"[EmailBackfill] failed: {type(e).__name__}: {e}")
            traceback.print_exc()
        finally:
            queue_state.finish_job("email_backfill")

    threading.Thread(target=worker, daemon=True,
                     name="email-backfill").start()
    return redirect(url_for("settings", _anchor="tab=app",
                            msg="Email backfill started — results land in "
                                "Ops Reports as 'Email backfill' when it "
                                "finishes (a few minutes per mailbox)."))


# ── Account — change your own password (available to every logged-in user) ─────

def account():
    return render_template("account.html", pw_success=request.args.get("pw_success"), pw_error=None)

def change_password():
    cfg = _M.load_config()
    current = request.form.get("current_password", "")
    new_pw  = request.form.get("new_password", "")
    confirm = request.form.get("confirm_password", "")
    user = session.get("user")
    staff_entry = next((s for s in cfg.get("staff", []) if s.get("name") == user), None)
    if not staff_entry or not _M._check_staff_password(staff_entry, current):
        return render_template("account.html", pw_error="Current password is incorrect.", pw_success=None)
    if not new_pw:
        return render_template("account.html", pw_error="New password cannot be blank.", pw_success=None)
    if new_pw != confirm:
        return render_template("account.html", pw_error="New passwords do not match.", pw_success=None)
    staff_entry["password"] = generate_password_hash(new_pw, method="pbkdf2:sha256")
    _M.save_config(cfg)
    return redirect(url_for("account", pw_success=1))

# ── Entities & Properties management (admin only) ─────────────────────────────

def manage_entities():
    return render_template("entities_manage.html", cfg=_M.load_config(),
                           msg=request.args.get("msg"), err=request.args.get("err"))

def add_entity():
    cfg = _M.load_config()
    code = request.form.get("code", "").strip()
    name = request.form.get("name", "").strip()
    if not code or not name:
        return redirect(url_for("manage_entities", err="Code and name are both required."))
    if any(e["code"].lower() == code.lower() for e in cfg["entities"]):
        return redirect(url_for("manage_entities", err=f"Entity code '{code}' already exists."))
    cfg["entities"].append({"code": code, "name": name})
    _M.save_config(cfg)
    _M._sync_model(sync_entity_upsert, code, name)
    log_activity(None, "Entity added", session.get("user", ""), f"{code} — {name}")
    return redirect(url_for("manage_entities", msg=f"Added entity {code}."))

def update_entity(code):
    cfg = _M.load_config()
    ent = next((e for e in cfg["entities"] if e["code"] == code), None)
    new_code = request.form.get("code", "").strip()
    name = request.form.get("name", "").strip()
    if not ent:
        return redirect(url_for("manage_entities", err="Entity not found."))
    if not new_code or not name:
        return redirect(url_for("manage_entities", err="Code and name are both required."))
    if new_code != code and any(e["code"].lower() == new_code.lower()
                                for e in cfg["entities"] if e is not ent):
        return redirect(url_for("manage_entities", err=f"Entity code '{new_code}' already exists."))
    # Changing the code cascades to every property + record that references it.
    if new_code != code:
        for p in cfg["properties"]:
            if p.get("entity_code") == code:
                p["entity_code"] = new_code
    ent["code"] = new_code
    ent["name"] = name
    _M.save_config(cfg)
    rename_entity_code(code, new_code, name)
    if new_code != code:
        _M._sync_model(sync_entity_rename, code, new_code, name)
    else:
        _M._sync_model(sync_entity_upsert, new_code, name)
    msg = (f"Renamed {code} → {new_code} (properties & records updated)."
           if new_code != code else f"Updated {code}.")
    log_activity(None, "Entity renamed" if new_code != code else "Entity updated",
                 session.get("user", ""), f"{code} → {new_code} ({name})")
    return redirect(url_for("manage_entities", msg=msg))

def delete_entity(code):
    cfg = _M.load_config()
    refs = [p["code"] for p in cfg["properties"] if p.get("entity_code") == code]
    if refs:
        return redirect(url_for("manage_entities",
            err=f"Can't delete {code}: {len(refs)} propert{'ies' if len(refs)!=1 else 'y'} still use it. Reassign them first."))
    cfg["entities"] = [e for e in cfg["entities"] if e["code"] != code]
    _M.save_config(cfg)
    _M._sync_model(sync_entity_deactivate, code)
    log_activity(None, "Entity deleted", session.get("user", ""), code)
    return redirect(url_for("manage_entities", msg=f"Deleted entity {code}."))

def _code_sort_key(code):
    """Natural sort: 'T27' before 'T131', not after."""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r'(\d+)', code or "")]

def manage_properties():
    cfg = _M.load_config()
    props = sorted(cfg.get("properties", []), key=lambda p: _code_sort_key(p.get("code", "")))
    return render_template("properties_manage.html", cfg=cfg, properties=props,
                           msg=request.args.get("msg"), err=request.args.get("err"))

def add_property():
    cfg = _M.load_config()
    code = request.form.get("code", "").strip()
    address = request.form.get("address", "").strip()
    if not code or not address:
        return redirect(url_for("manage_properties", err="Code and address are both required."))
    if any(p["code"].lower() == code.lower() for p in cfg["properties"]):
        return redirect(url_for("manage_properties", err=f"Property code '{code}' already exists."))
    prop = {"code": code, "address": address,
            "entity_code": request.form.get("entity_code", "").strip()}
    note = request.form.get("note", "").strip()
    if note:
        prop["note"] = note
    cfg["properties"].append(prop)
    _M.save_config(cfg)
    _M._sync_model(sync_property_upsert, code, address, prop["entity_code"], note)
    log_activity(None, "Property added", session.get("user", ""), f"{code} — {address}")
    return redirect(url_for("manage_properties", msg=f"Added property {code}."))

def update_property(code):
    cfg = _M.load_config()
    prop = next((p for p in cfg["properties"] if p["code"] == code), None)
    address = request.form.get("address", "").strip()
    if not prop:
        return redirect(url_for("manage_properties", err="Property not found."))
    if not address:
        return redirect(url_for("manage_properties", err="Address cannot be blank."))
    prop["address"] = address
    prop["entity_code"] = request.form.get("entity_code", "").strip()
    note = request.form.get("note", "").strip()
    if note:
        prop["note"] = note
    elif "note" in prop:
        del prop["note"]
    _M.save_config(cfg)
    _M._sync_model(sync_property_upsert, code, address, prop["entity_code"], note)
    log_activity(None, "Property updated", session.get("user", ""), code)
    return redirect(url_for("manage_properties", msg=f"Updated {code}."))

def delete_property(code):
    # 2026-07-25 code review, P1-10: mirror delete_entity's reference-integrity
    # guard -- deleting a property still referenced by historical bills silently
    # orphans those records' property_code (breaks cabinet drawers, unit-weight
    # splitting, dropdowns). property_reference_count (property_model.py) already
    # existed for sync_property_deactivate's own best-effort guard, but that guard
    # runs too late (after config.json is already saved) and its exception is
    # swallowed by _sync_model -- so the actual fix has to check and refuse here,
    # before config.json is touched, same as delete_entity does.
    refs = property_reference_count(code)
    if refs:
        return redirect(url_for("manage_properties",
            err=f"Can't delete {code}: {refs} record{'s' if refs != 1 else ''} still reference it. Reassign or leave it in place."))
    cfg = _M.load_config()
    cfg["properties"] = [p for p in cfg["properties"] if p["code"] != code]
    _M.save_config(cfg)
    _M._sync_model(sync_property_deactivate, code)
    log_activity(None, "Property deleted", session.get("user", ""), code)
    return redirect(url_for("manage_properties", msg=f"Deleted property {code}."))

# ── Bill types management (admin only) ────────────────────────────────────────

_BILL_CATEGORIES = ["Bill", "Income", "Statement"]

def manage_bill_types():
    return render_template("bill_types_manage.html", cfg=_M.load_config(),
                           categories=_BILL_CATEGORIES,
                           msg=request.args.get("msg"), err=request.args.get("err"))

def add_bill_type():
    cfg = _M.load_config()
    cfg.setdefault("bill_types", [])
    code = request.form.get("code", "").strip()
    desc = request.form.get("description", "").strip()
    category = request.form.get("category", "Bill").strip() or "Bill"
    if not code or not desc:
        return redirect(url_for("manage_bill_types", err="Code and description are both required."))
    if any(b["code"].lower() == code.lower() for b in cfg["bill_types"]):
        return redirect(url_for("manage_bill_types", err=f"Bill type code '{code}' already exists."))
    cfg["bill_types"].append({"code": code, "description": desc, "category": category})
    _M.save_config(cfg)
    log_activity(None, "Bill type added", session.get("user", ""), f"{code} — {desc}")
    return redirect(url_for("manage_bill_types", msg=f"Added bill type {code}."))

def api_add_bill_type():
    """Inline bill-type creation from the review form (Tier 3 #20) — any logged-in
    reviewer, no Settings trip. Returns JSON. Idempotent: an existing code (case-
    insensitive) is just returned, not duplicated."""
    data = request.get_json(silent=True) or {}
    code = (data.get("code") or "").strip()
    desc = (data.get("description") or "").strip() or code
    category = (data.get("category") or "Bill").strip() or "Bill"
    if not code:
        return jsonify({"ok": False, "error": "A code is required."}), 400
    cfg = _M.load_config()
    cfg.setdefault("bill_types", [])
    existing = next((b for b in cfg["bill_types"] if b["code"].lower() == code.lower()), None)
    if existing:
        return jsonify({"ok": True, "code": existing["code"],
                        "description": existing.get("description", ""), "existed": True})
    cfg["bill_types"].append({"code": code, "description": desc, "category": category})
    _M.save_config(cfg)
    print(f"[Config] Bill type '{code}' added inline by {session.get('user','?')}")
    return jsonify({"ok": True, "code": code, "description": desc, "existed": False})

def update_bill_type(code):
    cfg = _M.load_config()
    bt = next((b for b in cfg.get("bill_types", []) if b["code"] == code), None)
    new_code = request.form.get("code", "").strip()
    desc = request.form.get("description", "").strip()
    category = request.form.get("category", "Bill").strip() or "Bill"
    if not bt:
        return redirect(url_for("manage_bill_types", err="Bill type not found."))
    if not new_code or not desc:
        return redirect(url_for("manage_bill_types", err="Code and description are both required."))
    if new_code != code and any(b["code"].lower() == new_code.lower()
                                for b in cfg["bill_types"] if b is not bt):
        return redirect(url_for("manage_bill_types", err=f"Bill type code '{new_code}' already exists."))
    bt["code"], bt["description"], bt["category"] = new_code, desc, category
    _M.save_config(cfg)
    if new_code != code:
        rename_bill_type_code(code, new_code)
    msg = (f"Renamed {code} → {new_code} (records updated)."
           if new_code != code else f"Updated {code}.")
    log_activity(None, "Bill type renamed" if new_code != code else "Bill type updated",
                 session.get("user", ""), f"{code} → {new_code} ({desc})")
    return redirect(url_for("manage_bill_types", msg=msg))

def delete_bill_type(code):
    cfg = _M.load_config()
    cfg["bill_types"] = [b for b in cfg.get("bill_types", []) if b["code"] != code]
    _M.save_config(cfg)
    log_activity(None, "Bill type deleted", session.get("user", ""), code)
    return redirect(url_for("manage_bill_types", msg=f"Deleted bill type {code}."))

# ── Mail accounts management (admin only — the mailboxes email intake polls) ──

def _mail_account_from_form(existing=None):
    """Build a mail_accounts entry from the Settings form fields. A blank
    password keeps the saved one when editing (the field never echoes it back).
    Returns (account, error_or_None)."""
    protocol = request.form.get("protocol", "imap").strip().lower()
    if protocol not in ("imap", "pop3"):
        protocol = "imap"
    use_ssl = request.form.get("ssl") == "on"
    host = request.form.get("host", "").strip()
    username = request.form.get("username", "").strip()
    if not host or not username:
        return None, "Mail server (host) and username are both required."
    port_raw = request.form.get("port", "").strip()
    try:
        # Blank port = the protocol's standard one.
        port = int(port_raw) if port_raw else \
            {("imap", True): 993, ("imap", False): 143,
             ("pop3", True): 995, ("pop3", False): 110}[(protocol, use_ssl)]
        lookback = int(request.form.get("lookback_days", "").strip()
                       or email_poller.DEFAULT_LOOKBACK_NEW)
    except ValueError:
        return None, "Port and lookback days must be numbers."
    # Providers show app passwords in spaced groups — store without the spaces.
    password = request.form.get("password", "").strip().replace(" ", "")
    if not password and existing:
        password = existing.get("password", "")
    return {"name": request.form.get("name", "").strip() or username,
            "protocol": protocol, "host": host, "port": port, "ssl": use_ssl,
            "username": username, "password": password, "enabled": True,
            "lookback_days": max(1, min(lookback, 365))}, None

def add_mail_account():
    cfg = _M.load_config()
    cfg.setdefault("mail_accounts", [])
    acct, err = _mail_account_from_form()
    if err:
        return redirect(url_for("settings", err=err, _anchor="tab=mail"))
    if not acct["password"]:
        return redirect(url_for("settings", err="A password is required for a new mailbox.", _anchor="tab=mail"))
    acct["id"] = max((a.get("id", 0) for a in cfg["mail_accounts"]), default=0) + 1
    cfg["mail_accounts"].append(acct)
    _M.save_config(cfg)
    log_activity(None, "Mailbox added", session.get("user", ""),
                 f"{acct['name']} ({acct['protocol']}://{acct['host']})")
    return redirect(url_for("settings", _anchor="tab=mail",
        msg=f"Added mailbox {acct['name']} — it's polled within 5 minutes, or click Check for new items."))

def update_mail_account(acct_id):
    cfg = _M.load_config()
    acct = next((a for a in cfg.get("mail_accounts", []) if a.get("id") == acct_id), None)
    if not acct:
        return redirect(url_for("settings", err="Mailbox not found.", _anchor="tab=mail"))
    new, err = _mail_account_from_form(existing=acct)
    if err:
        return redirect(url_for("settings", err=err, _anchor="tab=mail"))
    new["id"] = acct_id
    new["enabled"] = request.form.get("enabled") == "on"
    cfg["mail_accounts"][cfg["mail_accounts"].index(acct)] = new
    _M.save_config(cfg)
    log_activity(None, "Mailbox updated", session.get("user", ""), new["name"])
    return redirect(url_for("settings", msg=f"Updated mailbox {new['name']}.", _anchor="tab=mail"))

def delete_mail_account(acct_id):
    cfg = _M.load_config()
    accounts = cfg.get("mail_accounts", [])
    acct = next((a for a in accounts if a.get("id") == acct_id), None)
    if not acct:
        return redirect(url_for("settings", err="Mailbox not found.", _anchor="tab=mail"))
    accounts.remove(acct)
    cfg["mail_accounts"] = accounts
    _M.save_config(cfg)
    log_activity(None, "Mailbox deleted", session.get("user", ""),
                 acct.get("name") or acct.get("username") or "")
    return redirect(url_for("settings", msg=f"Deleted mailbox {acct.get('name') or acct.get('username')}.", _anchor="tab=mail"))

# ── Staff management (admin only) ───────────────────────────────────────────
# Before this, the only way to add a user was hand-editing config.json and
# restarting. login() calls load_config() fresh on every attempt (no caching),
# so every route below takes effect immediately — a brand-new staff member can
# sign in right away, no restart needed.

# 8 since 2026-07-31 (design q4, Joshua's blanket go-ahead; was 4 -- thin for
# a financial-document system). Applies only where this constant is checked:
# NEW staff passwords and resets. Existing stored hashes are untouched; each
# account upgrades naturally at its next reset.
_MIN_STAFF_PASSWORD_LEN = 8

def add_staff():
    cfg = _M.load_config()
    cfg.setdefault("staff", [])
    name = request.form.get("name", "").strip()
    password = request.form.get("password", "")
    if not name:
        return redirect(url_for("settings", err="Name is required.", _anchor="tab=staff"))
    if any((s.get("name") or "").lower() == name.lower() for s in cfg["staff"]):
        return redirect(url_for("settings", err=f"'{name}' is already a staff member.", _anchor="tab=staff"))
    if len(password) < _MIN_STAFF_PASSWORD_LEN:
        return redirect(url_for("settings",
            err=f"Password must be at least {_MIN_STAFF_PASSWORD_LEN} characters.", _anchor="tab=staff"))
    is_admin = request.form.get("admin") == "on"
    cfg["staff"].append({"name": name,
                          "password": generate_password_hash(password, method="pbkdf2:sha256"),
                          "admin": is_admin})
    _M.save_config(cfg)
    log_activity(None, "Staff added", session.get("user", ""),
                 f"{name}{' (admin)' if is_admin else ''}")
    return redirect(url_for("settings", msg=f"Added {name}.", _anchor="tab=staff"))

def reset_staff_password():
    cfg = _M.load_config()
    name = request.form.get("name", "").strip()
    password = request.form.get("password", "")
    entry = next((s for s in cfg.get("staff", []) if s.get("name") == name), None)
    if not entry:
        return redirect(url_for("settings", err="Staff member not found.", _anchor="tab=staff"))
    if len(password) < _MIN_STAFF_PASSWORD_LEN:
        return redirect(url_for("settings",
            err=f"Password must be at least {_MIN_STAFF_PASSWORD_LEN} characters.", _anchor="tab=staff"))
    entry["password"] = generate_password_hash(password, method="pbkdf2:sha256")
    _M.save_config(cfg)
    log_activity(None, "Staff password reset", session.get("user", ""), name)
    return redirect(url_for("settings", msg=f"Password reset for {name}.", _anchor="tab=staff"))

def toggle_staff_admin():
    cfg = _M.load_config()
    name = request.form.get("name", "").strip()
    entry = next((s for s in cfg.get("staff", []) if s.get("name") == name), None)
    if not entry:
        return redirect(url_for("settings", err="Staff member not found.", _anchor="tab=staff"))
    if entry.get("admin"):
        # Demoting: never let an admin take away their own access, and never
        # drop the admin count to zero (an office with no admin can't manage
        # itself — not even by editing config.json, since that also needs an
        # admin-gated route or a restart).
        if entry.get("name") == session.get("user"):
            return redirect(url_for("settings",
                err="You can't remove your own admin access.", _anchor="tab=staff"))
        admins = [s for s in cfg.get("staff", []) if s.get("admin")]
        if len(admins) <= 1:
            return redirect(url_for("settings",
                err=f"Can't remove admin access from {name} — at least one admin must remain.",
                _anchor="tab=staff"))
        entry["admin"] = False
        msg = f"{name} is no longer an admin."
    else:
        entry["admin"] = True
        msg = f"{name} is now an admin."
    _M.save_config(cfg)
    log_activity(None, "Staff admin toggled", session.get("user", ""), msg)
    return redirect(url_for("settings", msg=msg, _anchor="tab=staff"))

def remove_staff():
    cfg = _M.load_config()
    name = request.form.get("name", "").strip()
    entry = next((s for s in cfg.get("staff", []) if s.get("name") == name), None)
    if not entry:
        return redirect(url_for("settings", err="Staff member not found.", _anchor="tab=staff"))
    if entry.get("name") == session.get("user"):
        return redirect(url_for("settings", err="You can't remove your own account.", _anchor="tab=staff"))
    if entry.get("admin"):
        admins = [s for s in cfg.get("staff", []) if s.get("admin")]
        if len(admins) <= 1:
            return redirect(url_for("settings",
                err=f"Can't remove {name} — at least one admin must remain.", _anchor="tab=staff"))
    cfg["staff"] = [s for s in cfg.get("staff", []) if s.get("name") != name]
    _M.save_config(cfg)
    log_activity(None, "Staff removed", session.get("user", ""), name)
    return redirect(url_for("settings", msg=f"Removed {name}.", _anchor="tab=staff"))
