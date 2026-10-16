"""The whole loop: discover → research → draft → approve → send → follow-up → reply."""

from email import message_from_bytes
from email.message import EmailMessage
from email.policy import default as default_policy

from outreach_agent import mailer
from outreach_agent.db import iso, utcnow


def _sent_files(engine):
    return sorted(engine.outbox.glob("*.eml")) if engine.outbox.exists() else []


def _reply_to(engine, seq_id, body, subject="Re: companion app for the trailcam pro", sender="sarah@trailcam.example"):
    first_id = engine.db.scalar("SELECT message_id FROM messages WHERE sequence_id=? AND step=1", (seq_id,))
    msg = EmailMessage()
    msg["From"] = f"Sarah Lee <{sender}>"
    msg["To"] = "hello@kitelabs.example"
    msg["Subject"] = subject
    msg["Message-ID"] = "<reply-1@trailcam.example>"
    msg["In-Reply-To"] = first_id
    msg["References"] = first_id
    msg.set_content(body + "\n\nOn Tue, Sam Rivera wrote:\n> Hi Sarah, your setup guide…")
    return mailer.parse(101, message_from_bytes(bytes(msg), policy=default_policy))


def test_full_cycle(engine, run):
    # discovery keeps only the real US company: the platform URL and the German firm are dropped
    run(engine.top_up())
    companies = engine.db.all("SELECT domain, status FROM companies")
    assert [c["domain"] for c in companies] == ["trailcam.example"]
    assert companies[0]["status"] == "drafted"

    # the address came from their own site, matched to the founder; support@ was never considered
    contact = engine.db.one("SELECT * FROM contacts")
    assert contact["email"] == "sarah@trailcam.example" and contact["kind"] == "person"

    # one draft, posted for approval, sign-off stripped, nothing sent yet
    seq_id = engine.recorder.drafts[0]
    first = engine.db.one("SELECT * FROM messages WHERE sequence_id=? AND step=1", (seq_id,))
    assert not first["body"].rstrip().endswith("Sam")
    assert run(engine.send_one()) is False
    assert _sent_files(engine) == []

    # approve → the first email sends, with signature, postal address and unsubscribe line
    assert engine.approve(seq_id) == []
    assert run(engine.send_one()) is True
    sent = message_from_bytes(_sent_files(engine)[0].read_bytes(), policy=default_policy)
    text = sent.get_body(preferencelist=("plain",)).get_content()
    rich = sent.get_body(preferencelist=("html",)).get_content()
    assert "kite-mark.png" in rich and "1 Test Street" in rich and "Worth a quick chat?" in rich
    assert sent["To"] == "sarah@trailcam.example"
    assert "Sam Rivera" in text and "1 Test Street" in text and "unsubscribe" in text
    assert sent["List-Unsubscribe"]
    assert engine.db.scalar("SELECT status FROM companies") == "contacted"

    # follow-ups are scheduled 4 and 10 days later and don't send early
    due = [engine.db.scalar("SELECT due_at FROM messages WHERE sequence_id=? AND step=?", (seq_id, s)) for s in (2, 3)]
    assert all(d > iso() for d in due)
    assert run(engine.send_one()) is False

    # when follow-up 1 is due, it goes out threaded under the first email
    engine.db.execute("UPDATE messages SET due_at=? WHERE sequence_id=? AND step=2", (iso(), seq_id))
    assert run(engine.send_one()) is True
    followup = message_from_bytes(_sent_files(engine)[-1].read_bytes(), policy=default_policy)
    if followup["Subject"] == sent["Subject"]:
        followup = message_from_bytes(_sent_files(engine)[0].read_bytes(), policy=default_policy)
    assert followup["Subject"].startswith("Re: ")
    assert followup["In-Reply-To"] == sent["Message-ID"]

    # a human reply stops the sequence, cancels follow-up 2, and arrives with a suggested answer
    inc = _reply_to(engine, seq_id, "Sounds interesting, can we talk next week?")
    assert "wrote:" not in inc.body
    seq, row = engine._match_sequence(inc)
    assert seq == seq_id
    run(engine.handle_inbound(inc, seq, row))
    assert engine.db.scalar("SELECT status FROM sequences WHERE id=?", (seq_id,)) == "stopped"
    assert engine.db.scalar("SELECT status FROM messages WHERE sequence_id=? AND step=3", (seq_id,)) == "cancelled"
    (inbound_id, draft_id), = engine.recorder.replies
    assert engine.db.scalar("SELECT classification FROM inbound WHERE id=?", (inbound_id,)) == "interested"

    # approving the reply sends it in-thread, without the cold-email footer
    run(engine.send_reply(draft_id))
    reply = [message_from_bytes(f.read_bytes(), policy=default_policy) for f in _sent_files(engine)]
    reply = next(m for m in reply if m["To"] == "sarah@trailcam.example" and m["In-Reply-To"] == "<reply-1@trailcam.example>")
    assert "unsubscribe" not in reply.get_body(preferencelist=("plain",)).get_content()
    assert "kite-mark.png" in reply.get_body(preferencelist=("html",)).get_content()


