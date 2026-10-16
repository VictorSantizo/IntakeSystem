"""The "new look" — Klick Klack's second view, Outlook-style.

The 2026-07-31 UI redesign (branch ui-redesign-sample: 6 static mockups +
ui-redesign/IMPLEMENTATION.md, the visual source of truth) ships as a SECOND
set of templates under templates/new/ instead of an in-place restyle. Each
person opts in and can hop back instantly via the always-visible top-right
switch (_skin_switch.html — staff-facing names: "Classy" = new, "Colorful" =
classic; these internal keys predate the names and stay put because they live
in config.json staff entries). The choice is per-person and survives moving
desks because it lives on their staff entry, not in a browser.

The rollout trick is the fallback in render_skinned(): a page that hasn't been
rebuilt yet simply renders its classic template, so the new look lands one
page at a time (dashboard first, 2026-08-05) without ever touching what
classic users see. To rebuild a page: create templates/new/<name>.html and
route its view through render_skinned — nothing else.

Same registration pattern as routes_karen.py: register(app, appmod) under
original-style endpoint names; app-module helpers reached through the module
object so test monkeypatching keeps working.
"""
from flask import (has_request_context, redirect, render_template, request,
                   session, url_for)
from jinja2 import TemplateNotFound

_M = None   # the app module (composition root); set by register()

SKINS = ("classic", "new")


def register(app, appmod):
    global _M
    _M = appmod
    app.add_url_rule("/account/skin", "set_skin",
                     appmod.config_mutating(set_skin), methods=["POST"])

    @app.context_processor
    def _inject_skin():
        # The Classy/Colorful switch (_skin_switch.html) lives in both chromes
        # and reads the person's SAVED pick — so even a classic-rendered page
        # (not yet rebuilt) shows the switch in the right position.
        return {"ui_skin":
                current_skin() if has_request_context() else "classic"}


def current_skin():
    """Which look this person sees: "classic" (default) or "new".
    Session-cached after the first read so the config file isn't re-parsed on
    every page; set_skin refreshes both copies."""
    user = session.get("user")
    if not user:
        return "classic"
    sk = session.get("ui_skin")
    if sk in SKINS:
        return sk
    entry = next((s for s in _M.load_config().get("staff", [])
                  if s.get("name") == user), None)
    sk = (entry or {}).get("ui_skin")
    sk = sk if sk in SKINS else "classic"
    session["ui_skin"] = sk
    return sk


def render_skinned(template_name, **context):
    """render_template, honoring the person's look. On the new look, render
    templates/new/<name> when that file exists; otherwise (and for everyone on
    classic) render the classic template. Only the new template's *absence*
    falls back — an error inside an existing new template still raises, so a
    broken page can't silently masquerade as "not rebuilt yet"."""
    if current_skin() == "new":
        try:
            _M.app.jinja_env.get_template("new/" + template_name)
        except TemplateNotFound:
            pass
        else:
            return render_template("new/" + template_name, **context)
    return render_template(template_name, **context)


def set_skin():
    """Flip this person between looks. Durable copy on their staff entry (so
    it follows them to any machine), working copy in the session."""
    skin = (request.form.get("skin") or "").strip().lower()
    if skin not in SKINS:
        skin = "classic"
    cfg = _M.load_config()
    entry = next((s for s in cfg.get("staff", [])
                  if s.get("name") == session.get("user")), None)
    if entry is not None and entry.get("ui_skin") != skin:
        entry["ui_skin"] = skin
        _M.save_config(cfg)
    session["ui_skin"] = skin
    # Land on the page that shows the change. ?full=1 keeps Karen on the real
    # dashboard instead of her /home bounce, so the toggle never looks dead.
    if skin == "new":
        return redirect(url_for("dashboard", full=1))
    return redirect(url_for("dashboard"))
