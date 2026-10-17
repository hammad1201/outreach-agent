"""The running system: a heartbeat that reads replies and keeps drafts flowing, and a send loop
that only ever sends what you approved, inside the recipient's working hours, under the daily cap."""

import asyncio
import logging
import random
import re
from datetime import datetime, timedelta
from email.utils import formatdate
from zoneinfo import ZoneInfo

from . import gate, mailer
from .brain import Brain, BrainError, BudgetExhausted, SpendLimitReached
from .config import ADDRESS_REQUIRED, Config
from .db import DB, iso, parse_iso, utcnow
from .mailer import MailError, Mailer
from .pipeline import Pipeline

log = logging.getLogger("outreach.engine")

COUNTRY_TZ = {
    "US": "America/Chicago", "CA": "America/Toronto", "GB": "Europe/London", "IE": "Europe/Dublin",
    "AU": "Australia/Sydney", "NZ": "Pacific/Auckland", "NL": "Europe/Amsterdam", "SE": "Europe/Stockholm",
    "DK": "Europe/Copenhagen", "NO": "Europe/Oslo", "FI": "Europe/Helsinki", "BE": "Europe/Brussels",
    "FR": "Europe/Paris", "ES": "Europe/Madrid", "IT": "Europe/Rome", "PT": "Europe/Lisbon",
    "CH": "Europe/Zurich", "SG": "Asia/Singapore", "AE": "Asia/Dubai", "IN": "Asia/Kolkata",
    "ZA": "Africa/Johannesburg",
}
UNSUBSCRIBE_RE = re.compile(
    r"\b(unsubscribe|remove me|take me off|stop (emailing|contacting|sending)|do not (email|contact)|"
    r"don'?t (email|contact)|opt[ -]?out|no more emails)\b", re.I)


class Notifier:
    """Where the engine reports. The Discord bot implements this; the console one is for dry runs."""

    async def draft_ready(self, seq_id: int): ...
    async def manual_ready(self, app_id: int): ...
    async def drafts_batch(self, count: int): ...
    async def sequence_changed(self, seq_id: int): ...
    async def reply_received(self, inbound_id: int, draft_id: int | None): ...
    async def alert(self, text: str): ...


class ConsoleNotifier(Notifier):
    async def draft_ready(self, seq_id):
        print(f"[draft ready] sequence {seq_id}")

    async def drafts_batch(self, count):
        print(f"[drafts] {count} new draft(s) waiting")

    async def reply_received(self, inbound_id, draft_id):
        print(f"[reply] inbound {inbound_id} (draft {draft_id})")

    async def alert(self, text):
        print(f"[alert] {text}")


def _restamp(msg, message_id: str, sent_at: str | None):
    """Give a rebuilt email the Message-ID and date it was really sent with."""
    del msg["Message-ID"]
    msg["Message-ID"] = message_id
    if sent_at:
        del msg["Date"]
        msg["Date"] = formatdate(parse_iso(sent_at).timestamp())
    return msg


