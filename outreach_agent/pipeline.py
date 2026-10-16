"""Discover → research → write. Each stage reads and writes the database, so any stage can be
re-run on its own and nothing is lost if the process stops halfway."""

import itertools
import logging
import random
import re
from contextlib import contextmanager
from dataclasses import dataclass, field

from . import gate, web
from .countries import normalize_country
from .brain import Brain
from .config import PROMPTS, Config, env
from .db import DB, iso, utcnow

log = logging.getLogger("outreach.pipeline")

# Domains that are platforms, not prospects — a lead pointing here was never resolved to a company.
PLATFORMS = {
    "kickstarter.com", "indiegogo.com", "crowdsupply.com", "linkedin.com", "facebook.com",
    "instagram.com", "x.com", "twitter.com", "reddit.com", "producthunt.com", "crunchbase.com",
    "amazon.com", "etsy.com", "shopify.com", "medium.com", "youtube.com", "github.com",
    "apple.com", "play.google.com", "google.com", "indeed.com", "glassdoor.com", "wellfound.com",
    "ycombinator.com", "substack.com", "notion.site", "wix.com", "squarespace.com", "tiktok.com",
}
MIN_FIT = 5


def _string_props(*names):
    return {n: {"type": "string"} for n in names}


DISCOVER_SCHEMA = {
    "type": "object",
    "properties": {"companies": {"type": "array", "items": {
        "type": "object",
        "properties": _string_props("name", "domain", "country", "why_fit", "evidence", "evidence_url"),
        "required": ["name", "domain", "country", "why_fit", "evidence", "evidence_url"],
        "additionalProperties": False,
    }}},
    "required": ["companies"],
    "additionalProperties": False,
}
RESEARCH_SCHEMA = {
    "type": "object",
    "properties": {
        "qualified": {"type": "boolean"},
        "fit_score": {"type": "integer"},
        **_string_props("reason", "observation", "pitch_angle", "summary", "decision_maker", "dm_title", "country"),
    },
    "required": ["qualified", "fit_score", "reason", "observation", "pitch_angle", "summary",
                 "decision_maker", "dm_title", "country"],
    "additionalProperties": False,
}
WRITE_SCHEMA = {
    "type": "object",
    "properties": _string_props("subject", "email1", "followup1", "followup2"),
    "required": ["subject", "email1", "followup1", "followup2"],
    "additionalProperties": False,
}
REPLY_SCHEMA = {
    "type": "object",
    "properties": {
        "classification": {"type": "string",
                           "enum": ["interested", "question", "referral", "not_interested", "other"]},
        "summary": {"type": "string"},
        "draft": {"type": "string"},
    },
    "required": ["classification", "summary", "draft"],
    "additionalProperties": False,
}


def read_profile(path) -> str:
    """A profile file's text without <!-- comments -->, which are notes for the person editing it."""
    if path is None or not path.exists():
        return ""
    return re.sub(r"<!--.*?-->", "", path.read_text(), flags=re.S).strip()


def _sections(path) -> dict:
    """Split a markdown file into {heading: body} on `## heading` lines."""
    parts = re.split(r"^## +(\S+)\s*$", read_profile(path), flags=re.M)
    return {parts[i].strip(): parts[i + 1].strip() for i in range(1, len(parts) - 1, 2)}


@dataclass
class Segment:
    """One kind of business to look for, from the profile's segments file."""
    key: str
    brief: str
    label: str = ""
    niches: list = field(default_factory=list)
    aliases: list = field(default_factory=list)


def _field_line(name: str, text: str) -> tuple[str, str]:
    """Pull a `Name: value` line out of a segment's text; returns (value, text without it)."""
    m = re.search(rf"^{name}:\s*(.*)$", text, re.M | re.I)
    if not m:
        return "", text
    return m.group(1).strip(), (text[:m.start()] + text[m.end():]).strip()


def load_segments(path) -> dict[str, Segment]:
    out = {}
    for key, text in _sections(path).items():
        label, text = _field_line("Label", text)
        niches, text = _field_line("Niches", text)
        aliases, text = _field_line("Aliases", text)
        out[key] = Segment(
            key=key, brief=text, label=label or key,
            niches=[n.strip() for n in niches.split(";") if n.strip()],
            aliases=[a.strip().lower() for a in aliases.split(",") if a.strip()],
        )
    return out


def render(text: str, **values) -> str:
    """Fill {name} slots; anything not named is left alone, so JSON examples and scraped text are safe."""
    return re.sub(r"\{(\w+)\}", lambda m: str(values[m.group(1)]) if m.group(1) in values else m.group(0), text)


def _prompt(name: str) -> str:
    return (PROMPTS / f"{name}.md").read_text()


