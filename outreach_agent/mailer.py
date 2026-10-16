"""Sending through SMTP and reading replies through IMAP.

The inbox is opened read-only: nothing is marked read, moved or deleted, and only mail that
belongs to an outreach thread (or a bounce of one) is ever stored.
"""

import email
import html
import imaplib
import logging
import re
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from email.policy import default as default_policy
from email.utils import formataddr, formatdate, make_msgid, parseaddr

from .config import Config, data_dir, env

log = logging.getLogger("outreach.mailer")

MSGID_RE = re.compile(r"<[^<>\s]+@[^<>\s]+>")
BOUNCE_SENDERS = re.compile(r"mailer-daemon|postmaster|mail delivery", re.I)
AUTO_SUBJECT = re.compile(r"out of (the )?office|automatic reply|auto[- ]?reply|autoreply|away from|on leave|vacation", re.I)


class MailError(Exception):
    pass


def signature(cfg: Config) -> str:
    s = cfg.sender
    return f"{s.name}\n{s.title}\n{cfg.business.domain}"


def footer(cfg: Config) -> str:
    address = cfg.sender.postal_address.strip()
    optout = "Not relevant? Reply \"unsubscribe\" and I won't email again."
    return f"{address}\n{optout}" if address else optout


def full_body(cfg: Config, body: str, cold: bool = True) -> str:
    text = f"{body.strip()}\n\n{signature(cfg)}"
    return f"{text}\n\n{footer(cfg)}" if cold else text


def html_signature(cfg: Config) -> str:
    """The profile's HTML signature without any <!-- comments -->, or "" when there is none."""
    path = cfg.business.path(cfg.business.signature_file)
    if path is None or not path.exists():
        return ""
    return re.sub(r"<!--.*?-->", "", path.read_text(), flags=re.S).strip()


def html_body(cfg: Config, body: str, cold: bool = True) -> str | None:
    """The email as HTML: the note in plain, ordinary type so it still reads like a personal email,
    then the brand signature, then the address and opt-out line, small and grey."""
    sig = html_signature(cfg)
    if not sig:
        return None
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", body.strip()) if p.strip()]
    note = "".join(f'<p style="margin:0 0 14px;">{html.escape(p).replace(chr(10), "<br>")}</p>' for p in paragraphs)
    parts = [f'<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.5;color:#222;">{note}</div>',
             f'<div style="margin-top:22px;">{sig}</div>']
    if cold:
        foot = html.escape(footer(cfg)).replace("\n", "<br>")
        parts.append(f'<div style="margin-top:22px;font-family:Arial,Helvetica,sans-serif;font-size:11px;'
                     f'line-height:1.5;color:#8A9AA8;">{foot}</div>')
    return "<!doctype html><html><body>" + "".join(parts) + "</body></html>"


def compose(cfg: Config, *, to: str, subject: str, body: str, in_reply_to: str = "",
            references: str = "", cold: bool = True) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = formataddr((cfg.sender.name, cfg.sender.email))
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=False)
    msg["Message-ID"] = make_msgid(domain=cfg.sender.email.split("@")[1])
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = (references + " " + in_reply_to).strip()
    if cold:
        msg["List-Unsubscribe"] = f"<mailto:{cfg.sender.email}?subject=unsubscribe>"
    msg.set_content(full_body(cfg, body, cold))
    rich = html_body(cfg, body, cold)
    if rich:
        msg.add_alternative(rich, subtype="html")
    return msg


