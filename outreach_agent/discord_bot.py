"""Discord: where drafts arrive for approval and replies arrive for answering.

Every draft is one card with Approve / Edit / Rewrite / Reject / Never contact. Buttons keep
working after a restart (their IDs encode the sequence), and only DISCORD_OWNER_ID can press them.
"""

import asyncio
import logging
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import discord
from discord import app_commands, ui

from .config import env
from .db import iso
from .engine import Engine, Notifier

log = logging.getLogger("outreach.discord")

NAVY = 0x16283A
MERIDIAN = 0x2E5E86
GREEN = 0x2E7D4F
GREY = 0x8A9AA8
NEEDLE = 0xC8402E
HELP = (
    "**Commands** — type any of these here, or use the / versions:\n"
    "`status` — where things stand right now\n"
    "`queue` — drafts waiting for you\n"
    "`approve all` — approve every waiting draft\n"
    "`stop` · `start` — stop or start all work: searching, drafting and sending\n"
    "`pause` · `resume` — stop or restart sending only (drafting continues)\n"
    "`restart` — restart the whole program\n"
    "`cap 30` — set today's email limit\n"
    "`find {example} 5` — search for prospects now ({kinds})\n"
    "`redraft` — rewrite every waiting draft in the current style\n"
    "`brain api` · `brain subscription` — write with your API credits or your Claude plan\n"
    "`credits 89.94` — tell me your real API balance from the Console, so `status` stays accurate\n"
    "`help` — this list"
)
TOPIC = ("Commands: status · start · stop · queue · approve all · pause · resume · cap 30 · find {example} 5 · "
         "redraft · restart · brain api · credits · help. Type one here, or use the / menu.")


def help_text(engine: Engine) -> str:
    kinds = engine.pipeline.active_segments() or ["segment"]
    return HELP.format(example=kinds[0], kinds=", ".join(kinds))


def topic_text(engine: Engine) -> str:
    kinds = engine.pipeline.active_segments() or ["segment"]
    return TOPIC.format(example=kinds[0])


REPLY_BADGE = {"interested": "🟢 Interested", "question": "🟡 Question", "referral": "🔵 Referral",
               "not_interested": "⚪ Not interested", "other": "💬 Reply"}


class _noop:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ── rendering ──

def draft_embed(engine: Engine, seq_id: int) -> discord.Embed:
    db = engine.db
    seq = db.one("SELECT * FROM sequences WHERE id=?", (seq_id,))
    c = db.one("SELECT * FROM companies WHERE id=?", (seq["company_id"],))
    ct = db.one("SELECT * FROM contacts WHERE id=?", (seq["contact_id"],))
    msgs = db.all("SELECT * FROM messages WHERE sequence_id=? ORDER BY step", (seq_id,))

    state = {
        "pending": ("", MERIDIAN),
        "active": ("✅ **Approved** — sends in their working hours, follow-ups on schedule.\n\n", GREEN),
        "completed": ("✅ **Sequence finished.**\n\n", GREEN),
        "rejected": ("✖️ **Rejected.**\n\n", GREY),
        "stopped": (f"⏹ **Stopped** ({seq['stop_reason']}).\n\n", GREY),
    }.get(seq["status"], ("", NAVY))

    if seq["status"] in ("active", "completed", "stopped"):
        state = (state[0] + delivery_lines(engine, msgs, c["country"]) + "\n\n", state[1])
    first = msgs[0] if msgs else None
    body = f"**Subject:** {first['subject']}\n\n{first['body']}" if first else "(no draft)"
    embed = discord.Embed(
        title=clip(f"{c['name']} — {engine.pipeline.segment_label(c['segment'])}", 250),
        url=f"https://{c['domain']}", description=clip(state[0] + body, 2200), colour=state[1],
    )

    # who and where
    where = "found on their website" if ct["source"] == "website" else f"via {ct['source']}"
    kind = "named person" if ct["kind"] == "person" else "shared inbox"
    embed.add_field(name="Website", value=f"[{c['domain']}](https://{c['domain']})", inline=True)
    embed.add_field(name="Sends to", value=clip(f"{ct['email']}\n{kind}, {where}", 200), inline=True)
    contact = c["decision_maker"] or ct["name"] or "not named on their site"
    title = c["dm_title"] or ct["title"]
    embed.add_field(name="Decision maker", value=clip(contact + (f"\n{title}" if title else ""), 200), inline=True)
    embed.add_field(name="Country", value=c["country"] or "?", inline=True)
    embed.add_field(name="Fit", value=f"{c['fit_score'] or '?'}/10", inline=True)
    embed.add_field(name="Found", value=(c["created_at"] or "")[:10], inline=True)

    # why them
    if c["summary"]:
        embed.add_field(name="What they do", value=clip(c["summary"], 350), inline=False)
    embed.add_field(name="Why them", value=clip(c["why_fit"] or "—", 350), inline=False)
    if c["observation"]:
        embed.add_field(name="What was noticed", value=clip(c["observation"], 350), inline=False)
    if c["evidence"]:
        source = f"\n[source]({c['evidence_url']})" if (c["evidence_url"] or "").startswith("http") else ""
        embed.add_field(name="Evidence", value=clip(c["evidence"], 300) + clip(source, 200), inline=False)

    # the follow-ups
    days = engine.cfg.schedule.followup_days
    for m in msgs[1:]:
        when = f"day {days[m['step'] - 2]}" if m["step"] - 2 < len(days) else ""
        status = "" if m["status"] in ("draft", "approved") else f" · {m['status']}"
        embed.add_field(name=f"Follow-up {m['step'] - 1} · {when}{status}", value=clip(m["body"], 500), inline=False)
    embed.set_footer(text=f"#{seq_id} · your signature, address and an unsubscribe line are added when it sends")
    return embed