class Engine:
    def __init__(self, cfg: Config, db: DB, brain: Brain, mail: Mailer, notifier: Notifier | None = None):
        self.cfg = cfg
        self.db = db
        self.brain = brain
        self.mail = mail
        self.pipeline = Pipeline(cfg, db, brain)
        self.notifier = notifier or ConsoleNotifier()
        self.pipeline_lock = asyncio.Lock()
        self.last_heartbeat = None
        if self.db.get("daily_cap") is None:
            self.db.set("daily_cap", cfg.volume.start_daily_cap)

    # ── state ──

    @property
    def paused(self) -> bool:
        return self.db.get("paused") == "1"

    def pause(self, reason: str) -> None:
        self.db.set("paused", "1")
        self.db.set("pause_reason", reason)
        self.db.log("paused", reason)

    def resume(self) -> None:
        self.db.set("paused", "0")
        self.db.set("pause_reason", "")
        self.db.log("resumed")

    @property
    def stopped(self) -> bool:
        """Stopped = no searching, drafting or sending. The inbox is still read, so replies and
        unsubscribes are always honoured."""
        return self.db.get("stopped") == "1"

    def stop(self) -> None:
        self.db.set("stopped", "1")
        self.db.log("stopped")

    def start(self) -> None:
        self.db.set("stopped", "0")
        self.db.log("started")

    def daily_cap(self) -> int:
        return int(self.db.get("daily_cap", self.cfg.volume.start_daily_cap))

    def set_cap(self, value: int) -> int:
        value = max(0, min(int(value), self.cfg.volume.max_daily_cap))
        self.db.set("daily_cap", value)
        return value

    def sent_today(self) -> int:
        return self.db.sent_since(self.db.start_of_utc_day())

    def status(self) -> dict:
        q = self.db.scalar
        rate, n = self.db.recent_bounce_rate()
        return {
            "paused": self.paused, "pause_reason": self.db.get("pause_reason", ""), "stopped": self.stopped,
            "awaiting_approval": q("SELECT COUNT(*) FROM sequences WHERE status='pending'", default=0),
            "apply_by_form": q("SELECT COUNT(*) FROM manual_apps WHERE status='pending'", default=0),
            "active_sequences": q("SELECT COUNT(*) FROM sequences WHERE status='active'", default=0),
            "queued_to_send": q("SELECT COUNT(*) FROM messages WHERE status='approved' AND due_at <= ?",
                                (iso(),), 0),
            "sent_today": self.sent_today(), "daily_cap": self.daily_cap(),
            "sent_total": q("SELECT COUNT(*) FROM messages WHERE status IN ('sent','bounced')", default=0),
            "replies": q("SELECT COUNT(*) FROM inbound WHERE kind='reply'", default=0),
            "bounce_rate": rate, "bounce_sample": n,
            "prospects_new": q("SELECT COUNT(*) FROM companies WHERE status='new'", default=0),
            "prospects_total": q("SELECT COUNT(*) FROM companies", default=0),
            "model_calls_today": self.brain.calls_today(), "model_call_budget": self.cfg.brain.max_calls_per_day,
            "provider": self.brain.provider,
        }

    # ── your decisions ──

    def decide_manual(self, app_id: int, done: bool) -> None:
        """You sent it through their form yourself, or skipped it."""
        app = self.db.one("SELECT * FROM manual_apps WHERE id = ?", (app_id,))
        if app is None or app["status"] != "pending":
            return
        self.db.execute("UPDATE manual_apps SET status=?, decided_at=? WHERE id=?",
                        ("done" if done else "skipped", iso(), app_id))
        self.db.execute("UPDATE companies SET status=?, status_reason=?, updated_at=? WHERE id=?",
                        ("contacted" if done else "disqualified", "" if done else "skipped by you", iso(),
                         app["company_id"]))
        self.db.log("manual_done" if done else "manual_skipped", {"app": app_id})

    def approve(self, seq_id: int) -> list[str]:
        seq = self.db.one("SELECT * FROM sequences WHERE id = ?", (seq_id,))
        if seq is None or seq["status"] != "pending":
            return [f"sequence {seq_id} is not waiting for approval"]
        country = self.db.scalar("SELECT country FROM companies WHERE id = ?", (seq["company_id"],), "")
        if not self._may_email(country):
            return [f"{country} law requires your postal address in every email — add postal_address in "
                    "config.toml to contact this company, or reject it"]
        msgs = self.db.all("SELECT * FROM messages WHERE sequence_id = ? ORDER BY step", (seq_id,))
        problems = gate.hard_problems(msgs[0]["subject"], [m["body"] for m in msgs])
        if problems:
            return problems
        now = iso()
        self.db.execute("UPDATE sequences SET status='active', decided_at=? WHERE id=?", (now, seq_id))
        self.db.execute("UPDATE messages SET status='approved' WHERE sequence_id=? AND status='draft'", (seq_id,))
        self.db.execute("UPDATE messages SET due_at=? WHERE sequence_id=? AND step=1", (now, seq_id))
        self.db.log("approved", {"sequence": seq_id})
        return []

    def reject(self, seq_id: int, block: bool = False) -> None:
        seq = self.db.one("SELECT * FROM sequences WHERE id = ?", (seq_id,))
        if seq is None:
            return
        self.db.execute("UPDATE sequences SET status='rejected', decided_at=? WHERE id=?", (iso(), seq_id))
        self.db.execute("UPDATE messages SET status='cancelled' WHERE sequence_id=? AND status IN ('draft','approved')",
                        (seq_id,))
        company = self.db.one("SELECT domain FROM companies WHERE id = ?", (seq["company_id"],))
        if block:
            self.db.suppress(company["domain"], "blocked by you")
        self.db.execute("UPDATE companies SET status=?, updated_at=? WHERE id=?",
                        ("blocked" if block else "dead", iso(), seq["company_id"]))
        self.db.log("rejected", {"sequence": seq_id, "block": block})

    def save_edit(self, seq_id: int, subject: str, bodies: list[str]) -> list[str]:
        for step, body in enumerate(bodies, start=1):
            subj = subject.strip() if step == 1 else f"Re: {subject.strip()}"
            self.db.execute("UPDATE messages SET subject=?, body=? WHERE sequence_id=? AND step=?",
                            (subj, body.strip(), seq_id, step))
        company = self.db.scalar("SELECT c.name FROM companies c JOIN sequences s ON s.company_id=c.id WHERE s.id=?",
                                 (seq_id,), "")
        return gate.check_sequence(subject, bodies, company)

    # ── sending ──

    def next_window(self, country: str, after: datetime) -> datetime:
        """The first moment at or after `after` that falls inside the recipient's sending hours."""
        tz = ZoneInfo(COUNTRY_TZ.get(country or "", "UTC"))
        s = self.cfg.schedule
        local = after.astimezone(tz)
        for day in range(8):
            d = (local + timedelta(days=day)).date()
            start = datetime(d.year, d.month, d.day, s.send_start_hour, tzinfo=tz)
            end = datetime(d.year, d.month, d.day, min(s.send_end_hour, 23), 59 if s.send_end_hour >= 24 else 0, tzinfo=tz)
            if d.weekday() not in s.send_days or local >= end:
                continue
            return max(start, local)
        return after

    def _in_window(self, country: str, now: datetime) -> bool:
        local = now.astimezone(ZoneInfo(COUNTRY_TZ.get(country or "", "UTC")))
        s = self.cfg.schedule
        return local.weekday() in s.send_days and s.send_start_hour <= local.hour < s.send_end_hour

    def _may_email(self, country: str) -> bool:
        return (not self.cfg.business.commercial or self.cfg.has_postal_address
                or (country or "").upper() not in ADDRESS_REQUIRED)

    def _next_due(self, now: datetime):
        rows = self.db.all(
            "SELECT m.*, c.country, c.name AS company, ct.email AS to_email, s.id AS seq_id"
            " FROM messages m JOIN sequences s ON s.id = m.sequence_id"
            " JOIN companies c ON c.id = s.company_id JOIN contacts ct ON ct.id = s.contact_id"
            " WHERE m.status = 'approved' AND s.status = 'active' AND m.due_at IS NOT NULL AND m.due_at <= ?"
            " ORDER BY m.step DESC, m.due_at LIMIT 200", (iso(now),))
        return next((r for r in rows if self._in_window(r["country"], now) and self._may_email(r["country"])), None)

    def _blocked_reason(self) -> str:
        if self.stopped:
            return "stopped"
        if self.paused:
            return "paused"
        backoff = parse_iso(self.db.get("send_backoff_until"))
        if backoff and backoff > utcnow():
            return "backing off after a mail-server error"
        if self.sent_today() >= self.daily_cap():
            return "daily cap reached"
        return ""

    async def send_one(self) -> bool:
        reason = self._blocked_reason()
        if reason:
            return False
        now = utcnow()
        m = self._next_due(now)
        if m is None:
            return False
        if self.db.is_suppressed(m["to_email"]):
            self._stop_sequence(m["seq_id"], "suppressed")
            return False
        problems = gate.hard_problems(m["subject"], [m["body"]])
        if problems:
            self.db.execute("UPDATE messages SET status='failed', error=? WHERE id=?", ("; ".join(problems), m["id"]))
            return False

        msg = self._compose_step(m["seq_id"], m["step"], m["to_email"], m["subject"], m["body"])
        try:
            message_id = await asyncio.to_thread(self.mail.send, msg)
        except MailError as e:
            if "recipient refused" in str(e):
                self._mark_bounced(m["id"], m["to_email"], str(e))
                await self._check_bounce_rate()
                return True
            self.db.set("send_backoff_until", iso(now + timedelta(minutes=30)))
            self.db.execute("UPDATE messages SET error=? WHERE id=?", (str(e), m["id"]))
            await self.notifier.alert(f"Sending stopped for 30 minutes: {e}")
            return False

        sent_at = iso()
        self.db.execute("UPDATE messages SET status='sent', sent_at=?, message_id=?, error=NULL WHERE id=?",
                        (sent_at, message_id, m["id"]))
        if m["step"] == 1:
            base = parse_iso(sent_at)
            for step, days in enumerate(self.cfg.schedule.followup_days, start=2):
                self.db.execute("UPDATE messages SET due_at=? WHERE sequence_id=? AND step=? AND status='approved'",
                                (iso(base + timedelta(days=days)), m["seq_id"], step))
            self.db.execute("UPDATE companies SET status='contacted', updated_at=? WHERE id ="
                            " (SELECT company_id FROM sequences WHERE id=?)", (sent_at, m["seq_id"]))
        remaining = self.db.scalar("SELECT COUNT(*) FROM messages WHERE sequence_id=? AND status='approved'",
                                   (m["seq_id"],), 0)
        if remaining == 0:
            self.db.execute("UPDATE sequences SET status='completed' WHERE id=? AND status='active'", (m["seq_id"],))
        self.db.log("sent", {"sequence": m["seq_id"], "step": m["step"], "to": m["to_email"]})
        await self.notifier.sequence_changed(m["seq_id"])
        return True

    def _compose_step(self, seq_id: int, step: int, to: str, subject: str, body: str):
        """One email of a sequence, threaded under the ones already sent."""
        earlier = [r["message_id"] for r in self.db.all(
            "SELECT message_id FROM messages WHERE sequence_id=? AND step<? AND message_id IS NOT NULL ORDER BY step",
            (seq_id, step))]
        attach = self.cfg.business.path(self.cfg.sender.attachment) if step == 1 else None
        return mailer.compose(self.cfg, to=to, subject=subject, body=body,
                              in_reply_to=(earlier[-1] if earlier else ""), references=" ".join(earlier[:-1]),
                              attachments=(attach,) if attach else ())

    def _compose_reply(self, inc, text: str):
        subject = inc["subject"] if re.match(r"^\s*re:", inc["subject"] or "", re.I) else f"Re: {inc['subject']}"
        refs = " ".join(r["message_id"] for r in self.db.all(
            "SELECT message_id FROM messages WHERE sequence_id=? AND message_id IS NOT NULL ORDER BY step",
            (inc["sequence_id"],)))
        return mailer.compose(self.cfg, to=inc["from_email"], subject=subject, body=text,
                              in_reply_to=inc["message_id"], references=refs, cold=False)

    def rebuild_sent(self) -> list:
        """Every email already sent, rebuilt with its original Message-ID and date, e.g. to file
        copies in a Sent folder after the fact."""
        out = []
        rows = self.db.all(
            "SELECT m.*, ct.email AS to_email FROM messages m JOIN sequences s ON s.id = m.sequence_id"
            " JOIN contacts ct ON ct.id = s.contact_id"
            " WHERE m.status IN ('sent','bounced') AND m.message_id IS NOT NULL ORDER BY m.sent_at")
        for m in rows:
            msg = self._compose_step(m["sequence_id"], m["step"], m["to_email"], m["subject"], m["body"])
            out.append(_restamp(msg, m["message_id"], m["sent_at"]))
        for d in self.db.all("SELECT * FROM reply_drafts WHERE status='sent' AND sent_message_id IS NOT NULL"):
            inc = self.db.one("SELECT * FROM inbound WHERE id=?", (d["inbound_id"],))
            sent_at = self.db.scalar("SELECT ts FROM events WHERE kind='reply_sent' AND detail LIKE ?",
                                     (f'%"draft": {d["id"]}}}%',), d["created_at"])
            out.append(_restamp(self._compose_reply(inc, d["body"]), d["sent_message_id"], sent_at))
        return out

    async def send_loop(self) -> None:
        while True:
            try:
                sent = await self.send_one()
            except Exception:
                log.exception("send loop error")
                sent = False
            v = self.cfg.volume
            await asyncio.sleep(random.uniform(v.min_gap_seconds, v.max_gap_seconds) if sent else 60)

    # ── bounces, ramp, kill switch ──

    def _stop_sequence(self, seq_id: int, reason: str) -> None:
        self.db.execute("UPDATE sequences SET status='stopped', stop_reason=? WHERE id=? AND status IN ('active','pending')",
                        (reason, seq_id))
        self.db.execute("UPDATE messages SET status='cancelled' WHERE sequence_id=? AND status IN ('approved','draft')",
                        (seq_id,))

    def _mark_bounced(self, message_row_id: int, address: str, why: str = "") -> None:
        row = self.db.one("SELECT sequence_id FROM messages WHERE id=?", (message_row_id,))
        self.db.execute("UPDATE messages SET status='bounced', error=?, sent_at=COALESCE(sent_at, ?) WHERE id=?",
                        (why[:300], iso(), message_row_id))
        self._stop_sequence(row["sequence_id"], "bounced")
        self.db.execute("UPDATE companies SET status='bounced', updated_at=? WHERE id ="
                        " (SELECT company_id FROM sequences WHERE id=?)", (iso(), row["sequence_id"]))
        if address:
            self.db.suppress(address, "bounced")
        self.db.log("bounce", {"message": message_row_id, "to": address})

    async def _check_bounce_rate(self) -> None:
        rate, n = self.db.recent_bounce_rate()
        if n >= 20 and rate >= self.cfg.volume.pause_bounce_rate and not self.paused:
            self.pause(f"bounce rate {rate:.0%} over the last {n} sends")
            await self.notifier.alert(
                f"⛔ Sending paused automatically: {rate:.0%} of the last {n} emails bounced. A rate this high "
                f"damages {self.cfg.sender.email.split('@')[-1]}'s reputation. Check recent bounces, then `/resume` when ready.")

    def daily_housekeeping(self) -> None:
        today = utcnow().date().isoformat()
        if self.db.get("ramp_date") == today:
            return
        last = self.db.get("ramp_date")
        self.db.set("ramp_date", today)
        if last is None:
            return
        v = self.cfg.volume
        cap = self.daily_cap()
        yesterday = self.db.sent_since(self.db.start_of_utc_day(-1)) - self.sent_today()
        rate, n = self.db.recent_bounce_rate()
        if n >= 20 and rate >= v.hold_bounce_rate:
            self.db.log("ramp", {"cap": cap, "action": "hold", "bounce_rate": rate})
        elif yesterday >= 0.8 * cap and cap < v.max_daily_cap:
            self.set_cap(cap + v.ramp_step)
            self.db.log("ramp", {"cap": self.daily_cap(), "action": "raise"})

    # ── inbox ──

    def _match_sequence(self, inc: mailer.Inbound) -> tuple[int | None, int | None]:
        """(sequence_id, message row id) this inbound mail belongs to, or (None, None)."""
        ids = inc.bounced_ids if inc.is_bounce else [inc.in_reply_to, *inc.references]
        for mid in filter(None, ids):
            row = self.db.one("SELECT id, sequence_id FROM messages WHERE message_id = ?", (mid,))
            if row:
                return row["sequence_id"], row["id"]
        address = inc.bounced_rcpt if inc.is_bounce else inc.from_email
        if address:
            row = self.db.one(
                "SELECT m.id, m.sequence_id FROM messages m JOIN sequences s ON s.id=m.sequence_id"
                " JOIN contacts ct ON ct.id=s.contact_id WHERE ct.email=? AND m.status IN ('sent','bounced')"
                " ORDER BY m.sent_at DESC LIMIT 1", (address,))
            if row:
                return row["sequence_id"], row["id"]
        return None, None

    async def poll_inbox(self) -> int:
        if not self.mail.configured() or self.mail.dry_run:
            return 0
        last = self.db.get("imap_last_uid")
        messages, new_last, validity = await asyncio.to_thread(
            self.mail.fetch_since, int(last) if last else None, self.db.get("imap_uidvalidity"))
        self.db.set("imap_last_uid", new_last)
        self.db.set("imap_uidvalidity", validity)
        handled = 0
        for uid, raw in messages:
            inc = mailer.parse(uid, raw)
            if inc.from_email == self.cfg.sender.email:
                continue
            seq_id, msg_row = self._match_sequence(inc)
            if seq_id is None:
                continue
            await self.handle_inbound(inc, seq_id, msg_row)
            handled += 1
        return handled

    async def handle_inbound(self, inc: mailer.Inbound, seq_id: int, msg_row: int) -> None:
        kind = "bounce" if inc.is_bounce else "auto" if inc.is_auto else "reply"
        if kind == "reply" and UNSUBSCRIBE_RE.search(f"{inc.subject}\n{inc.body[:600]}"):
            kind = "unsubscribe"
        inbound_id = self.db.insert(
            "INSERT OR IGNORE INTO inbound(uid, message_id, in_reply_to, from_email, subject, body, received_at,"
            " sequence_id, kind) VALUES(?,?,?,?,?,?,?,?,?)",
            (inc.uid, inc.message_id, inc.in_reply_to, inc.from_email, inc.subject, inc.body, inc.date, seq_id, kind))
        company = self.db.one("SELECT c.* FROM companies c JOIN sequences s ON s.company_id=c.id WHERE s.id=?",
                              (seq_id,))

        if kind == "bounce":
            to = self.db.scalar("SELECT ct.email FROM contacts ct JOIN sequences s ON s.contact_id=ct.id WHERE s.id=?",
                                (seq_id,), inc.bounced_rcpt)
            self._mark_bounced(msg_row, to, "bounced")
            self.db.execute("UPDATE inbound SET handled=1 WHERE id=?", (inbound_id,))
            await self._check_bounce_rate()
            return
        if kind == "auto":
            self.db.execute("UPDATE inbound SET handled=1 WHERE id=?", (inbound_id,))
            return

        self._stop_sequence(seq_id, "replied")
        if kind == "unsubscribe":
            self.db.suppress(inc.from_email, "asked to unsubscribe")
            self.db.suppress(company["domain"], "asked to unsubscribe")
            self.db.execute("UPDATE companies SET status='dead', status_reason='unsubscribed', updated_at=? WHERE id=?",
                            (iso(), company["id"]))
            self.db.execute("UPDATE inbound SET handled=1, classification='unsubscribe' WHERE id=?", (inbound_id,))
            await self.notifier.alert(f"🚫 {company['name']} ({inc.from_email}) asked to unsubscribe. "
                                      "They're on the do-not-contact list; nothing more will be sent.")
            return

        self.db.execute("UPDATE companies SET status='replied', updated_at=? WHERE id=?", (iso(), company["id"]))
        draft_id = None
        row = self.db.one("SELECT * FROM inbound WHERE id=?", (inbound_id,))
        try:
            result = await self.pipeline.classify_reply(row)
        except BrainError as e:
            result = {"classification": "other", "summary": f"(couldn't analyse: {e})", "draft": ""}
        self.db.execute("UPDATE inbound SET classification=?, summary=?, handled=1 WHERE id=?",
                        (result["classification"], result["summary"], inbound_id))
        if result["classification"] == "not_interested":
            self.db.suppress(inc.from_email, "not interested")
            self.db.execute("UPDATE companies SET status='dead', updated_at=? WHERE id=?", (iso(), company["id"]))
        if result.get("draft", "").strip():
            draft_id = self.db.insert("INSERT INTO reply_drafts(inbound_id, body, created_at) VALUES(?,?,?)",
                                      (inbound_id, gate.strip_signoff(result["draft"]), iso()))
        self.db.log("reply", {"sequence": seq_id, "classification": result["classification"]})
        await self.notifier.reply_received(inbound_id, draft_id)

    async def send_reply(self, draft_id: int, body: str | None = None) -> str:
        d = self.db.one("SELECT * FROM reply_drafts WHERE id=?", (draft_id,))
        inc = self.db.one("SELECT * FROM inbound WHERE id=?", (d["inbound_id"],))
        text = (body or d["body"]).strip()
        msg = self._compose_reply(inc, text)
        message_id = await asyncio.to_thread(self.mail.send, msg)
        self.db.execute("UPDATE reply_drafts SET status='sent', body=?, sent_message_id=? WHERE id=?",
                        (text, message_id, draft_id))
        self.db.log("reply_sent", {"draft": draft_id})
        return message_id

    # ── keeping drafts flowing ──

    async def top_up(self, max_calls: int = 14) -> dict:
        """Fill the approval queue: write drafts for qualified prospects, research new ones,
        and discover more when the pool runs low. Bounded per run so a heartbeat stays short."""
        done = {"drafted": 0, "researched": 0, "discovered": 0}
        drafted, notes = [], []
        if self.pipeline_lock.locked():
            return done
        async with self.pipeline_lock:
            start_calls = self.brain.calls_today()

            def spent():
                return self.brain.calls_today() - start_calls

            target = self.cfg.schedule.approval_queue_target
            try:
                while spent() < max_calls and self.brain.budget_left() > 3:
                    pending = self.db.scalar("SELECT COUNT(*) FROM sequences WHERE status='pending'", default=0) \
                        + self.db.scalar("SELECT COUNT(*) FROM manual_apps WHERE status='pending'", default=0)
                    if pending >= target:
                        break
                    form_only = self.db.one(
                        "SELECT c.id FROM companies c WHERE c.status='apply_manually' AND NOT EXISTS"
                        " (SELECT 1 FROM manual_apps m WHERE m.company_id=c.id) ORDER BY c.fit_score DESC, c.id LIMIT 1")
                    if form_only:
                        app_id = await self._safely(self.pipeline.write_note, form_only["id"])
                        if app_id:
                            done["drafted"] += 1
                            notes.append(app_id)
                            await self.notifier.manual_ready(app_id)
                        continue
                    ready = self.db.one(
                        "SELECT c.id FROM companies c WHERE c.status='qualified'"
                        " AND EXISTS (SELECT 1 FROM contacts ct WHERE ct.company_id=c.id)"
                        " ORDER BY c.fit_score DESC, c.id LIMIT 1")
                    if ready:
                        seq_id = await self._safely(self.pipeline.write, ready["id"])
                        if seq_id:
                            done["drafted"] += 1
                            drafted.append(seq_id)
                            await self.notifier.draft_ready(seq_id)
                        continue
                    fresh = self.db.one("SELECT id FROM companies WHERE status='new' ORDER BY id LIMIT 1")
                    if fresh:
                        await self._safely(self.pipeline.research, fresh["id"])
                        done["researched"] += 1
                        continue
                    segment = self._next_segment()
                    added = await self.pipeline.discover(segment)
                    done["discovered"] += added
                    if added == 0:
                        break
            except BudgetExhausted:
                pass
            except BrainError as e:
                await self._brain_alert(str(e))
        # Each card is posted as soon as it's written, so some may already be decided by the time the
        # run ends: count only the ones still waiting.
        waiting = self.db.scalar(
            f"SELECT COUNT(*) FROM sequences WHERE status='pending' AND id IN ({','.join('?' * len(drafted))})",
            drafted, 0) if drafted else 0
        waiting += self.db.scalar(
            f"SELECT COUNT(*) FROM manual_apps WHERE status='pending' AND id IN ({','.join('?' * len(notes))})",
            notes, 0) if notes else 0
        if waiting:
            await self.notifier.drafts_batch(waiting)
        return done

    async def _safely(self, fn, company_id: int):
        try:
            return await fn(company_id)
        except (BudgetExhausted, SpendLimitReached):
            raise
        except BrainError as e:
            if "logged in" in str(e) or "API key" in str(e) or "not found" in str(e):
                raise
            self.db.execute("UPDATE companies SET status='error', status_reason=?, updated_at=? WHERE id=?",
                            (str(e)[:300], iso(), company_id))
        except Exception as e:  # a broken website or odd page must not stall the pipeline
            log.exception("pipeline step failed for company %s", company_id)
            self.db.execute("UPDATE companies SET status='error', status_reason=?, updated_at=? WHERE id=?",
                            (str(e)[:300], iso(), company_id))
        return None

    def _next_segment(self) -> str:
        segments = self.pipeline.active_segments()
        if not segments:
            raise ValueError("no segments: add at least one `## name` section to the profile's segments file")
        i = int(self.db.get("segment_cursor", "0") or 0)
        self.db.set("segment_cursor", i + 1)
        return segments[i % len(segments)]

    async def _brain_alert(self, text: str) -> None:
        if self.db.get("last_brain_alert") == text:
            return
        self.db.set("last_brain_alert", text)
        await self.notifier.alert(f"⚠️ Drafting is stuck: {text}")

    # ── heartbeat ──

    async def heartbeat(self) -> None:
        self.last_heartbeat = utcnow()
        self.daily_housekeeping()
        try:
            await self.poll_inbox()
        except MailError as e:
            await self._brain_alert(f"can't read the inbox — {e}")
        except Exception:
            log.exception("inbox poll failed")
        if not self.stopped:
            await self.top_up()

    async def heartbeat_loop(self) -> None:
        while True:
            try:
                await self.heartbeat()
            except Exception:
                log.exception("heartbeat error")
            await asyncio.sleep(self.cfg.schedule.heartbeat_minutes * 60)