def first_name(full: str) -> str:
    return (full or "").strip().split(" ")[0]


def clean_name(name: str) -> str:
    """'Harbour Cuts (Harbour Grooming Ltd)' → 'Harbour Cuts': the name people call them by."""
    short = re.sub(r"\s*\([^)]*\)", "", name or "").strip(" -–—|,")
    return short or (name or "").strip()


class Activity:
    """What the pipeline is doing right now, for `status`. In memory: it only describes this process."""

    def __init__(self):
        self._items = {}
        self._ids = itertools.count()

    @contextmanager
    def track(self, text: str):
        key = next(self._ids)
        self._items[key] = (text, utcnow())
        try:
            yield
        finally:
            self._items.pop(key, None)

    def current(self) -> list:
        return sorted(self._items.values(), key=lambda item: item[1])


class Pipeline:
    def __init__(self, cfg: Config, db: DB, brain: Brain):
        self.cfg = cfg
        self.db = db
        self.brain = brain
        self.activity = Activity()

    def _fill(self, name: str, **values) -> str:
        """A prompt with the business and sender filled in, plus any task-specific values."""
        b, s = self.cfg.business, self.cfg.sender
        return render(_prompt(name), business=b.name, website=b.domain, sender=s.name,
                      sender_first=s.first_name, sender_title=s.title, **values)

    @property
    def system(self) -> str:
        facts = "\n\n".join(filter(None, (read_profile(p) for p in self.cfg.business.facts_paths())))
        return self._fill("system", facts=facts or f"{self.cfg.business.name}, {self.cfg.business.domain}")

    # Profile files are re-read on use, so edits take effect without a restart.

    @property
    def segments(self) -> dict[str, Segment]:
        return load_segments(self.cfg.business.path(self.cfg.business.segments_file))

    @property
    def examples(self) -> dict:
        return _sections(self.cfg.business.path(self.cfg.business.examples_file))

    def active_segments(self) -> list[str]:
        known = self.segments
        chosen = [s for s in self.cfg.targets.segments if s in known]
        return chosen or list(known)

    def resolve_segment(self, word: str) -> str | None:
        word = (word or "").strip().lower()
        for key, seg in self.segments.items():
            if word in (key.lower(), seg.label.lower(), *seg.aliases):
                return key
        return None

    def segment_label(self, key: str) -> str:
        seg = self.segments.get(key)
        return seg.label if seg else key

    # ── 1. discover ──

    async def discover(self, segment: str, count: int | None = None) -> int:
        if segment not in self.segments:
            raise ValueError(f"unknown segment {segment!r}; known: {', '.join(self.segments)}")
        count = count or self.cfg.targets.discovery_batch
        known = [r["domain"] for r in self.db.all("SELECT domain FROM companies ORDER BY id DESC LIMIT 400")]
        seg = self.segments[segment]
        options = seg.niches or [seg.label]
        last = self.db.get(f"last_focus:{segment}")
        focus = random.choice([o for o in options if o != last] or options)
        self.db.set(f"last_focus:{segment}", focus)
        prompt = self._fill(
            "discover", count=count, segment=segment, brief=seg.brief, focus=focus,
            countries=", ".join(self.cfg.target_countries()),
            exclude=", ".join(known) or "(none yet)",
        )
        with self.activity.track(f"🔎 Searching for {seg.label} prospects: {focus}"):
            result = await self.brain.json(task="discover", system=self.system, prompt=prompt,
                                           schema=DISCOVER_SCHEMA, web=True, effort="medium", timeout=900)
        added = 0
        for item in (result or {}).get("companies", []):
            if self._add_company(item, segment):
                added += 1
        self.db.log("discover", {"segment": segment, "focus": focus,
                                 "returned": len(result.get("companies", [])), "added": added})
        return added

    def _add_company(self, item: dict, segment: str) -> bool:
        domain = web.normalize_domain(item.get("domain", ""))
        country = normalize_country(item.get("country"))
        root = ".".join(domain.split(".")[-2:])
        if not domain or "." not in domain or root in PLATFORMS or domain in PLATFORMS:
            return False
        if country not in self.cfg.target_countries():
            return False
        if self.db.one("SELECT 1 FROM suppression WHERE value = ?", (domain,)):
            return False
        if self.db.one("SELECT 1 FROM companies WHERE domain = ?", (domain,)):
            return False
        now = iso()
        self.db.insert(
            "INSERT INTO companies(name, domain, segment, country, why_fit, evidence, evidence_url, source,"
            " created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (clean_name(item.get("name") or domain)[:120], domain, segment, country, item.get("why_fit", ""),
             item.get("evidence", ""), item.get("evidence_url", ""), "web", now, now),
        )
        return True

    # ── 2. research ──

    def _set_status(self, company_id: int, status: str, reason: str = "") -> None:
        self.db.execute("UPDATE companies SET status = ?, status_reason = ?, updated_at = ? WHERE id = ?",
                        (status, reason[:300], iso(), company_id))

    async def research(self, company_id: int) -> str:
        name = self.db.scalar("SELECT name FROM companies WHERE id = ?", (company_id,), "a prospect")
        with self.activity.track(f"🔬 Researching {name}"):
            return await self._research(company_id)

    async def _research(self, company_id: int) -> str:
        c = self.db.one("SELECT * FROM companies WHERE id = ?", (company_id,))
        snap = await web.snapshot(c["domain"])
        if not snap.pages:
            self._set_status(company_id, "disqualified", snap.error or "website unreachable")
            return "disqualified"
        if not snap.has_mx:
            self._set_status(company_id, "disqualified", "domain has no mail server")
            return "disqualified"

        blocked = tuple(self.cfg.targets.blocked_email_domains)
        if web.choose_address(snap.emails, blocked_domains=blocked) is None and not env("HUNTER_API_KEY"):
            # nothing we could write to: skip before spending a model call
            self._set_status(company_id, "no_email", "no published address found")
            return "no_email"

        seg = self.segments.get(c["segment"])
        prompt = self._fill(
            "research", segment=c["segment"], brief=seg.brief if seg else "", why_fit=c["why_fit"], evidence=c["evidence"],
            evidence_url=c["evidence_url"], site=snap.as_text(),
        )
        r = await self.brain.json(task="research", system=self.system, prompt=prompt,
                                  schema=RESEARCH_SCHEMA, effort="medium")
        country = normalize_country(r.get("country")) or c["country"] or ""
        self.db.execute(
            "UPDATE companies SET fit_score=?, observation=?, pitch_angle=?, summary=?, decision_maker=?,"
            " dm_title=?, country=?, updated_at=? WHERE id=?",
            (r["fit_score"], r["observation"], r["pitch_angle"], r["summary"], r["decision_maker"],
             r["dm_title"], country or c["country"], iso(), company_id),
        )
        if not r["qualified"]:
            self._set_status(company_id, "disqualified", r["reason"])
            return "disqualified"
        if r["fit_score"] < MIN_FIT:
            self._set_status(company_id, "disqualified", f"low fit ({r['fit_score']}/10): {r['reason']}")
            return "disqualified"
        if country and country not in self.cfg.target_countries():
            self._set_status(company_id, "disqualified", f"based in {country}, outside target countries")
            return "disqualified"

        pick = web.choose_address(snap.emails, r["decision_maker"], r["dm_title"], blocked)
        if pick is None:
            pick = await web.hunter_lookup(c["domain"], env("HUNTER_API_KEY"), r["decision_maker"])
        if pick is None or self.db.is_suppressed(pick.email):
            self._set_status(company_id, "no_email", "no published address found")
            return "no_email"
        if not pick.name and pick.kind == "person" and r["decision_maker"]:
            pick.name, pick.title = r["decision_maker"], r["dm_title"]
        self.db.execute(
            "INSERT OR IGNORE INTO contacts(company_id, name, title, email, kind, source, created_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (company_id, pick.name, pick.title, pick.email, pick.kind, pick.source, iso()),
        )
        self._set_status(company_id, "qualified")
        return "qualified"

    # ── 3. write ──

    def _write_prompt(self, c, contact) -> str:
        dm = c["decision_maker"] or ""
        if contact["kind"] == "person" and contact["name"]:
            greeting = f"Hi {first_name(contact['name'])},"
            recipient = f"{contact['name']}{', ' + contact['title'] if contact['title'] else ''} — writing to their own address"
        elif dm:
            greeting = f"Hi {first_name(dm)},"
            recipient = (f"the company's general inbox ({contact['email']}); {dm}"
                         f"{', ' + c['dm_title'] if c['dm_title'] else ''} is the person you're addressing")
        else:
            greeting = "Hi there,"
            recipient = f"the company's general inbox ({contact['email']}); no named person"
        seg = self.segments.get(c["segment"])
        examples = self.examples
        example = examples.get(c["segment"]) or examples.get("default") or "\n\n".join(examples.values())
        b = self.cfg.business
        link_rule = (f"include any link or web address other than {b.domain}, or mention {b.domain} more than once "
                     "in an email (mention it as plain text, only where it helps them act)"
                     if b.link_in_email else "include links or web addresses (your website is in the signature)")
        return self._fill(
            "write", link_rule=link_rule, brief=seg.brief if seg else "", company=c["name"], domain=c["domain"], country=c["country"], segment=c["segment"],
            recipient=recipient, observation=c["observation"], pitch_angle=c["pitch_angle"],
            summary=c["summary"], template=example, greeting=greeting,
        )

    async def _draft(self, prompt: str, company: str) -> tuple[dict, list[str]]:
        draft = await self.brain.json(task="write", system=self.system, prompt=prompt,
                                      schema=WRITE_SCHEMA, effort="medium")
        bodies = [gate.strip_signoff(draft[k]) for k in ("email1", "followup1", "followup2")]
        draft = {"subject": draft["subject"].strip(), "bodies": bodies}
        return draft, gate.check_sequence(draft["subject"], bodies, company,
                                          self.cfg.business.name, self.cfg.business.domain)

    async def write(self, company_id: int) -> int | None:
        name = self.db.scalar("SELECT name FROM companies WHERE id = ?", (company_id,), "a prospect")
        with self.activity.track(f"✍️ Writing the emails to {name}"):
            return await self._write(company_id)

    async def _write(self, company_id: int) -> int | None:
        c = self.db.one("SELECT * FROM companies WHERE id = ?", (company_id,))
        contact = self.db.one("SELECT * FROM contacts WHERE company_id = ? ORDER BY id LIMIT 1", (company_id,))
        if contact is None:
            return None
        prompt = self._write_prompt(c, contact)
        draft, problems = await self._draft(prompt, c["name"])
        if problems:
            retry = prompt + "\n\nYour previous draft failed these checks — fix every one:\n- " + "\n- ".join(problems)
            draft, problems = await self._draft(retry, c["name"])
        if problems:
            self._set_status(company_id, "error", "draft failed checks: " + "; ".join(problems))
            self.db.log("write_failed", {"company": c["domain"], "problems": problems})
            return None
        return self._store_sequence(c, contact, draft)

    def _store_sequence(self, c, contact, draft: dict) -> int:
        seq_id = self.db.insert(
            "INSERT INTO sequences(company_id, contact_id, segment, status, created_at) VALUES(?,?,?,?,?)",
            (c["id"], contact["id"], c["segment"], "pending", iso()),
        )
        for step, body in enumerate(draft["bodies"], start=1):
            subject = draft["subject"] if step == 1 else f"Re: {draft['subject']}"
            self.db.insert("INSERT INTO messages(sequence_id, step, subject, body, status) VALUES(?,?,?,?,?)",
                           (seq_id, step, subject, body, "draft"))
        self._set_status(c["id"], "drafted")
        return seq_id

    async def rewrite(self, seq_id: int, instruction: str) -> list[str]:
        seq = self.db.one("SELECT * FROM sequences WHERE id = ?", (seq_id,))
        c = self.db.one("SELECT * FROM companies WHERE id = ?", (seq["company_id"],))
        contact = self.db.one("SELECT * FROM contacts WHERE id = ?", (seq["contact_id"],))
        current = self.db.all("SELECT step, subject, body FROM messages WHERE sequence_id = ? ORDER BY step", (seq_id,))
        shown = "\n\n".join(f"[email {m['step']}] subject: {m['subject']}\n{m['body']}" for m in current)
        prompt = (self._write_prompt(c, contact)
                  + f"\n\nThe current draft:\n<draft>\n{shown}\n</draft>\n\n"
                  + f"{self.cfg.sender.first_name} wants this changed: {instruction}\nRewrite the sequence accordingly.")
        draft, problems = await self._draft(prompt, c["name"])
        for step, body in enumerate(draft["bodies"], start=1):
            subject = draft["subject"] if step == 1 else f"Re: {draft['subject']}"
            self.db.execute("UPDATE messages SET subject = ?, body = ? WHERE sequence_id = ? AND step = ?",
                            (subject, body, seq_id, step))
        return problems

    # ── replies ──

    async def classify_reply(self, inbound) -> dict:
        sent = self.db.all(
            "SELECT m.step, m.subject, m.body FROM messages m WHERE m.sequence_id = ? AND m.status IN ('sent','bounced')"
            " ORDER BY m.step", (inbound["sequence_id"],)) if inbound["sequence_id"] else []
        shown = "\n\n".join(f"[email {m['step']}] {m['subject']}\n{m['body']}" for m in sent) or "(not found)"
        prompt = self._fill("reply", sent=shown, from_email=inbound["from_email"],
                                         subject=inbound["subject"], body=(inbound["body"] or "")[:6000])
        return await self.brain.json(task="reply", system=self.system, prompt=prompt,
                                     schema=REPLY_SCHEMA, effort="medium")