def _when(engine: Engine, dt: datetime, country: str) -> str:
    """'Mon 09:00 their time (13:00 yours)'."""
    from .engine import COUNTRY_TZ
    theirs = dt.astimezone(ZoneInfo(COUNTRY_TZ.get(country or "", "UTC")))
    yours = dt.astimezone(ZoneInfo(engine.cfg.schedule.timezone))
    same_day = theirs.date() == yours.date()
    return f"{theirs:%a %H:%M} their time ({yours:%H:%M}" + ("" if same_day else f" {yours:%a}") + " yours)"


def delivery_lines(engine: Engine, msgs, country: str) -> str:
    from .db import parse_iso, utcnow
    labels = {1: "Email", 2: "Follow-up 1", 3: "Follow-up 2"}
    tz = ZoneInfo(engine.cfg.schedule.timezone)
    lines = []
    for m in msgs:
        name = labels.get(m["step"], f"Email {m['step']}")
        if m["status"] == "sent":
            lines.append(f"✉️ {name} sent {parse_iso(m['sent_at']).astimezone(tz):%a %H:%M} your time")
        elif m["status"] == "bounced":
            lines.append(f"↩️ {name} bounced")
        elif m["status"] == "cancelled":
            lines.append(f"⏹ {name} cancelled")
        elif m["status"] == "approved" and m["due_at"]:
            due = max(parse_iso(m["due_at"]), utcnow())
            go = engine.next_window(country, due)
            when = "now, within a few minutes" if go <= utcnow() else _when(engine, go, country)
            lines.append(f"⏳ {name} queued: goes {when}")
        elif m["status"] == "approved":
            lines.append(f"⏳ {name} waits for the first email")
        elif m["status"] == "failed":
            lines.append(f"⚠️ {name} failed: {clip(m['error'], 120)}")
    return "\n".join(lines)


def draft_view(seq_id: int, status: str) -> ui.View | None:
    if status != "pending":
        return None
    view = ui.View(timeout=None)
    for action in ("approve", "edit", "rewrite", "reject", "block"):
        view.add_item(DraftButton(action, seq_id))
    return view


def reply_embed(engine: Engine, inbound_id: int, draft_id: int | None) -> discord.Embed:
    db = engine.db
    inc = db.one("SELECT * FROM inbound WHERE id=?", (inbound_id,))
    c = db.one("SELECT c.* FROM companies c JOIN sequences s ON s.company_id=c.id WHERE s.id=?",
               (inc["sequence_id"],))
    draft = db.one("SELECT * FROM reply_drafts WHERE id=?", (draft_id,)) if draft_id else None
    badge = REPLY_BADGE.get(inc["classification"] or "other", "💬 Reply")
    text = (f"**{badge}** — {clip(inc['summary'], 300)}\n\n**From:** {inc['from_email']}\n"
            f"**Subject:** {clip(inc['subject'], 200)}\n\n>>> {clip(inc['body'], 1500)}")
    embed = discord.Embed(title=clip(f"Reply from {c['name'] if c else inc['from_email']}", 250),
                          description=clip(text, 4000), colour=NEEDLE)
    if draft:
        label = {"pending": "Suggested reply", "sent": "✅ Sent", "dismissed": "Dismissed"}.get(draft["status"], "Reply")
        embed.add_field(name=label, value=clip(draft["body"], 1024), inline=False)
    return embed


def reply_view(draft_id: int | None, status: str = "pending") -> ui.View | None:
    if not draft_id or status != "pending":
        return None
    view = ui.View(timeout=None)
    for action in ("send", "edit", "dismiss"):
        view.add_item(ReplyButton(action, draft_id))
    return view


# ── buttons and forms ──

def is_owner(interaction: discord.Interaction) -> bool:
    return interaction.user.id == interaction.client.owner_id


def in_my_channel(interaction: discord.Interaction) -> bool:
    """Several businesses can share one Discord bot, each in its own channel. Every running copy
    receives every interaction, so each answers only in its own channel and leaves the rest alone."""
    return interaction.channel_id == interaction.client.channel_id


async def refuse(interaction: discord.Interaction) -> None:
    await interaction.response.send_message("Only the owner can do that.", ephemeral=True)


DRAFT_BUTTONS = {
    "approve": ("Approve", discord.ButtonStyle.success, "✅"),
    "edit": ("Edit", discord.ButtonStyle.secondary, "✏️"),
    "rewrite": ("Rewrite", discord.ButtonStyle.secondary, "🔄"),
    "reject": ("Reject", discord.ButtonStyle.secondary, "✖️"),
    "block": ("Never contact", discord.ButtonStyle.danger, "🚫"),
}


