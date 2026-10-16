"""The "physical office" paper views — /desk, /cabinet, /rolodex, /intray,
/calendar, /board, /tray — extracted from app.py in the 2026-07-29 modular
split (GH #6). Same pattern as routes_karen.py (see its docstring): original
endpoint names via add_url_rule, app-module helpers through _M so tests'
monkeypatching keeps working. Alternate presentations over the same queries
the dashboard and Catch Up already use — read-only except the board's
take-down and the in-tray's clear-missing (both unchanged write paths).
The calendar deliberately has no pay action: Karen pays in received order
from the stack; the calendar is for watching due dates.
"""
import os
from datetime import date, datetime, timedelta

from flask import redirect, render_template, request, session, url_for

from database import (confirm_counts_for_day, count_taken_down_since,
                      docs_for_entity_month, entity_doc_counts,
                      find_vendor_clusters, get_bills_by_due_date,
                      get_bills_for_month, get_bills_for_vendor,
                      get_board_items, get_dashboard_stats, get_entry,
                      get_expected_bills, get_queue_depth_history,
                      get_vendor_directory, get_vendor_hint_exact,
                      log_activity, months_for_entity, record_queue_depth,
                      rename_vendor, update_entry)

_M = None   # the app module (composition root); set by register()

_TRAY_TRIAGE_LINE = 40   # depth where triage began the last time the queue blew up


def register(app, appmod):
    global _M
    _M = appmod
    adm = appmod.admin_required
    r = app.add_url_rule
    r("/calendar", "wall_calendar", wall_calendar)
    r("/board", "board", board)
    r("/board/<int:entry_id>/takedown", "board_takedown", board_takedown, methods=["POST"])
    r("/board/sweep", "board_sweep", board_sweep, methods=["POST"])
    r("/tray", "tray", tray)
    r("/desk", "desk", desk)
    r("/cabinet", "cabinet", cabinet)
    r("/rolodex", "rolodex", rolodex)
    r("/rolodex/merge_vendor", "merge_vendor", adm(merge_vendor), methods=["POST"])
    r("/intray", "intray", intray)
    r("/intray/clear_missing", "intray_clear_missing", intray_clear_missing, methods=["POST"])


def _fmt_day(iso: str) -> str:
    """'2026-07-05' -> 'Jul 5' (no %-d on Windows strftime)."""
    try:
        d = date.fromisoformat((iso or "")[:10])
        return f"{d.strftime('%b')} {d.day}"
    except ValueError:
        return iso or ""