def test_unsubscribe_suppresses_company(engine, run):
    run(engine.top_up())
    seq_id = engine.recorder.drafts[0]
    engine.approve(seq_id)
    run(engine.send_one())
    inc = _reply_to(engine, seq_id, "Please remove me from your list.", subject="Re: hi")
    run(engine.handle_inbound(inc, *engine._match_sequence(inc)))
    assert engine.db.is_suppressed("sarah@trailcam.example")
    assert engine.db.is_suppressed("anyone@trailcam.example")      # whole company
    assert engine.db.scalar("SELECT status FROM companies") == "dead"
    assert "unsubscribe" in engine.recorder.alerts[0]


def test_out_of_office_does_not_stop_sequence(engine, run):
    run(engine.top_up())
    seq_id = engine.recorder.drafts[0]
    engine.approve(seq_id)
    run(engine.send_one())
    inc = _reply_to(engine, seq_id, "I'm away until Monday.", subject="Automatic reply: companion app")
    assert inc.is_auto
    run(engine.handle_inbound(inc, *engine._match_sequence(inc)))
    assert engine.db.scalar("SELECT status FROM sequences WHERE id=?", (seq_id,)) == "active"


def test_bounce_suppresses_and_kill_switch_trips(engine, run):
    run(engine.top_up())
    seq_id = engine.recorder.drafts[0]
    engine.approve(seq_id)
    run(engine.send_one())
    first_id = engine.db.scalar("SELECT message_id FROM messages WHERE sequence_id=? AND step=1", (seq_id,))
    bounce = EmailMessage()
    bounce["From"] = "Mail Delivery Subsystem <mailer-daemon@googlemail.com>"
    bounce["Subject"] = "Delivery Status Notification (Failure)"
    bounce.set_content(f"Final-Recipient: rfc822; sarah@trailcam.example\nMessage-ID: {first_id}\n")
    inc = mailer.parse(7, message_from_bytes(bytes(bounce), policy=default_policy))
    assert inc.is_bounce and inc.bounced_rcpt == "sarah@trailcam.example"
    run(engine.handle_inbound(inc, *engine._match_sequence(inc)))
    assert engine.db.is_suppressed("sarah@trailcam.example")
    assert engine.db.scalar("SELECT status FROM messages WHERE sequence_id=? AND step=1", (seq_id,)) == "bounced"

    # 20 more sends with 2 bounces among the last 21 → over 5% → automatic pause
    for i in range(19):
        engine.db.insert("INSERT INTO messages(sequence_id, step, subject, body, status, sent_at) VALUES(?,?,?,?,?,?)",
                         (seq_id, 10 + i, "s", "b", "sent", iso()))
    engine.db.insert("INSERT INTO messages(sequence_id, step, subject, body, status, sent_at) VALUES(?,?,?,?,?,?)",
                     (seq_id, 99, "s", "b", "bounced", iso()))
    run(engine._check_bounce_rate())
    assert engine.paused
    assert "paused automatically" in engine.recorder.alerts[-1]