class DraftButton(ui.DynamicItem[ui.Button], template=r"draft:(?P<action>[a-z]+):(?P<id>\d+)"):
    def __init__(self, action: str, seq_id: int):
        label, style, emoji = DRAFT_BUTTONS[action]
        super().__init__(ui.Button(label=label, style=style, emoji=emoji, custom_id=f"draft:{action}:{seq_id}"))
        self.action = action
        self.seq_id = seq_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["action"], int(match["id"]))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return in_my_channel(interaction)

    async def callback(self, interaction: discord.Interaction):
        if not is_owner(interaction):
            return await refuse(interaction)
        engine: Engine = interaction.client.engine
        status = engine.db.scalar("SELECT status FROM sequences WHERE id=?", (self.seq_id,))
        if status != "pending" and self.action != "edit":
            return await interaction.response.edit_message(embed=draft_embed(engine, self.seq_id), view=None)

        if self.action == "approve":
            problems = engine.approve(self.seq_id)
            if problems:
                return await interaction.response.send_message("Can't approve yet: " + "; ".join(problems),
                                                               ephemeral=True)
            await interaction.response.edit_message(embed=draft_embed(engine, self.seq_id), view=None)
        elif self.action in ("reject", "block"):
            engine.reject(self.seq_id, block=self.action == "block")
            await interaction.response.edit_message(embed=draft_embed(engine, self.seq_id), view=None)
        elif self.action == "edit":
            await interaction.response.send_modal(EditModal(engine, self.seq_id))
        elif self.action == "rewrite":
            await interaction.response.send_modal(RewriteModal(self.seq_id))


class EditModal(ui.Modal, title="Edit this sequence"):
    def __init__(self, engine: Engine, seq_id: int):
        super().__init__(timeout=1800)
        self.seq_id = seq_id
        msgs = engine.db.all("SELECT * FROM messages WHERE sequence_id=? ORDER BY step", (seq_id,))
        self.subject = ui.TextInput(label="Subject", default=msgs[0]["subject"], max_length=120)
        self.email1 = ui.TextInput(label="First email", style=discord.TextStyle.paragraph,
                                   default=msgs[0]["body"], max_length=3000)
        self.f1 = ui.TextInput(label="Follow-up 1", style=discord.TextStyle.paragraph,
                               default=msgs[1]["body"] if len(msgs) > 1 else "", max_length=1500)
        self.f2 = ui.TextInput(label="Follow-up 2", style=discord.TextStyle.paragraph,
                               default=msgs[2]["body"] if len(msgs) > 2 else "", max_length=1500)
        for item in (self.subject, self.email1, self.f1, self.f2):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction):
        engine: Engine = interaction.client.engine
        warnings = engine.save_edit(self.seq_id, self.subject.value,
                                    [self.email1.value, self.f1.value, self.f2.value])
        status = engine.db.scalar("SELECT status FROM sequences WHERE id=?", (self.seq_id,))
        await interaction.response.edit_message(embed=draft_embed(engine, self.seq_id),
                                                view=draft_view(self.seq_id, status))
        note = "Saved." + (" Heads-up, it would fail the usual checks: " + "; ".join(warnings) +
                           ". You wrote it, so you can still approve." if warnings else "")
        await interaction.followup.send(note, ephemeral=True)


class RewriteModal(ui.Modal, title="Rewrite with an instruction"):
    instruction = ui.TextInput(label="What should change?", style=discord.TextStyle.paragraph,
                               placeholder="e.g. shorter, warmer, lead with a different detail",
                               max_length=600)

    def __init__(self, seq_id: int):
        super().__init__(timeout=1800)
        self.seq_id = seq_id

    async def on_submit(self, interaction: discord.Interaction):
        engine: Engine = interaction.client.engine
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            problems = await engine.pipeline.rewrite(self.seq_id, self.instruction.value)
        except Exception as e:
            return await interaction.followup.send(f"Rewrite failed: {e}", ephemeral=True)
        await interaction.client.refresh_draft(self.seq_id)
        msg = "Rewritten — the card is updated."
        if problems:
            msg += " It still trips these checks: " + "; ".join(problems)
        await interaction.followup.send(msg, ephemeral=True)


class ReplyButton(ui.DynamicItem[ui.Button], template=r"reply:(?P<action>[a-z]+):(?P<id>\d+)"):
    STYLES = {"send": ("Send reply", discord.ButtonStyle.success, "📨"),
              "edit": ("Edit", discord.ButtonStyle.secondary, "✏️"),
              "dismiss": ("I'll handle it", discord.ButtonStyle.secondary, "👋")}

    def __init__(self, action: str, draft_id: int):
        label, style, emoji = self.STYLES[action]
        super().__init__(ui.Button(label=label, style=style, emoji=emoji, custom_id=f"reply:{action}:{draft_id}"))
        self.action = action
        self.draft_id = draft_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["action"], int(match["id"]))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return in_my_channel(interaction)

    async def callback(self, interaction: discord.Interaction):
        if not is_owner(interaction):
            return await refuse(interaction)
        engine: Engine = interaction.client.engine
        d = engine.db.one("SELECT * FROM reply_drafts WHERE id=?", (self.draft_id,))
        if d["status"] != "pending":
            return await interaction.response.edit_message(view=None)
        if self.action == "edit":
            return await interaction.response.send_modal(ReplyEditModal(self.draft_id, d["body"]))
        if self.action == "dismiss":
            engine.db.execute("UPDATE reply_drafts SET status='dismissed' WHERE id=?", (self.draft_id,))
            return await interaction.response.edit_message(
                embed=reply_embed(engine, d["inbound_id"], self.draft_id), view=None)
        await interaction.response.defer()
        try:
            await engine.send_reply(self.draft_id)
        except Exception as e:
            return await interaction.followup.send(f"Couldn't send: {e}", ephemeral=True)
        await interaction.message.edit(embed=reply_embed(engine, d["inbound_id"], self.draft_id), view=None)