class Mailer:
    def __init__(self, cfg: Config, dry_run: bool = False):
        self.cfg = cfg
        self.dry_run = dry_run
        self.user = env("SMTP_USER")
        self.password = env("SMTP_PASSWORD").replace(" ", "")
        self.imap_user = env("IMAP_USER") or self.user
        self.imap_password = (env("IMAP_PASSWORD") or env("SMTP_PASSWORD")).replace(" ", "")

    def configured(self) -> bool:
        return bool(self.user and self.password)

    # ── send ──

    def send(self, msg: EmailMessage) -> str:
        """Send one message; returns its Message-ID. Runs in a worker thread."""
        if self.dry_run:
            out = data_dir() / "outbox_dryrun"
            out.mkdir(parents=True, exist_ok=True)
            name = re.sub(r"[^\w.-]", "_", msg["Message-ID"].strip("<>"))
            (out / f"{name}.eml").write_bytes(bytes(msg))
            return msg["Message-ID"]
        if not self.configured():
            raise MailError("SMTP_USER / SMTP_PASSWORD are not set in .env")
        try:
            with smtplib.SMTP(self.cfg.mail.smtp_host, self.cfg.mail.smtp_port, timeout=30) as smtp:
                smtp.starttls()
                smtp.login(self.user, self.password)
                refused = smtp.send_message(msg)
        except smtplib.SMTPRecipientsRefused as e:
            raise MailError(f"recipient refused: {e.recipients}")
        except smtplib.SMTPAuthenticationError:
            raise MailError("the mail server rejected the login — check SMTP_USER and the app password")
        except (smtplib.SMTPException, OSError) as e:
            raise MailError(f"SMTP error: {e}")
        if refused:
            raise MailError(f"recipient refused: {refused}")
        return msg["Message-ID"]

    # ── read ──

    def _imap(self) -> imaplib.IMAP4_SSL:
        if not (self.imap_user and self.imap_password):
            raise MailError("no IMAP credentials (SMTP_USER / SMTP_PASSWORD) in .env")
        conn = imaplib.IMAP4_SSL(self.cfg.mail.imap_host, self.cfg.mail.imap_port, timeout=30)
        try:
            conn.login(self.imap_user, self.imap_password)
        except imaplib.IMAP4.error:
            raise MailError("the IMAP server rejected the login — check the app password")
        conn.select("INBOX", readonly=True)
        return conn

    def fetch_since(self, last_uid: int | None, uidvalidity: str | None, limit: int = 200):
        """Returns (messages, new_last_uid, uidvalidity). On first run it only records where the
        inbox currently ends, so older mail is never read."""
        conn = self._imap()
        try:
            status = conn.status("INBOX", "(UIDVALIDITY UIDNEXT)")[1][0].decode()
            current_validity = re.search(r"UIDVALIDITY (\d+)", status).group(1)
            uidnext = int(re.search(r"UIDNEXT (\d+)", status).group(1))
            if last_uid is None or uidvalidity != current_validity:
                return [], uidnext - 1, current_validity
            typ, data = conn.uid("search", None, f"UID {last_uid + 1}:*")
            uids = [int(u) for u in (data[0] or b"").split() if int(u) > last_uid][:limit]
            messages = []
            for uid in uids:
                typ, parts = conn.uid("fetch", str(uid), "(BODY.PEEK[])")
                raw = next((p[1] for p in parts if isinstance(p, tuple)), None)
                if raw:
                    messages.append((uid, email.message_from_bytes(raw, policy=default_policy)))
            new_last = max(uids) if uids else last_uid
            return messages, new_last, current_validity
        finally:
            try:
                conn.logout()
            except Exception:
                pass

    def check_login(self) -> None:
        if not self.dry_run:
            with smtplib.SMTP(self.cfg.mail.smtp_host, self.cfg.mail.smtp_port, timeout=30) as smtp:
                smtp.starttls()
                smtp.login(self.user, self.password)
        self._imap().logout()


@dataclass
class Inbound:
    uid: int
    message_id: str
    in_reply_to: str
    references: list
    from_email: str
    subject: str
    body: str
    date: str
    is_bounce: bool
    is_auto: bool
    bounced_ids: list
    bounced_rcpt: str


def plain_text(msg) -> str:
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    text = part.get_content()
    if part.get_content_type() == "text/html":
        text = re.sub(r"<[^>]+>", " ", text)
    # Drop the quoted thread below the reply, so the model reads what they wrote, not what we did.
    text = re.split(r"\n(?:On .{5,200}wrote:|-{2,} ?Original Message|From: .*\nSent: )", text, maxsplit=1)[0]
    lines = [ln for ln in text.splitlines() if not ln.lstrip().startswith(">")]
    return "\n".join(lines).strip()


def parse(uid: int, msg) -> Inbound:
    from_email = parseaddr(msg.get("From", ""))[1].lower()
    subject = str(msg.get("Subject", ""))
    is_bounce = bool(BOUNCE_SENDERS.search(msg.get("From", ""))) or msg.get_content_type() == "multipart/report"
    auto_header = (msg.get("Auto-Submitted", "no").lower() != "no") or bool(msg.get("X-Autoreply")) \
        or msg.get("Precedence", "").lower() in ("auto_reply", "bulk", "junk")
    is_auto = not is_bounce and (auto_header or bool(AUTO_SUBJECT.search(subject)))
    bounced_ids, bounced_rcpt = [], ""
    if is_bounce:
        raw = msg.as_string()
        bounced_ids = MSGID_RE.findall(raw)
        m = re.search(r"(?:Final|Original)-Recipient:\s*rfc822;\s*([^\s;]+)", raw, re.I)
        bounced_rcpt = m.group(1).lower() if m else ""
    return Inbound(
        uid=uid,
        message_id=str(msg.get("Message-ID", "")).strip(),
        in_reply_to=str(msg.get("In-Reply-To", "")).strip(),
        references=MSGID_RE.findall(str(msg.get("References", ""))),
        from_email=from_email,
        subject=subject,
        body=plain_text(msg) if not is_bounce else "",
        date=str(msg.get("Date", "")),
        is_bounce=is_bounce,
        is_auto=is_auto,
        bounced_ids=bounced_ids,
        bounced_rcpt=bounced_rcpt,
    )
