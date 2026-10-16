"""Properties & Units editor — extracted from app.py in the modular split (GH #6),
same add_url_rule pattern as routes_karen.py (see its docstring for why these are
not Blueprints).

Grew out of design question #7 (units as first-class data, reports #20 and #54).
The page used to edit one number per property; now that the `units` table is
actually populated it edits the units themselves, and the number is derived from
them. The old number is kept editable for properties that legitimately have no
units — single-family homes and offices — since there's nothing to count there.
"""
from flask import jsonify, render_template, request

from property_model import (add_unit, delete_unit, get_properties,
                            get_units_by_property, rename_unit,
                            unit_reference_count, update_property_units)

_M = None   # the app module (composition root); set by register()


def register(app, appmod):
    global _M
    _M = appmod
    adm = appmod.admin_required
    r = app.add_url_rule
    r("/settings/property-units", "property_units", adm(property_units))
    r("/api/property/<code>/units", "api_set_property_units",
      adm(api_set_property_units), methods=["POST"])
    r("/api/property/<code>/unit", "api_add_unit", adm(api_add_unit), methods=["POST"])
    r("/api/unit/<int:unit_id>", "api_update_unit", adm(api_update_unit), methods=["POST"])
    r("/api/unit/<int:unit_id>/delete", "api_delete_unit",
      adm(api_delete_unit), methods=["POST"])


def property_units():
    """Review + correct the seeded units. Unit counts weight the proportional
    bill-split across entities (#2), so what's listed here is money math, not
    just an inventory."""
    return render_template("property_units.html",
                           properties=get_properties(),
                           units_by_property=get_units_by_property())


def api_set_property_units(code):
    """Inline save of a hand-typed unit count — only meaningful for a property with
    no unit rows; once units exist the count is derived from them."""
    data = request.get_json(silent=True) or {}
    update_property_units(code, data.get("unit_count"))
    return jsonify({"ok": True})


def api_add_unit(code):
    data = request.get_json(silent=True) or {}
    label = (data.get("label") or "").strip()
    if not add_unit(code, label):
        return jsonify({"ok": False,
                        "error": f"{code}-{label} already exists" if label
                                 else "Enter a unit number"}), 400
    return jsonify({"ok": True})


def api_update_unit(unit_id):
    data = request.get_json(silent=True) or {}
    if not rename_unit(unit_id, data.get("label")):
        return jsonify({"ok": False, "error": "That unit number is already taken"}), 400
    return jsonify({"ok": True})


def api_delete_unit(unit_id):
    """Prune a unit. Refuses while records still reference it — deleting one out from
    under a filed bill would orphan that bill's unit exactly the way the June filed-path
    drift orphaned records' PDFs."""
    n = unit_reference_count(unit_id)
    if n:
        return jsonify({"ok": False,
                        "error": f"Still on {n} record{'s' if n != 1 else ''}"}), 409
    if not delete_unit(unit_id):
        return jsonify({"ok": False, "error": "No such unit"}), 404
    return jsonify({"ok": True})