class ReplyEditModal(ui.Modal, title="Edit your reply"):
    def __init__(self, draft_id: int, body: str):
        super().__init__(timeout=1800)
        self.draft_id = draft_id
        self.body = ui.TextInput(label="Reply", style=discord.TextStyle.paragraph, default=body, max_length=3000)
        self.add_item(self.body)

    async def on_submit(self, interaction: discord.Interaction):
        engine: Engine = interaction.client.engine
        engine.db.execute("UPDATE reply_drafts SET body=? WHERE id=?", (self.body.value.strip(), self.draft_id))
        inbound_id = engine.db.scalar("SELECT inbound_id FROM reply_drafts WHERE id=?", (self.draft_id,))
        await interaction.response.edit_message(embed=reply_embed(engine, inbound_id, self.draft_id),
                                                view=reply_view(self.draft_id))


# ── the bot ──

class DiscordNotifier(Notifier):
    def __init__(self, bot: "OutreachBot"):
        self.bot = bot

    async def draft_ready(self, seq_id):
        await self.bot.post_draft(seq_id)

    async def sequence_changed(self, seq_id):
        await self.bot.refresh_draft(seq_id)

    async def drafts_batch(self, count):
        await self.bot.say(f"<@{self.bot.owner_id}> {count} new draft{'s' if count != 1 else ''} waiting for you.")

    async def reply_received(self, inbound_id, draft_id):
        engine = self.bot.engine
        channel = await self.bot.channel()
        msg = await channel.send(content=f"<@{self.bot.owner_id}>", embed=reply_embed(engine, inbound_id, draft_id),
                                 view=reply_view(draft_id))
        if draft_id:
            engine.db.execute("UPDATE reply_drafts SET discord_message_id=? WHERE id=?", (str(msg.id), draft_id))

    async def alert(self, text):
        await self.bot.say(f"<@{self.bot.owner_id}> {text}")