def wall_calendar():
    """Bills-due wall calendar — the paper DUE DATES CHECKLIST, live. One slip
    per bill on its due date; expected-but-unseen bills stay in the margin
    (the radar knows the period, not the day)."""
    import calendar as calmod
    today = date.today()
    ym = request.args.get("month") or today.strftime("%Y-%m")
    try:
        first = date.fromisoformat(ym + "-01")
    except ValueError:
        first = today.replace(day=1)
        ym = first.strftime("%Y-%m")
    prev_m = (first - timedelta(days=1)).strftime("%Y-%m")
    next_m = (first + timedelta(days=32)).replace(day=1).strftime("%Y-%m")
    today_iso = today.isoformat()

    # Position of every unpaid bill in Karen's received-order pay stack.
    stack_pos = {b["id"]: i + 1 for i, b in enumerate(_M._catchup_received_rows())}

    bills = get_bills_for_month(ym)
    days_map = {}
    tallies = {"paid_count": 0, "paid_sum": 0.0, "stack_count": 0, "stack_sum": 0.0,
               "late_count": 0, "late_sum": 0.0}
    late_positions = []
    for b in bills:
        dd = (b.get("due_date") or "")[:10]
        amt = b.get("amount") or 0
        paid = b.get("status") in ("Paid", "Filed") or bool(b.get("paid_date"))
        if paid:
            state = "paid"
            tallies["paid_count"] += 1; tallies["paid_sum"] += amt
        elif dd < today_iso:
            state = "late"
            tallies["late_count"] += 1; tallies["late_sum"] += amt
            if stack_pos.get(b["id"]):
                late_positions.append(stack_pos[b["id"]])
        else:
            state = "stack"
        if state != "paid":
            tallies["stack_count"] += 1; tallies["stack_sum"] += amt
        days_map.setdefault(dd, []).append({
            "id": b["id"], "vendor": b.get("vendor") or "—", "amount": amt,
            "entity_code": b.get("entity_code") or "", "property_code": b.get("property_code") or "",
            "state": state, "due_today": dd == today_iso,
            "paid_label": (f"paid {_fmt_day(b.get('paid_date'))}"
                           + (f" · ck {b['check_number']}" if b.get("check_number") else ""))
                          if paid else "",
            "pos": stack_pos.get(b["id"]),
        })

    try:
        ghosts = [s for s in _M._expected_statuses() if s["state"] == "waiting"]
    except Exception:
        ghosts = []
    ghost_sum = sum(g.get("typical_amount") or 0 for g in ghosts)

    # Bills already received whose due date falls in NEXT month — the paper
    # calendar's blind spot, kept visible in the margin.
    next_in_stack = sum(1 for b in get_bills_by_due_date()
                        if (b.get("due_date") or "").startswith(next_m))

    weeks = []
    for wk in calmod.Calendar(firstweekday=6).monthdayscalendar(first.year, first.month):
        row = []
        for d in wk:
            if d == 0:
                row.append(None)
                continue
            iso = f"{ym}-{d:02d}"
            slips = days_map.get(iso, [])
            shown, more = slips[:3], slips[3:]
            row.append({
                "day": d, "is_today": iso == today_iso, "slips": shown,
                "more_count": len(more),
                "more_sum": sum(s["amount"] for s in more),
                "total": sum(s["amount"] for s in slips) if len(slips) > 1 else None,
            })
        weeks.append(row)

    stickies = []
    if late_positions:
        pos_txt = ", ".join(f"#{p}" for p in sorted(late_positions)[:4])
        stickies.append({
            "text": f"{len(late_positions)} past-due bill"
                    f"{'s are' if len(late_positions) != 1 else ' is'} sitting "
                    f"mid-stack ({pos_txt}).",
            "url": "/catchup?order=due", "link": "pull them to the top?"})
    if ghosts:
        names = ghosts[0].get("vendor") or "a bill"
        stickies.append({
            "text": f"{names} usually shows up by now — nothing scanned yet. "
                    f"{len(ghosts)} like this." if len(ghosts) > 1 else
                    f"{names} usually shows up by now — nothing scanned yet.",
            "url": "/expected", "link": "see the radar"})

    return render_template("calendar.html",
                           month_label=first.strftime("%B %Y"), ym=ym,
                           prev_m=prev_m, next_m=next_m, weeks=weeks,
                           tallies=tallies, ghosts=ghosts, ghost_sum=ghost_sum,
                           bill_count=len(bills) + len(ghosts),
                           month_sum=tallies["paid_sum"] + tallies["stack_sum"],
                           next_in_stack=next_in_stack, stickies=stickies)


