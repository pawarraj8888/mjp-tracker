"""Email alerts for qualifying MJP projects, with the actual official PDFs
attached.

Rules that the brief makes non-negotiable, enforced here:

* Threshold is on the **project's** documented value (revised cost if present,
  else approved cost), strictly above ₹1,00,00,000 -- never a multi-project GR
  total. An unknown-value project is kept visible for review but never counted
  as qualifying.
* The original official PDFs are **attached** (from the durable store), not
  linked. A generated summary, if attached, is named so it can never be mistaken
  for the original. An alert is recorded "sent" only when its attachments were
  actually attached; otherwise it stays pending and retries.
* Delivery is tracked per (event, recipient) so repeated checks never re-send,
  and the initial import is a single clearly-labelled backfill digest, not a
  flood of individual "new" alerts.

Live delivery needs SMTP secrets (``SMTP_USER`` / ``SMTP_PASS``); without them
this prepares each alert and records it **pending** -- it never claims a send it
did not make.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal
from pathlib import Path

import money
import timez

from ..store import Store
from . import store as mstore

ROOT = Path(__file__).resolve().parent.parent.parent
ALERTS_LOG = ROOT / "mjp_store" / "alerts.json"
DOCS_DIR = ROOT / "mjp_store" / "documents"

THRESHOLD = Decimal(10_000_000)            # strictly above ₹1 crore
ATTACH_LIMIT_BYTES = 20 * 1024 * 1024      # keep each email well under Gmail 25MB
DEFAULT_RECIPIENTS = ("pawarraj8888@gmail.com", "sppawar.sachin7777@gmail.com")


def recipients() -> list[str]:
    import os
    raw = os.environ.get("MJP_ALERT_TO", "")
    if raw.strip():
        return [r.strip() for r in re.split(r"[,;]", raw) if r.strip()]
    return list(DEFAULT_RECIPIENTS)


def project_value(project: dict) -> Decimal | None:
    """The project's own documented value: revised cost if present, else the
    approved cost. Returns ``None`` (unknown) when neither is known."""
    for key in ("revised_cost", "approved_cost"):
        d = money.str_to_dec((project.get(key) or {}).get("inr"))
        if d is not None:
            return d
    return None


def qualifies(project: dict) -> bool:
    v = project_value(project)
    return v is not None and v > THRESHOLD


# -- delivery log ------------------------------------------------------------

def load_log() -> dict:
    if ALERTS_LOG.exists():
        try:
            return json.loads(ALERTS_LOG.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            pass
    return {"recipients": [], "delivered": [], "pending": [], "updated_at": ""}


def save_log(log: dict) -> None:
    ALERTS_LOG.parent.mkdir(parents=True, exist_ok=True)
    log["updated_at"] = timez.iso_ist()
    ALERTS_LOG.write_text(json.dumps(log, ensure_ascii=False, indent=1,
                                     sort_keys=True) + "\n", encoding="utf-8")


def _delivered_parts(log: dict, event_key: str, recipient: str) -> set:
    return {d["part_no"] for d in log.get("delivered", [])
            if d["event_key"] == event_key and d["recipient"] == recipient}


def is_delivered(log: dict, event_key: str, recipient: str) -> bool:
    parts = [d for d in log.get("delivered", [])
             if d["event_key"] == event_key and d["recipient"] == recipient]
    return bool(parts) and len(parts) >= parts[0].get("parts_total", 1)


# -- alert events ------------------------------------------------------------

DIGEST_KEY = "backfill_digest"       # stable across runs and set size


def _approved_sig(project: dict) -> str:
    # The approval alert is keyed on the APPROVED cost only, so a later revision
    # (which changes project_value) does not mint a second "approval" alert --
    # the revision has its own key.
    d = money.str_to_dec((project.get("approved_cost") or {}).get("inr"))
    return money.dec_to_str(d) or "unknown"


def project_event_keys(p: dict) -> list[str]:
    """The stable alert keys a qualifying project currently has."""
    keys = ["approval:%s:%s" % (p["id"], _approved_sig(p))]
    if money.is_known((p.get("revised_cost") or {}).get("inr")):
        keys.append("revision:%s:%s" % (p["id"], p["revised_cost"]["inr"]))
    for l in p.get("links", []):
        if l["status"] == "linked":
            keys.append("tenderlink:%s:%s" % (p["id"], l["tender_id"]))
    return keys


def due_events(projects: list[dict]) -> list[dict]:
    """Alert-worthy changes for qualifying projects: a qualifying approval, a
    material revision, and each confirmed tender link. Each carries a stable
    event_key for dedup."""
    events = []
    for p in projects:
        if not qualifies(p):
            continue
        pid = p["id"]
        events.append({
            "event_key": "approval:%s:%s" % (pid, _approved_sig(p)),
            "kind": "qualifying_approval", "project": p,
            "what_changed": "A qualifying MJP approval was recorded."})
        if money.is_known((p.get("revised_cost") or {}).get("inr")):
            events.append({
                "event_key": "revision:%s:%s"
                % (pid, (p["revised_cost"]["inr"])),
                "kind": "revision", "project": p,
                "what_changed": "The sanctioned cost was revised."})
        for l in p.get("links", []):
            if l["status"] == "linked":
                events.append({
                    "event_key": "tenderlink:%s:%s" % (pid, l["tender_id"]),
                    "kind": "tender_linked", "project": p,
                    "tender": l,
                    "what_changed": "A tender was linked to this project."})
    return events


# -- message construction ----------------------------------------------------

def _fmt_amt(a: dict | None) -> str:
    if not a or not a.get("inr"):
        return "Unknown"
    return "₹%s (%s)" % (a.get("plain") or a["inr"], a.get("display") or "")


def _tender_facts(tender: dict, details: dict) -> list[str]:
    out = []
    d = details.get(tender["tender_id"], {}) if details else {}
    for label, key in (("Bid submission ends", "Bid Submission End Date"),
                       ("Bid opening", "Bid Opening Date"),
                       ("EMD", "EMD Amount in ₹"),
                       ("Tender fee", "Tender Fee in ₹"),
                       ("Estimated value", "Tender Value in ₹")):
        if d.get(key):
            out.append("%s: %s" % (label, d[key]))
    return out


def build_body(event: dict, details: dict, backfill: bool) -> str:
    p = event["project"]
    lines = []
    if backfill:
        lines.append("[BACKFILL / historical import -- not a newly published "
                     "approval]")
    lines.append("Project: %s" % (p.get("title_en") or p.get("title_original")))
    loc = ", ".join(x for x in (p.get("municipality"), p.get("district")) if x)
    lines.append("Location: %s" % (loc or "not stated"))
    lines.append("MJP office: %s" % (p.get("mjp_office") or "not stated"))
    lines.append("MJP role: %s (confidence %.0f%%)"
                 % (p.get("mjp_role"), 100 * (p.get("mjp_role_confidence") or 0)))
    lines.append("What changed: %s" % event["what_changed"])
    lines.append("")
    lines.append("Approved cost: %s" % _fmt_amt(p.get("approved_cost")))
    if money.is_known((p.get("revised_cost") or {}).get("inr")):
        lines.append("Revised cost: %s" % _fmt_amt(p.get("revised_cost")))
    if money.is_known((p.get("funds_released") or {}).get("inr")):
        lines.append("Funds released: %s" % _fmt_amt(p.get("funds_released")))
    lines.append("Meaning: %s. Approval is documented; it does not by itself "
                 "mean funds have been disbursed." % p.get("status_label"))
    lines.append("")
    lines.append("Scope:")
    for w in (p.get("components") or [])[:6]:
        lines.append("  - %s" % w)
    lines.append("")
    lines.append("Official documents (attached as originals):")
    for d in p.get("documents", []):
        lines.append("  - %s | %s | GR %s | issued %s | %s"
                     % (d["doc_code"], d["doc_type"], d.get("gr_number") or "-",
                        d.get("issue_date") or "-", d.get("url") or ""))
    lines.append("")
    lines.append("Tender status: %s" % p.get("tender_match_status"))
    if event.get("tender"):
        facts = _tender_facts(event["tender"], details)
        if facts:
            lines.append("Tender details:")
            lines.extend("  - " + f for f in facts)
    for u in (p.get("uncertainties") or []):
        lines.append("Note: %s" % u)
    lines.append("")
    lines.append("Generated %s IST by the MJP tracker. Attachments named "
                 "OFFICIAL-* are the original government PDFs." % timez.fmt_ist(
                     timez.now_ist()))
    return "\n".join(lines)


def _attachments_for(project: dict) -> tuple[list[dict], list[str]]:
    """Return (attachments, missing_codes). Each attachment is the original
    stored PDF; a missing original is reported so the alert stays pending."""
    attachments, missing = [], []
    for d in project.get("documents", []):
        code = d.get("doc_code", "")
        if not re.match(r"^\d{6,25}$", code):   # defense-in-depth on the path
            missing.append(code or "?")
            continue
        path = DOCS_DIR / ("%s.pdf" % code)
        if path.exists():
            attachments.append({
                "filename": "OFFICIAL-GR-%s.pdf" % d["doc_code"],
                "path": str(path), "size": path.stat().st_size})
        else:
            missing.append(d["doc_code"])
    return attachments, missing


def build_digest_body(qualifying: list[dict]) -> str:
    """One clearly-labelled backfill digest covering the historical import,
    instead of one 'new' email per project."""
    lines = [
        "[BACKFILL / historical import]",
        "This is the initial import of MJP projects already on record whose "
        "documented value exceeds ₹1 crore. These are NOT newly published "
        "approvals; future genuinely-new approvals will arrive as individual "
        "alerts.",
        "",
        "%d qualifying project(s):" % len(qualifying), ""]
    for p in qualifying:
        loc = ", ".join(x for x in (p.get("municipality"), p.get("district")) if x)
        lines.append("• %s (%s) -- %s" % (
            p.get("municipality") or p.get("title_en", "")[:50], loc or "n/a",
            _fmt_amt(p.get("approved_cost"))))
        lines.append("   MJP role: %s; status: %s; tender: %s"
                     % (p.get("mjp_role"), p.get("status_label"),
                        p.get("tender_match_status")))
        for d in p.get("documents", []):
            lines.append("   Official doc %s (%s), issued %s -- attached."
                         % (d["doc_code"], d["doc_type"], d.get("issue_date")))
        lines.append("")
    lines.append("Attachments named OFFICIAL-* are the original government "
                 "PDFs, stored unchanged.")
    return "\n".join(lines)


def _attachments_for_many(projects: list[dict]) -> tuple[list[dict], list[str]]:
    attachments, missing = [], []
    for p in projects:
        a, m = _attachments_for(p)
        attachments.extend(a)
        missing.extend(m)
    return attachments, missing


def _split_parts(attachments: list[dict]) -> list[list[dict]]:
    parts, cur, size = [], [], 0
    for a in attachments:
        if cur and size + a["size"] > ATTACH_LIMIT_BYTES:
            parts.append(cur)
            cur, size = [], 0
        cur.append(a)
        size += a["size"]
    if cur or not parts:
        parts.append(cur)
    return parts


# -- SMTP send (reuses the tracker's configuration) --------------------------

def smtp_configured() -> bool:
    import os
    return bool(os.environ.get("SMTP_USER") and os.environ.get("SMTP_PASS"))


def _send_message(subject: str, body: str, to: str,
                  attachments: list[dict]) -> tuple[bool, str]:
    if not smtp_configured():
        return False, "SMTP not configured (SMTP_USER/SMTP_PASS unset)"
    import os
    import smtplib
    from email.message import EmailMessage
    # Strip any CR/LF from the subject so an odd project title can never abort
    # the run (EmailMessage would raise) -- it also forecloses header injection.
    subject = re.sub(r"[\r\n]+", " ", subject)
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = os.environ["SMTP_USER"]
    msg["To"] = to
    msg.set_content(body)
    for a in attachments:
        try:
            with open(a["path"], "rb") as fh:
                msg.add_attachment(fh.read(), maintype="application",
                                   subtype="pdf", filename=a["filename"])
        except OSError as exc:
            return False, "attachment %s unreadable: %s" % (a["filename"], exc)
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "465"))
    try:
        with smtplib.SMTP_SSL(host, port, timeout=60) as smtp:
            smtp.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
            smtp.send_message(msg)
        return True, ""
    except Exception as exc:                # pragma: no cover - network path
        return False, str(exc)


# -- run ---------------------------------------------------------------------

def _deliver(store: Store, log: dict, event_key: str, subject: str, body: str,
             attachments: list[dict], missing: list[str], recipient: str,
             backfill: bool, send, dry_run: bool, now: str,
             also_cover: list[str] | None = None) -> str:
    """Deliver one alert (all its numbered parts) to one recipient. Returns
    'sent' | 'pending' | 'skipped'. On success, any ``also_cover`` keys are
    recorded delivered too (used so a backfill digest suppresses the individual
    per-project alerts it already covered)."""
    if is_delivered(log, event_key, recipient):
        return "skipped"
    parts = _split_parts(attachments)
    done = _delivered_parts(log, event_key, recipient)
    fake_event = {"event_key": event_key, "kind": "alert"}
    for idx, part in enumerate(parts, start=1):
        if idx in done:
            continue
        psubj = subject + (" [Part %d/%d]" % (idx, len(parts))
                           if len(parts) > 1 else "")
        if missing:
            _record_pending(log, fake_event, recipient,
                            "originals missing: %s" % ",".join(missing), now)
            mstore.record_alert(store, event_key, recipient, "pending", False,
                                now, part_no=idx, parts_total=len(parts),
                                is_backfill=backfill,
                                last_error="originals missing")
            return "pending"
        if dry_run:
            _record_pending(log, fake_event, recipient, "dry_run", now)
            return "pending"
        ok, err = send(psubj, body, recipient, part)
        if ok:
            log.setdefault("delivered", []).append({
                "event_key": event_key, "recipient": recipient, "part_no": idx,
                "parts_total": len(parts), "is_backfill": backfill,
                "sent_at": now})
            mstore.record_alert(store, event_key, recipient, "sent", True, now,
                                part_no=idx, parts_total=len(parts),
                                is_backfill=backfill, sent_at=now)
        else:
            _record_pending(log, fake_event, recipient, err, now)
            mstore.record_alert(store, event_key, recipient, "pending",
                                bool(attachments), now, part_no=idx,
                                parts_total=len(parts), is_backfill=backfill,
                                last_error=err)
            return "pending"
    _clear_pending(log, event_key, recipient)
    # Mark covered per-project keys delivered so they never re-fire individually.
    for k in (also_cover or []):
        if not is_delivered(log, k, recipient):
            log.setdefault("delivered", []).append({
                "event_key": k, "recipient": recipient, "part_no": 1,
                "parts_total": 1, "is_backfill": backfill, "sent_at": now,
                "covered_by": event_key})
    return "sent"


def run(store: Store, projects: list[dict], details: dict | None = None,
        dry_run: bool = False, send_fn=None) -> dict:
    """Send (or prepare) all due alerts. The first run with an empty log is a
    single labelled backfill digest (not a flood of per-project 'new' alerts);
    thereafter each change alerts individually. ``send_fn`` is injectable for
    tests."""
    details = details or {}
    log = load_log()
    log["recipients"] = recipients()
    # Backfill mode persists (via a durable flag) until the digest has actually
    # reached every recipient. A failed/pending first send (e.g. SMTP not yet
    # configured) therefore keeps retrying the ONE digest -- it never falls
    # through to a flood of individual "new" alerts.
    backfill = not log.get("backfill_done")
    send = send_fn or _send_message
    sent = pending = skipped = 0
    now = timez.iso_ist()

    if backfill:
        qualifying = [p for p in projects if qualifies(p)]
        if qualifying:
            attachments, missing = _attachments_for_many(qualifying)
            covered = [k for p in qualifying for k in project_event_keys(p)]
            subject = ("[MJP BACKFILL] Historical import: %d qualifying MJP "
                       "project(s)" % len(qualifying))
            body = build_digest_body(qualifying)
            for rcpt in recipients():
                outcome = _deliver(store, log, DIGEST_KEY, subject, body,
                                   attachments, missing, rcpt, True, send,
                                   dry_run, now, also_cover=covered)
                sent += outcome == "sent"
                pending += outcome == "pending"
                skipped += outcome == "skipped"
        # Complete only when the digest reached everyone (or nothing qualified).
        log["backfill_done"] = (not qualifying) or all(
            is_delivered(log, DIGEST_KEY, r) for r in recipients())
        save_log(log)
        return {"sent": sent, "pending": pending, "skipped_already_sent": skipped,
                "backfill": True, "backfill_done": log["backfill_done"],
                "qualifying": len(qualifying),
                "smtp_configured": smtp_configured(), "recipients": recipients()}

    for event in due_events(projects):
        p = event["project"]
        attachments, missing = _attachments_for(p)
        subject = "[MJP ALERT] %s: %s (%s)" % (
            event["kind"].replace("_", " ").title(),
            p.get("municipality") or p.get("title_en", "")[:40],
            _fmt_amt(p.get("approved_cost")))
        body = build_body(event, details, False)
        for rcpt in recipients():
            outcome = _deliver(store, log, event["event_key"], subject, body,
                               attachments, missing, rcpt, False, send, dry_run,
                               now)
            sent += outcome == "sent"
            pending += outcome == "pending"
            skipped += outcome == "skipped"
    save_log(log)
    return {"sent": sent, "pending": pending, "skipped_already_sent": skipped,
            "backfill": False, "smtp_configured": smtp_configured(),
            "recipients": recipients()}


def _record_pending(log: dict, event: dict, recipient: str, reason: str,
                    now: str) -> None:
    pend = log.setdefault("pending", [])
    for e in pend:
        if e["event_key"] == event["event_key"] and e["recipient"] == recipient:
            e["reason"] = reason
            e["attempts"] = e.get("attempts", 0) + 1
            e["updated_at"] = now
            return
    pend.append({"event_key": event["event_key"], "recipient": recipient,
                 "kind": event["kind"], "reason": reason, "attempts": 1,
                 "updated_at": now})


def _clear_pending(log: dict, event_key: str, recipient: str) -> None:
    log["pending"] = [e for e in log.get("pending", [])
                      if not (e["event_key"] == event_key
                              and e["recipient"] == recipient)]