class OutreachBot(discord.Client):
    def __init__(self, engine: Engine, read_messages: bool = True):
        intents = discord.Intents.default()
        # Reading plain-text commands needs Discord's "Message Content Intent" switched on for the bot.
        # Without it the bot still works: slash commands and @mentions need no special permission.
        intents.message_content = read_messages
        super().__init__(intents=intents)
        self.read_messages = read_messages
        self.engine = engine
        self.channel_id = int(env("DISCORD_CHANNEL_ID") or 0)
        self.owner_id = int(env("DISCORD_OWNER_ID") or 0)
        self.tree = app_commands.CommandTree(self)
        self.tree.interaction_check = self._tree_check
        self._started = False
        engine.notifier = DiscordNotifier(self)
        register_commands(self)

    async def setup_hook(self):
        self.add_dynamic_items(DraftButton, ReplyButton)

    async def channel(self) -> discord.TextChannel:
        return self.get_channel(self.channel_id) or await self.fetch_channel(self.channel_id)

    async def say(self, text: str) -> None:
        await (await self.channel()).send(text[:1990])

    async def post_draft(self, seq_id: int) -> None:
        status = self.engine.db.scalar("SELECT status FROM sequences WHERE id=?", (seq_id,))
        msg = await (await self.channel()).send(embed=draft_embed(self.engine, seq_id), view=draft_view(seq_id, status))
        self.engine.db.execute("UPDATE sequences SET discord_message_id=? WHERE id=?", (str(msg.id), seq_id))

    async def refresh_draft(self, seq_id: int) -> None:
        row = self.engine.db.one("SELECT status, discord_message_id FROM sequences WHERE id=?", (seq_id,))
        if not row or not row["discord_message_id"]:
            return
        try:
            msg = await (await self.channel()).fetch_message(int(row["discord_message_id"]))
            await msg.edit(embed=draft_embed(self.engine, seq_id), view=draft_view(seq_id, row["status"]))
        except discord.HTTPException:
            log.warning("couldn't refresh card for sequence %s", seq_id)

    async def on_ready(self):
        if self._started:
            return
        self._started = True
        channel = await self.channel()
        self.tree.copy_global_to(guild=channel.guild)
        await self.tree.sync(guild=channel.guild)
        unposted = self.engine.db.all("SELECT id FROM sequences WHERE status='pending' AND discord_message_id IS NULL")
        st = self.engine.status()
        note = "" if self.read_messages else (
            "\nPlain-text commands are off: turn on **Message Content Intent** for the bot in the Discord developer "
            "portal (Bot tab), then restart. Until then, use the / commands or @mention me.")
        await channel.send(
            f"🧭 {self.engine.cfg.business.name} outreach is online — {st['provider']} brain, {st['awaiting_approval']} draft(s) waiting, "
            f"daily cap {st['daily_cap']}."
            + ("  ⏹ **Stopped** — type `start` to begin working." if st["stopped"]
               else "  ⏸ Sending is paused." if st["paused"] else "")
            + "\nType `help` for commands." + note)
        await self.ensure_help(channel)
        for row in unposted:
            await self.post_draft(row["id"])
        # bring approved cards' delivery status up to date after a restart
        for row in self.engine.db.all("SELECT id FROM sequences WHERE status='active' AND discord_message_id IS NOT NULL"
                                      " ORDER BY id DESC LIMIT 50"):
            await self.refresh_draft(row["id"])
        asyncio.create_task(self.engine.heartbeat_loop())
        asyncio.create_task(self.engine.send_loop())
        asyncio.create_task(self.digest_loop())

    async def ensure_help(self, channel: discord.TextChannel) -> None:
        """Keep the command list visible: in the channel topic, and as a pinned message.
        Each needs a permission the bot may not have, so either can quietly fall back."""
        missing = []
        topic, help_ = topic_text(self.engine), help_text(self.engine)
        if channel.topic != topic:
            try:
                await channel.edit(topic=topic)
            except discord.Forbidden:
                missing.append("Manage Channels (to set the channel topic)")
        db = self.engine.db
        msg = None
        if db.get("help_message_id"):
            try:
                msg = await channel.fetch_message(int(db.get("help_message_id")))
                if msg.content != help_:
                    await msg.edit(content=help_)
            except discord.HTTPException:
                msg = None
        if msg is None:
            msg = await channel.send(help_)
            db.set("help_message_id", msg.id)
        if not msg.pinned:
            try:
                await msg.pin()
            except discord.Forbidden:
                missing.append("Pin Messages (to pin this list)")
        if missing and db.get("help_permission_note") != ",".join(missing):
            db.set("help_permission_note", ",".join(missing))
            await channel.send("To keep the command list in view, give my role "
                               + " and ".join(missing) + " in Server Settings → Roles. Or pin the list above yourself.")

    # ── text commands ──

    async def _tree_check(self, interaction: discord.Interaction) -> bool:
        return in_my_channel(interaction)

    async def on_message(self, message: discord.Message):
        # Only the board channel: a bot shared by several businesses can't tell whose a DM is.
        if message.author.bot or message.author.id != self.owner_id or message.channel.id != self.channel_id:
            return
        text = re.sub(r"<@!?\d+>", " ", message.content or "").strip().lstrip("!/").strip().lower()
        if not text:
            return
        if not await self.run_text_command(message, text) and self.user in message.mentions:
            await message.reply("I didn't catch that. Type `help` for the list.")

    async def run_text_command(self, message: discord.Message, text: str) -> bool:
        words = text.split()
        cmd = words[0]
        if cmd in ("status", "stats"):
            async with message.channel.typing() if hasattr(message.channel, "typing") else _noop():
                embed = await status_embed(self.engine)
            await message.reply(embed=embed)
        elif cmd in ("help", "commands"):
            await message.reply(help_text(self.engine))
        elif cmd == "queue":
            await message.reply(self.queue_text(message.guild.id if message.guild else 0))
        elif cmd == "stop":
            self.engine.stop()
            await message.reply("⏹ Stopped: no searching, drafting or sending, so no credits are used. "
                                "I'm still reading replies. Type `start` to begin again.")
        elif cmd == "start":
            self.engine.start()
            await message.reply("▶️ Started. Searching, drafting and sending are back on.")
            asyncio.create_task(self.top_up_when_free())
        elif cmd == "restart":
            await message.reply("🔄 Restarting. I'll be back online in about a minute.")
            asyncio.create_task(self.restart())
        elif cmd == "pause":
            self.engine.pause("paused by you")
            await message.reply("⏸ Sending paused. Drafts keep arriving; nothing goes out.")
        elif cmd in ("resume", "unpause"):
            self.engine.resume()
            await message.reply("▶️ Sending resumed.")
        elif text.replace("_", " ") in ("approve all", "approveall"):
            await message.reply(await self.approve_all())
        elif cmd == "cap" and len(words) > 1 and words[1].isdigit():
            value = self.engine.set_cap(int(words[1]))
            await message.reply(f"Daily cap set to {value}.")
        elif cmd == "find":
            segment = self.engine.pipeline.resolve_segment(words[1] if len(words) > 1 else "")
            if not segment:
                kinds = self.engine.pipeline.active_segments()
                await message.reply("Which kind? " + ", ".join(f"`find {k} 5`" for k in kinds) + ".")
                return True
            count = min(int(words[2]), 25) if len(words) > 2 and words[2].isdigit() else 10
            label = self.engine.pipeline.segment_label(segment)
            await message.reply(f"🔎 Searching for {count} {label} prospects. This takes a few minutes.")
            asyncio.create_task(self.find(segment, count))
        elif cmd in ("credits", "balance") and len(words) > 1:
            await message.reply(self.set_credits(words[1]))
        elif cmd == "brain":
            await message.reply(self.switch_brain(words[1] if len(words) > 1 else ""))
        elif cmd == "redraft":
            n = await self.redraft()
            await message.reply(f"🔄 Rewriting {n} draft(s) in the current style. New cards will appear shortly."
                                if n else "Nothing waiting to redraft.")
        else:
            return False
        return True

    # ── actions shared by text and slash commands ──

    async def approve_all(self) -> str:
        ids = [r["id"] for r in self.engine.db.all("SELECT id FROM sequences WHERE status='pending'")]
        approved, skipped = 0, []
        for seq_id in ids:
            if self.engine.approve(seq_id):
                skipped.append(seq_id)
            else:
                approved += 1
                await self.refresh_draft(seq_id)
        note = f"✅ Approved {approved} draft(s)."
        if skipped:
            note += f" Skipped {len(skipped)} with problems: " + ", ".join(f"#{i}" for i in skipped)
        return note

    async def find(self, segment: str, count: int) -> None:
        try:
            added = await self.engine.pipeline.discover(segment, count)
            await self.say(f"Found {added} new {self.engine.pipeline.segment_label(segment)} prospect(s). Researching and drafting next.")
            await self.top_up_when_free()
        except Exception as e:
            await self.say(f"Search failed: {e}")

    def queue_text(self, guild_id: int) -> str:
        rows = self.engine.db.all(
            "SELECT s.id, s.discord_message_id, c.name FROM sequences s JOIN companies c ON c.id=s.company_id"
            " WHERE s.status='pending' ORDER BY s.id")
        if not rows:
            return "Nothing waiting."
        lines = [f"#{r['id']} [{r['name']}](https://discord.com/channels/{guild_id}/{self.channel_id}/{r['discord_message_id']})"
                 if r["discord_message_id"] and guild_id else f"#{r['id']} {r['name']}" for r in rows]
        return "\n".join(lines)[:1990]

    async def redraft(self) -> int:
        """Retire every waiting draft and write it again with the current prompts."""
        db = self.engine.db
        rows = db.all("SELECT id, company_id, discord_message_id FROM sequences WHERE status='pending'")
        channel = await self.channel()
        for r in rows:
            db.execute("UPDATE sequences SET status='rejected', stop_reason='redrafted', decided_at=? WHERE id=?",
                       (iso(), r["id"]))
            db.execute("UPDATE messages SET status='cancelled' WHERE sequence_id=?", (r["id"],))
            db.execute("UPDATE companies SET status='qualified', updated_at=? WHERE id=? AND status='drafted'",
                       (iso(), r["company_id"]))
            if r["discord_message_id"]:
                try:
                    await (await channel.fetch_message(int(r["discord_message_id"]))).delete()
                except discord.HTTPException:
                    pass
        if rows:
            asyncio.create_task(self.top_up_when_free())
        return len(rows)

    def set_credits(self, value: str) -> str:
        try:
            amount = float(value.replace("$", "").replace(",", ""))
        except ValueError:
            return "Give the balance as a number, e.g. `credits 89.94`."
        self.engine.db.set("credit_balance", f"{amount}|{iso()}")
        return (f"💳 Noted: ${amount:.2f} of API credits right now. `status` will count down from this as "
                "outreach spends.")

    def switch_brain(self, choice: str) -> str:
        brain = self.engine.brain
        label = {"cli": "your Claude subscription", "api": "your Anthropic API credits"}
        target = {"api": "api", "credits": "api", "subscription": "cli", "sub": "cli", "cli": "cli",
                  "plan": "cli", "max": "cli"}.get(choice)
        if not target:
            return f"Writing with {label[brain.provider]}. Switch with `brain api` or `brain subscription`."
        try:
            brain.set_provider(target)
        except Exception as e:
            return f"Couldn't switch: {e}"
        return f"🧠 Now writing with {label[target]}. No restart needed."

    async def restart(self) -> None:
        """Exit cleanly; the background service starts a fresh copy (launchd KeepAlive)."""
        self.engine.db.log("restart", "requested from Discord")
        await asyncio.sleep(1)
        await self.close()

    async def top_up_when_free(self) -> None:
        while self.engine.pipeline_lock.locked():
            await asyncio.sleep(15)
        await self.engine.top_up()

    async def digest_loop(self):
        tz = ZoneInfo(self.engine.cfg.schedule.timezone)
        while True:
            now = datetime.now(tz)
            today = now.date().isoformat()
            if now.hour >= self.engine.cfg.schedule.digest_hour and self.engine.db.get("digest_date") != today:
                self.engine.db.set("digest_date", today)
                try:
                    await (await self.channel()).send(embed=await status_embed(self.engine, "Morning summary"))
                except discord.HTTPException:
                    log.exception("digest failed")
            await asyncio.sleep(300)