def board():
    """Corkboard — everything on the dashboard's needs-attention list, pinned
    until someone takes it down. Take-down files nothing and deletes nothing;
    it just records handled-by-whom-when."""
    items = get_board_items()
    today = date.today()
    for it in items:
        it["label"] = _M._doc_label(it)
        base = ((it.get("flagged_at") or it.get("created_at") or "")[:10]
                or today.isoformat())
        try:
            it["age_days"] = max((today - date.fromisoformat(base)).days, 0)
        except ValueError:
            it["age_days"] = 0
        it["tint"] = ("stale" if it["age_days"] >= 8
                      else "aging" if it["age_days"] >= 3 else "")
        manual = bool(it.get("important")) and not (it.get("important_reason") or "").strip()
        it["pin"] = "staff" if manual else "ai"
        it["flagline"] = "flagged by staff" if manual else "AI flagged"
        it["why"] = ((it.get("notice_summary") or "").strip()
                     or (it.get("important_reason") or "").strip()
                     or (it.get("doc_type") or "document").lower() + " — needs a look")
        it["is_clip"] = bool((it.get("notice_summary") or "").strip())
        it["clip_text"] = ""
        if it["is_clip"] and it.get("doc_type") == "Bill":
            amt = f"${(it.get('amount') or 0):,.2f}"
            paid = (f"paid {_fmt_day(it.get('paid_date'))}"
                    + (f" · ck {it['check_number']}" if it.get("check_number") else "")
                    ) if it.get("paid_date") else "unpaid"
            it["clip_text"] = f"arrived inside a bill: {it.get('vendor') or ''} {amt} — {paid}"
        it["received_label"] = _fmt_day(it.get("date_received"))
    pinned, tray_items = items[:12], items[12:]
    week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    return render_template("board.html", pinned=pinned, tray=tray_items,
                           pinned_count=len(items),
                           taken_down_week=count_taken_down_since(week_ago),
                           oldest_days=max((i["age_days"] for i in items), default=0),
                           tray_oldest=max((i["age_days"] for i in tray_items), default=0))