def test_without_postal_address_us_and_canada_are_skipped(engine, run):
    # drafted while an address was set, then the address is removed: the US draft can't be approved
    run(engine.top_up())
    seq_id = engine.recorder.drafts[0]
    engine.cfg.sender.postal_address = ""
    assert engine.cfg.target_countries() == ["GB"]
    assert "postal address" in engine.approve(seq_id)[0]

    # an already-approved US email is held back rather than sent without the legally required address
    engine.db.execute("UPDATE sequences SET status='active' WHERE id=?", (seq_id,))
    engine.db.execute("UPDATE messages SET status='approved', due_at=? WHERE sequence_id=?", (iso(), seq_id))
    assert run(engine.send_one()) is False

    # discovery no longer accepts US prospects at all
    engine.db.execute("DELETE FROM messages"); engine.db.execute("DELETE FROM sequences")
    engine.db.execute("DELETE FROM contacts"); engine.db.execute("DELETE FROM companies")
    assert run(engine.pipeline.discover("hardware")) == 0


def test_uk_prospect_sends_without_postal_address(engine, run):
    engine.cfg.sender.postal_address = ""
    engine.brain.answers["discover"]["companies"][0]["country"] = "GB"
    engine.brain.answers["research"]["country"] = "GB"
    run(engine.top_up())
    seq_id = engine.recorder.drafts[0]
    assert engine.approve(seq_id) == []
    assert run(engine.send_one()) is True
    text = message_from_bytes(_sent_files(engine)[0].read_bytes(),
                              policy=default_policy).get_body(preferencelist=("plain",)).get_content()
    assert "unsubscribe" in text and "Test Street" not in text
    assert text.rstrip().endswith("won't email again.")


def test_reject_and_block(engine, run):
    run(engine.top_up())
    seq_id = engine.recorder.drafts[0]
    engine.reject(seq_id, block=True)
    assert engine.db.is_suppressed("x@trailcam.example")
    assert engine.db.scalar("SELECT COUNT(*) FROM messages WHERE status='cancelled'") == 3
    assert run(engine.send_one()) is False


def test_daily_cap_and_ramp(engine, run):
    engine.set_cap(1)
    run(engine.top_up())
    seq_id = engine.recorder.drafts[0]
    engine.approve(seq_id)
    engine.db.execute("UPDATE messages SET due_at=? WHERE sequence_id=?", (iso(), seq_id))
    assert run(engine.send_one()) is True
    assert run(engine.send_one()) is False          # cap of 1 reached

    # next day: yesterday hit the cap with no bounces → cap rises by ramp_step
    engine.db.set("ramp_date", "2000-01-01")
    engine.db.execute("UPDATE messages SET sent_at=? WHERE status='sent'",
                      (iso(engine.db.start_of_utc_day(-1).replace(hour=12)),))
    engine.daily_housekeeping()
    assert engine.daily_cap() == 1 + engine.cfg.volume.ramp_step


def test_send_window_follows_recipient_time(engine):
    from datetime import datetime, timezone
    engine.cfg.schedule.send_days = [0, 1, 2, 3, 4]
    engine.cfg.schedule.send_start_hour, engine.cfg.schedule.send_end_hour = 9, 16
    tuesday_3pm_london = datetime(2026, 10, 13, 14, 0, tzinfo=timezone.utc)
    assert engine._in_window("GB", tuesday_3pm_london)
    assert not engine._in_window("US", datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc))   # 7am Chicago
    assert not engine._in_window("GB", datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc))   # Saturday