async def usage_lines(engine: Engine) -> list[str]:
    brain = engine.brain
    in_use = " ← in use"
    lines = []
    plan = await brain.subscription_usage()
    if plan:
        parts = []
        if "session" in plan:
            pct, resets = plan["session"]
            parts.append(f"{pct}% of this 5-hour session" + (f" (resets {resets})" if resets else ""))
        if "week" in plan:
            pct, resets = plan["week"]
            parts.append(f"{pct}% of the week" + (f" (resets {resets})" if resets else ""))
        lines.append("**Claude plan:** " + " · ".join(parts) + (in_use if brain.provider == "cli" else ""))
    else:
        lines.append("**Claude plan:** usage unavailable right now" + (in_use if brain.provider == "cli" else ""))
    from .db import parse_iso, utcnow
    now = utcnow()
    today = brain.api_spend(engine.db.start_of_utc_day())
    month = brain.api_spend(now.replace(day=1, hour=0, minute=0, second=0, microsecond=0))
    api = f"**API credits:** outreach spent ${today:.2f} today · ${month:.2f} this month"
    budget = engine.cfg.brain.api_budget_usd
    known = engine.db.get("credit_balance")
    if known:
        balance, at = known.split("|")
        since = parse_iso(at)
        left = float(balance) - brain.api_spend(since)
        yours = since.astimezone(ZoneInfo(engine.cfg.schedule.timezone))
        api += f" · about **${max(left, 0):.2f} left** (your ${float(balance):.2f} at {yours:%b %d %H:%M}, minus outreach since)"
    elif budget:
        api += f" · about ${max(budget - month, 0):.2f} of your ${budget:.0f} left"
    api += (in_use if brain.provider == "api" else "")
    lines.append(api)
    lines.append("-# API figures count outreach's own calls. Your exact balance: "
                 "[Anthropic Console](https://console.anthropic.com/settings/billing)")
    return lines