def _board_ack(entry_id: int):
    entry = get_entry(entry_id)
    if not entry:
        return
    update_entry(entry_id, {
        "acknowledged_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "acknowledged_by": session.get("user", "")})
    log_activity(entry_id, "Taken down from board", session.get("user", ""))


def board_takedown(entry_id):
    _board_ack(entry_id)
    return redirect("/board")


def board_sweep():
    """Take down every item marked done — sign the cover sheet, not each page."""
    for sid in request.form.getlist("ids"):
        try:
            _board_ack(int(sid))
        except (TypeError, ValueError):
            pass
    return redirect("/board")


def tray():
    """The inbox tray, edge-on — review-queue depth as literal paper. The
    count is the dashboard's 'waiting for review' number; the stack is the
    glance. Rendering upserts today's depth so the history shelf stays live."""
    items = _M._queue_items()
    today = date.today()
    today_iso = today.isoformat()
    depth = len(items)
    urgent = sum(1 for _, d in items if _M._scan_is_overdue(d))

    groups, oldest = {}, None
    for _, d in items:
        rd = ((d.get("received_date") or d.get("scanned_date") or today_iso)[:10])
        bn = d.get("batch_number") or "no batch"
        g = groups.setdefault(bn, {"batch_number": bn, "received": rd, "count": 0})
        g["count"] += 1
        g["received"] = min(g["received"], rd)
        oldest = rd if oldest is None or rd < oldest else oldest
    batch_groups = sorted(groups.values(), key=lambda g: g["received"])
    for g in batch_groups:
        try:
            age = (today - date.fromisoformat(g["received"])).days
        except ValueError:
            age = 0
        g["tint"] = "stale" if age >= 8 else "aging" if age >= 3 else ""
        g["received_label"] = _fmt_day(g["received"])

    record_queue_depth(today_iso, depth, urgent)
    history = get_queue_depth_history(14)
    for h in history:
        h["label"] = "today" if h["day"] == today_iso else _fmt_day(h["day"])
        h["flood"] = (h.get("depth") or 0) > _TRAY_TRIAGE_LINE
    depths = sorted(h.get("depth") or 0 for h in history)
    normal_line = depths[len(depths) // 2] if len(depths) >= 5 else 12
    yesterday_iso = (today - timedelta(days=1)).isoformat()
    yesterday_depth = next((h.get("depth") for h in history
                            if h["day"] == yesterday_iso), None)

    counts = confirm_counts_for_day(today_iso)
    landed = counts["landed_and_cleared"] + sum(
        1 for _, d in items if (d.get("scanned_date") or "")[:10] == today_iso)

    verdict = ("quiet" if depth <= normal_line else
               "busy" if depth <= _TRAY_TRIAGE_LINE else
               "backed up" if depth <= _TRAY_TRIAGE_LINE * 2 else "flooded")

    return render_template("tray.html", depth=depth, urgent=urgent,
                           batch_groups=batch_groups, verdict=verdict,
                           oldest_label=_fmt_day(oldest) if oldest else "",
                           landed_today=landed, cleared_today=counts["cleared"],
                           yesterday_depth=yesterday_depth, history=history,
                           normal_line=normal_line, triage_line=_TRAY_TRIAGE_LINE)


def _ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        return f"{n}th"
    return f"{n}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th') }"


def desk():
    """The desk — the paper series' landing page. Every pile is a live queue:
    incoming mail under its batch cover sheet, the to-pay stack oldest-on-top,
    the clipboard of flagged correspondence, the filed tray. Click a pile to
    work it; sticky notes are system findings."""
    items = _M._queue_items()
    today_iso = date.today().isoformat()

    batches = {}
    for _, d in items:
        bn = d.get("batch_number") or ""
        if bn:
            b = batches.setdefault(bn, {"batch_number": bn, "count": 0,
                                        "received": (d.get("received_date") or "")[:10]})
            b["count"] += 1
            b["received"] = max(b["received"], (d.get("received_date") or "")[:10])
    top_batch = max(batches.values(), key=lambda b: b["received"]) if batches else None
    if top_batch:
        top_batch["received_label"] = _fmt_day(top_batch["received"])

    stack = _M._catchup_received_rows()
    top_bill = stack[0] if stack else None
    overdue_in_stack = sum(1 for b in stack
                           if (b.get("due_date") or "") and b["due_date"] < today_iso)

    board_items = get_board_items()
    for it in board_items[:2]:
        it["label"] = _M._doc_label(it)
    filed_total = get_dashboard_stats().get("total", 0)
    cleared_today = confirm_counts_for_day(today_iso)["cleared"]

    stickies = []
    try:
        certain, queue_dups, probable, near_queue = _M._triage_groups()
        rep = len(certain) + len(queue_dups) + len(near_queue)
        if rep:
            stickies.append({"text": f"{rep} group{'s' if rep != 1 else ''} of bills look "
                                     f"logged or queued twice —",
                             "url": "/review/triage", "link": "sort them out"})
    except Exception:
        pass
    if overdue_in_stack:
        stickies.append({"text": f"{overdue_in_stack} bill{'s' if overdue_in_stack != 1 else ''} "
                                 f"in the stack {'are' if overdue_in_stack != 1 else 'is'} already past due —",
                         "url": "/catchup?order=due", "link": "pull them forward"})
    try:
        waiting = sum(1 for s in _M._expected_statuses() if s["state"] == "waiting")
    except Exception:
        waiting = 0
    if waiting:
        stickies.append({"text": f"{waiting} expected bill{'s' if waiting != 1 else ''} "
                                 f"ha{'ve' if waiting != 1 else 's'}n't shown up this period —",
                         "url": "/expected", "link": "see the radar"})

    return render_template("desk.html",
                           pending_count=len(items), top_batch=top_batch,
                           top_bill=top_bill, stack_count=len(stack),
                           stack_total=sum(b.get("amount") or 0 for b in stack),
                           board_top=board_items[:2], board_count=len(board_items),
                           filed_total=filed_total, cleared_today=cleared_today,
                           stickies=stickies, today_label=_fmt_day(today_iso))


def cabinet():
    """The file cabinet — one drawer per entity, month tabs like hanging
    folders, every document as its typed label. The archive Karen has kept on
    paper for 13 years, same shape, findable in seconds."""
    cfg = _M.load_config()
    counts = entity_doc_counts()
    drawers = [{"code": e["code"], "name": e.get("name") or "",
                "count": counts.get(e["code"], 0)}
               for e in cfg.get("entities", [])]
    if counts.get("", 0):
        drawers.append({"code": "", "name": "Unsorted", "count": counts[""]})

    codes = [d["code"] for d in drawers]
    sel = request.args.get("entity")
    if sel is None or sel not in codes:
        sel = next((d["code"] for d in drawers if d["count"]), codes[0] if codes else "")
    sel_name = next((d["name"] for d in drawers if d["code"] == sel), "")

    months = months_for_entity(sel)
    ym = request.args.get("month") or (months[0]["ym"] if months else date.today().strftime("%Y-%m"))
    month_names = ["", "JANUARY", "FEBRUARY", "MARCH", "APRIL", "MAY", "JUNE", "JULY",
                   "AUGUST", "SEPTEMBER", "OCTOBER", "NOVEMBER", "DECEMBER"]
    for m in months:
        mm = int(m["ym"][5:7])
        m["label"] = f"{mm:02d}. {month_names[mm][:3]}"
    try:
        ym_label = f"{int(ym[5:7]):02d}. {month_names[int(ym[5:7])]}"
    except (ValueError, IndexError):
        ym_label = ym

    docs = docs_for_entity_month(sel, ym)
    for d in docs:
        d["label"] = _M._doc_label(d)
        paid = d.get("status") in ("Paid", "Filed") or bool(d.get("paid_date"))
        if paid:
            d["chip"], d["chip_kind"] = "paid", "paid"
        elif d.get("doc_type") == "Bill" and d.get("due_date"):
            d["chip"], d["chip_kind"] = f"due {d['due_date'][5:]}", "due"
        else:
            d["chip"], d["chip_kind"] = (d.get("doc_type") or "").lower(), "type"

    return render_template("cabinet.html",
                           drawers=drawers, sel=sel, sel_name=sel_name,
                           months=months[:8], ym=ym, ym_label=ym_label,
                           year=ym[:4], docs=docs,
                           queue_depth=len(_M._queue_items()))


def rolodex():
    """The rolodex — every vendor ever logged, as a card. Account numbers,
    where it routes, what it usually costs and when it's usually due (read off
    the actual payment history), and the last few payments."""
    directory = get_vendor_directory()

    def first_letter(v):
        c = (v or "#").strip()[:1].upper()
        return c if c.isalpha() else "#"

    letters = sorted({first_letter(r["vendor"]) for r in directory})
    letter = (request.args.get("letter") or "").upper()
    vendor = request.args.get("vendor") or ""
    if vendor and not letter:
        letter = first_letter(vendor)
    if letter not in letters:
        letter = letters[0] if letters else "A"
    in_letter = [r for r in directory if first_letter(r["vendor"]) == letter]
    if not vendor or vendor.lower() not in {r["vendor"].lower() for r in in_letter}:
        vendor = in_letter[0]["vendor"] if in_letter else ""

    card = None
    if vendor:
        bills = get_bills_for_vendor(vendor)
        paid = sorted((b for b in bills if b.get("paid_date")),
                      key=lambda b: b["paid_date"], reverse=True)
        amounts = [b["amount"] for b in paid[:6] if b.get("amount")]
        due_days = sorted(int(b["due_date"][8:10]) for b in bills
                          if (b.get("due_date") or "")[8:10].isdigit())
        hint = get_vendor_hint_exact(vendor)
        newest = bills[0] if bills else {}
        radar = next((e for e in get_expected_bills(active_only=True)
                      if (e.get("vendor") or "").strip().lower() == vendor.strip().lower()),
                     None)
        card = {
            "vendor": vendor,
            "kind": " · ".join(x for x in [(newest.get("bill_type_code") or newest.get("doc_type") or "").lower(),
                                           (radar or {}).get("cadence") or ""] if x),
            "accounts": sorted({(b.get("account_number") or "").strip()
                                for b in bills if (b.get("account_number") or "").strip()}),
            "route_entity": (hint or {}).get("entity_code") or newest.get("entity_code") or "",
            "route_prop": (hint or {}).get("property_codes") or newest.get("property_code") or "",
            "typical": (f"${min(amounts):,.0f} – ${max(amounts):,.0f}" if len(amounts) > 1
                        else (f"${amounts[0]:,.2f}" if amounts else "")),
            "usual_day": _ordinal(due_days[len(due_days) // 2]) if due_days else "",
            "on_radar": bool(radar),
            "payments": [{"date": b["paid_date"],
                          "method": (f"ck {b['check_number']}" if b.get("check_number")
                                     else (b.get("payment_method") or "paid").lower()),
                          "amount": b.get("amount") or 0} for b in paid[:5]],
            "total_bills": len(bills),
            "unpaid": sum(1 for b in bills if b.get("status") not in ("Paid", "Filed")),
        }

    is_admin = _M._current_user_is_admin()
    return render_template("rolodex.html",
                           letters=letters, letter=letter, in_letter=in_letter,
                           vendor=vendor, card=card, total_vendors=len(directory),
                           vendor_clusters=find_vendor_clusters() if is_admin else [],
                           is_admin=is_admin,
                           msg=request.args.get("msg", ""))


def merge_vendor():
    """One-time cleanup (Karen, 2026-07-18): fold one or more vendor spelling
    variants into the chosen canonical name in one click (a whole cluster from
    the rolodex's 'possible duplicate vendors' panel). Recomputed server-side
    isn't needed here — rename_vendor matches by exact case-insensitive
    string, so a stale page can't merge the wrong thing (worst case: no rows
    match, nothing happens)."""
    new = (request.form.get("new_vendor") or "").strip()
    olds = [v.strip() for v in request.form.getlist("old_vendor") if v.strip()]
    if not new or not olds:
        return redirect(url_for("rolodex", msg="Pick a vendor group to merge."))
    changed, merged = 0, []
    for old in olds:
        if old.lower() == new.lower():
            continue   # the chosen keeper is posted alongside the others — skip it
        changed += rename_vendor(old, new)
        merged.append(old)
    if not merged:
        return redirect(url_for("rolodex", msg="Nothing to merge — pick two different vendor names."))
    return redirect(url_for("rolodex", vendor=new,
                            msg=f'Merged {len(merged)} spelling{"s" if len(merged) != 1 else ""} '
                                f'into "{new}" ({changed} record{"s" if changed != 1 else ""}).'))


def _prior_payment_match(vendor, amount, account_number=""):
    """The in-tray's reconciliation stamp: the most recent PAID bill from this
    vendor whose account number matches exactly or whose amount is within 15%.
    None when there's nothing comparable — the stamp simply doesn't appear."""
    if not vendor:
        return None
    try:
        prior = sorted((b for b in get_bills_for_vendor(vendor) if b.get("paid_date")),
                       key=lambda b: b["paid_date"], reverse=True)
    except Exception:
        return None
    acct = (account_number or "").strip().lower()
    for b in prior[:12]:
        pa = b.get("amount") or 0
        acct_match = bool(acct) and acct == (b.get("account_number") or "").strip().lower()
        amt_match = bool(amount) and bool(pa) and abs(pa - amount) <= 0.15 * max(pa, amount)
        if acct_match or amt_match:
            return {"paid_date": b["paid_date"], "amount": pa,
                    "method": (f"ck {b['check_number']}" if b.get("check_number")
                               else (b.get("payment_method") or "paid").lower())}
    return None


def _scan_file_missing(data: dict) -> bool:
    """True when a queued sheet's recorded scan file is not on disk — the row
    outlived its file (moved by the junk filter, or gone). Nothing may be filed
    from such a row."""
    p = data.get("pdf_path") or ""
    return not (p and os.path.exists(p))


def intray():
    """The in-tray — the review queue one sheet at a time, oldest received
    first. The stamp submits the AI's reading through the SAME confirm route
    the review form uses (same duplicate check, same filing); corrections go
    to the full review form. This page adds no new write path."""
    items = _M._queue_items()
    # A sheet whose file went missing is always an old one, so plain oldest-first
    # puts every error notice ahead of the real work. They keep their place in the
    # queue and stay reachable, but they sink below the sheets someone can act on.
    order = sorted(items, key=lambda kv: (_scan_file_missing(kv[1]),
                                          (kv[1].get("received_date") or "9999")[:10], kv[0]))
    ids = [sid for sid, _ in order]
    missing_count = sum(1 for _, d in items if _scan_file_missing(d))
    today_iso = date.today().isoformat()
    cleared_today = confirm_counts_for_day(today_iso)["cleared"]

    sid = request.args.get("scan", type=int)
    if sid not in ids:
        sid = ids[0] if ids else None
    if sid is None:
        return render_template("intray.html", empty=True, scan_id=None, data=None,
                               props=[], pos=0, total=0, remaining=0,
                               cleared_today=cleared_today, match=None,
                               missing_count=missing_count,
                               overdue=False, can_stamp=False, today=today_iso)

    data = _M.get_scan(sid)
    if data is None:
        return redirect("/intray")
    pos = ids.index(sid)
    props = [p.strip() for p in (data.get("property_code") or "").replace(";", ",").split(",")
             if p.strip()]
    match = _prior_payment_match(data.get("vendor"), data.get("amount"),
                                 data.get("account_number"))
    # The stamp only offers itself when the AI actually read enough to file by —
    # and never for a sheet whose scan file has gone missing from disk. Confirming
    # one would log a record whose paper can't be found (_finalize_entry skips the
    # move silently); set-aside is the recovery path for those.
    pdf_path = data.get("pdf_path") or ""
    file_missing = _scan_file_missing(data)
    can_stamp = (not file_missing
                 and bool((data.get("vendor") or "").strip())
                 and bool((data.get("entity_code") or "").strip()))

    return render_template("intray.html", empty=False, scan_id=sid, data=data,
                           file_missing=file_missing, missing_path=pdf_path,
                           props=props, pos=pos + 1, total=len(ids),
                           remaining=len(ids) - pos - 1,
                           next_id=(ids[pos + 1] if pos + 1 < len(ids) else None),
                           prev_id=(ids[pos - 1] if pos > 0 else None),
                           cleared_today=cleared_today, match=match,
                           missing_count=missing_count,
                           overdue=_M._scan_is_overdue(data), can_stamp=can_stamp,
                           today=today_iso)


def intray_clear_missing():
    """Clear every queue row whose scan file is gone. The missing set is
    recomputed from the CURRENT queue, never taken from the submitted page, so a
    stale tab can't clear a sheet that has since been repaired. There is no file
    to move aside — that is the whole point — so only the row goes; anything the
    junk filter moved is still on the Filtered page, and _repair_queue_paths gets
    a shot at the rest on every boot before anyone reaches for this."""
    cleared = 0
    for sid, d in _M._queue_items():
        if not _scan_file_missing(d):
            continue
        data = _M._claim_scan(sid)
        if not data:
            continue
        try:
            print(f"[In-tray] Cleared queue sheet {sid} "
                  f"({data.get('vendor') or 'no vendor read'}) — its recorded file is not "
                  f"on disk: {data.get('pdf_path') or 'no path recorded'}")
            _M._queue_remove(sid)
            cleared += 1
        finally:
            _M._release_scan(sid)
    if cleared:
        log_activity(None, "Queue cleanup", user=session.get("user", ""),
                     detail=f"Cleared {cleared} in-tray sheet(s) whose scan file was "
                            f"missing from disk. No documents were deleted.")
    return redirect("/intray")