def test_research_country_names_are_understood(engine, run):
    engine.brain.answers["discover"]["companies"][0]["country"] = "United Kingdom"
    engine.brain.answers["research"]["country"] = "England"
    engine.cfg.targets.allowed_countries = ["GB"]
    run(engine.top_up())
    assert engine.db.one("SELECT country, status FROM companies")["country"] == "GB"
    assert engine.recorder.drafts


def test_next_window_skips_evenings_and_weekends(engine):
    from datetime import datetime, timezone
    engine.cfg.schedule.send_days = [0, 1, 2, 3, 4]
    engine.cfg.schedule.send_start_hour, engine.cfg.schedule.send_end_hour = 9, 16
    friday_evening_uk = datetime(2026, 10, 16, 16, 38, tzinfo=timezone.utc)      # 17:38 London
    go = engine.next_window("GB", friday_evening_uk)
    assert (go.weekday(), go.hour) == (0, 9) and go.tzname() in ("BST", "GMT")    # Monday 9:00 London
    tuesday_morning_uk = datetime(2026, 10, 13, 9, 30, tzinfo=timezone.utc)       # 10:30 London
    assert engine.next_window("GB", tuesday_morning_uk) == tuesday_morning_uk


def test_card_shows_delivery_status(engine, run):
    from outreach_agent import discord_bot as d
    run(engine.top_up())
    seq_id = engine.recorder.drafts[0]
    engine.approve(seq_id)
    run(engine.send_one())
    text = d.draft_embed(engine, seq_id).description
    assert "✉️ Email sent" in text and "Follow-up 1 queued" in text


def test_api_spend_limit_stops_drafting_and_alerts(engine, run):
    from outreach_agent.brain import Brain
    engine.brain.json = Brain.json.__get__(engine.brain)        # use the real budget checks
    engine.brain.db.set("brain_provider", "api")
    engine.cfg.brain.api_budget_usd = 1
    engine.db.log("api_usage", {"in": 0, "out": 0, "web_searches": 0, "cost": 1.5})
    run(engine.top_up())
    assert engine.recorder.drafts == []
    assert "monthly API budget" in engine.recorder.alerts[0]
    assert engine.db.scalar("SELECT COUNT(*) FROM companies WHERE status='error'") == 0



def test_consecutive_searches_use_different_niches(engine, run):
    seen = []
    original = engine.brain.json

    async def spy(**kw):
        if kw["task"] == "discover":
            seen.append(kw["prompt"].split("Focus this run on: ")[1].split("\n")[0])
        return await original(**kw)

    engine.brain.json = spy
    for _ in range(6):
        run(engine.pipeline.discover("hardware"))
    assert all(a != b for a, b in zip(seen, seen[1:]))



def test_stopped_blocks_sending_but_still_honours_replies(engine, run):
    run(engine.top_up())
    seq_id = engine.recorder.drafts[0]
    engine.approve(seq_id)
    run(engine.send_one())
    engine.db.execute("UPDATE messages SET due_at=? WHERE sequence_id=? AND step=2", (iso(), seq_id))
    engine.stop()
    assert run(engine.send_one()) is False                    # follow-up held while stopped
    inc = _reply_to(engine, seq_id, "Please unsubscribe me.")
    run(engine.handle_inbound(inc, *engine._match_sequence(inc)))
    assert engine.db.is_suppressed("sarah@trailcam.example")  # but the unsubscribe is honoured


def test_batch_note_counts_only_drafts_still_waiting(engine, run):
    run(engine.top_up())
    assert engine.recorder.batches == [1]

    # the owner approves each card as it arrives, before the run ends: no "N waiting" note
    engine.db.execute("UPDATE companies SET status='new'")
    engine.db.execute("DELETE FROM messages"); engine.db.execute("DELETE FROM sequences")
    engine.recorder.batches.clear()
    original = engine.recorder.draft_ready

    async def approve_at_once(seq_id):
        await original(seq_id)
        engine.approve(seq_id)
    engine.recorder.draft_ready = approve_at_once
    run(engine.top_up())
    assert len(engine.recorder.drafts) == 2 and engine.recorder.batches == []