def _ago(start: datetime) -> str:
    from .db import utcnow
    seconds = int((utcnow() - start).total_seconds())
    return f"{seconds}s" if seconds < 90 else f"{seconds // 60} min"


def activity_lines(engine: Engine) -> list[str]:
    import json as _json
    from datetime import timedelta
    from .db import parse_iso, utcnow
    lines = ["**Working on now:**"]
    current = engine.pipeline.activity.current()
    for text, started in current:
        lines.append(f"{text} ({_ago(started)})")
    if not current:
        nxt = ""
        if engine.last_heartbeat:
            due = engine.last_heartbeat + timedelta(minutes=engine.cfg.schedule.heartbeat_minutes)
            mins = max(0, int((due - utcnow()).total_seconds() // 60))
            nxt = f": next check in about {mins} min" if mins else ": checking again shortly"
        lines.append("Nothing running" + nxt)
    last = engine.db.one("SELECT ts, detail FROM events WHERE kind='discover' ORDER BY id DESC LIMIT 1")
    if last:
        d = _json.loads(last["detail"] or "{}")
        when = parse_iso(last["ts"]).astimezone(ZoneInfo(engine.cfg.schedule.timezone))
        focus = f" ({d['focus']})" if d.get("focus") else ""
        lines.append(f"-# Last search {when:%H:%M}{focus}: {d.get('added', 0)} new companies")
    nxt = engine.db.one(
        "SELECT m.due_at, c.name, c.country FROM messages m JOIN sequences s ON s.id=m.sequence_id"
        " JOIN companies c ON c.id=s.company_id WHERE m.status='approved' AND s.status='active'"
        " AND m.due_at IS NOT NULL ORDER BY m.due_at LIMIT 1")
    if nxt:
        go = engine.next_window(nxt["country"], max(parse_iso(nxt["due_at"]), utcnow()))
        when = "within a few minutes" if go <= utcnow() else _when(engine, go, nxt["country"])
        lines.append(f"-# Next email: {nxt['name']}, {when}")
    return lines


async def status_embed(engine: Engine, title: str = "Status") -> discord.Embed:
    s = engine.status()
    lines = [
        "⏹ **Stopped** — not searching, drafting or sending (still reading replies). Type `start`."
        if s["stopped"] else ("⏸ **Sending paused** — " + (s["pause_reason"] or "by you") + ". Drafting continues.")
        if s["paused"] else "▶️ Running",
        f"**Waiting for you:** {s['awaiting_approval']} draft(s)",
        f"**Sent today:** {s['sent_today']} / {s['daily_cap']} · queued now: {s['queued_to_send']}",
        f"**Sequences running:** {s['active_sequences']} · **sent all-time:** {s['sent_total']}",
        f"**Replies:** {s['replies']} · **bounce rate:** {s['bounce_rate']:.1%} of last {s['bounce_sample']}",
        f"**Prospects:** {s['prospects_new']} new to research · {s['prospects_total']} found in total",
        f"**Model calls today:** {s['model_calls_today']} / {s['model_call_budget']}",
        "",
        *activity_lines(engine),
        "",
        *await usage_lines(engine),
    ]
    return discord.Embed(title=title, description="\n".join(lines), colour=NAVY)


def register_commands(bot: OutreachBot) -> None:
    tree = bot.tree

    def owner_only():
        return app_commands.check(lambda i: i.user.id == bot.owner_id)

    @tree.command(name="status", description="Where outreach stands right now")
    @owner_only()
    async def status(interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)
        await interaction.followup.send(embed=await status_embed(bot.engine))

    @tree.command(name="pause", description="Stop all sending until you resume")
    @owner_only()
    async def pause(interaction: discord.Interaction):
        bot.engine.pause("paused by you")
        await interaction.response.send_message("⏸ Sending paused. Drafts keep arriving; nothing goes out.")

    @tree.command(name="resume", description="Resume sending")
    @owner_only()
    async def resume(interaction: discord.Interaction):
        bot.engine.resume()
        await interaction.response.send_message("▶️ Sending resumed.")

    @tree.command(name="approve_all", description="Approve every draft that's waiting")
    @owner_only()
    async def approve_all(interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)
        await interaction.followup.send(await bot.approve_all())

    @tree.command(name="cap", description="Set today's maximum number of emails")
    @owner_only()
    async def cap(interaction: discord.Interaction, emails_per_day: app_commands.Range[int, 0, 500]):
        value = bot.engine.set_cap(emails_per_day)
        note = f"Daily cap set to {value}."
        if value < emails_per_day:
            note += f" (Capped at max_daily_cap = {bot.engine.cfg.volume.max_daily_cap} in config.toml.)"
        await interaction.response.send_message(note)

    async def segment_choices(interaction: discord.Interaction, current: str):
        # Suggested as you type rather than a fixed list, so businesses sharing a bot keep their own segments.
        p = bot.engine.pipeline
        return [app_commands.Choice(name=p.segment_label(k)[:100], value=k) for k in p.active_segments()
                if current.lower() in f"{k} {p.segment_label(k)}".lower()][:25]

    @tree.command(name="find", description="Look for new prospects in one segment now")
    @owner_only()
    @app_commands.autocomplete(segment=segment_choices)
    async def find(interaction: discord.Interaction, segment: str,
                   how_many: app_commands.Range[int, 1, 25] = 10):
        key = bot.engine.pipeline.resolve_segment(segment)
        if not key:
            known = ", ".join(bot.engine.pipeline.active_segments())
            return await interaction.response.send_message(f"Unknown segment. Try one of: {known}.", ephemeral=True)
        await interaction.response.send_message(f"🔎 Searching for {how_many} "
                                                f"{bot.engine.pipeline.segment_label(key)} prospects. "
                                                "This takes a few minutes.")
        asyncio.create_task(bot.find(key, how_many))

    @tree.command(name="queue", description="List drafts waiting for you")
    @owner_only()
    async def queue(interaction: discord.Interaction):
        await interaction.response.send_message(bot.queue_text(interaction.guild_id or 0), ephemeral=True)

    @tree.command(name="redraft", description="Rewrite every waiting draft in the current style")
    @owner_only()
    async def redraft(interaction: discord.Interaction):
        n = await bot.redraft()
        await interaction.response.send_message(
            f"🔄 Rewriting {n} draft(s). New cards will appear shortly." if n else "Nothing waiting to redraft.")

    @tree.command(name="brain", description="Write with your API credits or your Claude subscription")
    @owner_only()
    @app_commands.choices(use=[app_commands.Choice(name="API credits", value="api"),
                               app_commands.Choice(name="Claude subscription", value="subscription")])
    async def brain(interaction: discord.Interaction, use: app_commands.Choice[str]):
        await interaction.response.send_message(bot.switch_brain(use.value))

    @tree.command(name="credits", description="Tell me your current API balance from the Anthropic Console")
    @owner_only()
    async def credits(interaction: discord.Interaction, balance: app_commands.Range[float, 0, 100000]):
        await interaction.response.send_message(bot.set_credits(str(balance)))

    @tree.command(name="stop", description="Stop all work: searching, drafting and sending")
    @owner_only()
    async def stop(interaction: discord.Interaction):
        bot.engine.stop()
        await interaction.response.send_message(
            "⏹ Stopped: no searching, drafting or sending. Still reading replies. `/start` to begin again.")

    @tree.command(name="start", description="Start all work again")
    @owner_only()
    async def start(interaction: discord.Interaction):
        bot.engine.start()
        await interaction.response.send_message("▶️ Started. Searching, drafting and sending are back on.")
        asyncio.create_task(bot.top_up_when_free())

    @tree.command(name="restart", description="Restart the whole program")
    @owner_only()
    async def restart(interaction: discord.Interaction):
        await interaction.response.send_message("🔄 Restarting. I'll be back online in about a minute.")
        asyncio.create_task(bot.restart())

    @tree.command(name="help", description="List the commands")
    @owner_only()
    async def help_(interaction: discord.Interaction):
        await interaction.response.send_message(help_text(bot.engine), ephemeral=True)

    @tree.error
    async def on_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
        msg = "Only the owner can do that." if isinstance(error, app_commands.CheckFailure) else f"Error: {error}"
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)


async def run_bot(engine: Engine) -> None:
    token = env("DISCORD_BOT_TOKEN")
    if not token or not env("DISCORD_CHANNEL_ID") or not env("DISCORD_OWNER_ID"):
        raise SystemExit("Set DISCORD_BOT_TOKEN, DISCORD_CHANNEL_ID and DISCORD_OWNER_ID in .env first.")
    for read_messages in (True, False):
        bot = OutreachBot(engine, read_messages)
        try:
            async with bot:
                await bot.start(token)
            return
        except discord.PrivilegedIntentsRequired:
            log.warning("Message Content Intent is off for this bot; starting without plain-text commands")
