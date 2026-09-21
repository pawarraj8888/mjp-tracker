"""MJP alerts: >₹1cr threshold, real attachments, dedup, backfill digest,
pending-without-SMTP, and the funding-instalment-vs-total rule."""

from pathlib import Path

import pytest

from pipeline.store import Store
from pipeline.mjp import alerts


def _project(pid="p1", approved="249925000", revised=None, released=None,
             docs=None):
    return {
        "id": pid, "title_en": "Water supply for Testville",
        "municipality": "Testville", "district": "Amravati",
        "mjp_office": "MJP Div", "mjp_role": "implementing_agency",
        "mjp_role_confidence": 0.9, "status_label": "Funding sanctioned",
        "tender_match_status": "No matching tender found as of now",
        "components": ["Intake well"], "uncertainties": [],
        "approved_cost": {"inr": approved, "plain": "24,99,25,000",
                          "display": "24.99 Cr"} if approved else
                         {"inr": None, "plain": None, "display": None},
        "revised_cost": {"inr": revised, "plain": revised, "display": revised}
                        if revised else {"inr": None},
        "funds_released": {"inr": released} if released else {"inr": None},
        "tender_estimate": {"inr": None}, "links": [],
        "documents": docs or [{"doc_code": "202606251057023925",
                               "doc_type": "funding_sanction",
                               "issue_date": "2026-06-25", "gr_number": "GR1",
                               "url": "https://x/y.pdf"}],
    }


@pytest.fixture
def env(tmp_path, monkeypatch):
    docs = tmp_path / "documents"
    docs.mkdir()
    (docs / "202606251057023925.pdf").write_bytes(b"%PDF-1.4 original")
    monkeypatch.setattr(alerts, "DOCS_DIR", docs)
    monkeypatch.setattr(alerts, "ALERTS_LOG", tmp_path / "alerts.json")
    monkeypatch.setenv("MJP_ALERT_TO", "a@example.com,b@example.com")
    monkeypatch.delenv("SMTP_USER", raising=False)
    monkeypatch.delenv("SMTP_PASS", raising=False)
    return tmp_path


def test_threshold_strictly_above_one_crore():
    assert alerts.qualifies(_project(approved="10000001")) is True
    assert alerts.qualifies(_project(approved="10000000")) is False  # exactly 1cr
    assert alerts.qualifies(_project(approved="9999999")) is False


def test_unknown_value_does_not_qualify():
    assert alerts.qualifies(_project(approved=None)) is False


def test_funding_instalment_does_not_disqualify_when_total_above():
    # Value is on the project's documented total (approved), not a small
    # instalment; a 24.99cr project qualifies.
    assert alerts.qualifies(_project(approved="249925000")) is True


def test_backfill_digest_one_per_recipient_with_attachments(env):
    s = Store(":memory:")
    captured = []

    def fake_send(subject, body, to, atts):
        captured.append((to, subject, [a["filename"] for a in atts]))
        return True, ""

    r = alerts.run(s, [_project()], {}, send_fn=fake_send)
    assert r["backfill"] is True
    assert len(captured) == 2                       # one digest per recipient
    for to, subject, atts in captured:
        assert "BACKFILL" in subject
        assert atts == ["OFFICIAL-GR-202606251057023925.pdf"]


def test_dedup_no_resend(env):
    s = Store(":memory:")
    sends = []
    send = lambda *a: (sends.append(a), (True, ""))[1]
    alerts.run(s, [_project()], {}, send_fn=send)
    n_first = len(sends)
    alerts.run(s, [_project()], {}, send_fn=send)      # second run
    assert len(sends) == n_first                       # nothing re-sent


def test_pending_when_smtp_unconfigured(env):
    s = Store(":memory:")
    # Use the real _send_message which returns not-configured -> pending.
    r = alerts.run(s, [_project()], {})
    assert r["smtp_configured"] is False
    assert r["sent"] == 0
    assert r["pending"] >= 1


def test_backfill_retries_digest_after_pending_not_flood(env):
    # First run cannot send (SMTP down) -> digest pending. Second run (SMTP up)
    # must send the ONE backfill digest, NOT a flood of individual alerts.
    s = Store(":memory:")
    projects = [_project(pid="p1"), _project(pid="p2", approved="150000000")]

    def failing(subject, body, to, atts):
        return False, "smtp down"

    r1 = alerts.run(s, projects, {}, send_fn=failing)
    assert r1["backfill"] is True and r1["backfill_done"] is False
    assert r1["sent"] == 0 and r1["pending"] >= 1

    subjects = []

    def ok(subject, body, to, atts):
        subjects.append(subject)
        return True, ""

    r2 = alerts.run(s, projects, {}, send_fn=ok)
    assert r2["backfill"] is True                 # still backfill mode
    assert r2["backfill_done"] is True
    # Exactly one digest per recipient, and every subject is the BACKFILL digest.
    assert len(subjects) == 2
    assert all("BACKFILL" in su for su in subjects)
    assert not any("[MJP ALERT]" in su for su in subjects)

    # Third run: nothing new -> no sends.
    more = []
    alerts.run(s, projects, {}, send_fn=lambda *a: (more.append(a), (True, ""))[1])
    assert more == []


def test_revision_does_not_remint_approval_key():
    p = _project(pid="p1", approved="249925000", revised="300000000")
    keys = alerts.project_event_keys(p)
    approval_keys = [k for k in keys if k.startswith("approval:")]
    # Keyed on approved cost, so a revision does not create a second approval.
    assert approval_keys == ["approval:p1:249925000"]
    assert any(k.startswith("revision:") for k in keys)


def test_original_pdf_survives_storage_and_attachment_unchanged(env):
    # The brief requires the original PDF to survive storage + email attachment
    # byte-for-byte. Capture what the send path would attach and compare hashes.
    import hashlib
    original = (env / "documents" / "202606251057023925.pdf").read_bytes()
    src_sha = hashlib.sha256(original).hexdigest()
    s = Store(":memory:")
    attached = {}

    def capture(subject, body, to, atts):
        for a in atts:
            with open(a["path"], "rb") as fh:
                attached[a["filename"]] = hashlib.sha256(fh.read()).hexdigest()
        return True, ""

    alerts.run(s, [_project()], {}, send_fn=capture)
    assert attached["OFFICIAL-GR-202606251057023925.pdf"] == src_sha


def test_missing_original_keeps_alert_pending(env, monkeypatch):
    # Remove the stored PDF: the alert must not be reported sent.
    (env / "documents" / "202606251057023925.pdf").unlink()
    s = Store(":memory:")
    sent_called = []
    r = alerts.run(s, [_project()], {},
                   send_fn=lambda *a: (sent_called.append(a), (True, ""))[1])
    assert not sent_called                # never attempted with missing original
    assert r["pending"] >= 1
