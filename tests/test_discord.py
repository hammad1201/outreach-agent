"""The Discord layer, offline: cards render within Discord's limits and buttons survive a restart."""

import re

import pytest

from outreach_agent import discord_bot as d


@pytest.fixture
def bot(engine, monkeypatch):
    monkeypatch.setenv("DISCORD_CHANNEL_ID", "123")
    monkeypatch.setenv("DISCORD_OWNER_ID", "456")
    b = d.OutreachBot(engine)
    b.wired_notifier = engine.notifier
    engine.notifier = engine.recorder   # offline: record instead of posting
    return b


def test_bot_wires_itself_in(bot, engine):
    assert isinstance(bot.wired_notifier, d.DiscordNotifier)
    assert bot.owner_id == 456
    names = {c.name for c in bot.tree.get_commands()}
    assert names == {"status", "pause", "resume", "approve_all", "cap", "find", "queue", "redraft", "help", "brain", "credits", "stop", "start", "restart"}


def test_draft_card_and_buttons(engine, bot, run):
    run(engine.top_up())
    seq_id = engine.recorder.drafts[0] if engine.recorder.drafts else 1
    embed = d.draft_embed(engine, seq_id)
    assert len(embed.description) <= 4096 and len(embed) <= 6000
    fields = {f.name.split(" ·")[0]: f.value for f in embed.fields}
    assert "sarah@trailcam.example" in fields["Sends to"] and "named person" in fields["Sends to"]
    assert fields["Website"] == "[trailcam.example](https://trailcam.example)"
    assert fields["Decision maker"].startswith("Sarah Lee") and fields["Country"] == "US"
    assert {"What they do", "Why them", "What was noticed", "Evidence", "Follow-up 1", "Follow-up 2"} <= set(fields)
    assert "Worth a quick chat?" in embed.description

    view = d.draft_view(seq_id, "pending")
    ids = [item.custom_id for item in view.children]
    assert ids == [f"draft:{a}:{seq_id}" for a in ("approve", "edit", "rewrite", "reject", "block")]
    # each ID round-trips through the template a restarted bot uses to rebuild the button
    for custom_id in ids:
        assert re.fullmatch(d.DraftButton.__discord_ui_compiled_template__, custom_id)
    assert d.draft_view(seq_id, "active") is None

    engine.approve(seq_id)
    assert "Approved" in d.draft_embed(engine, seq_id).description


def test_reply_card(engine, bot, run):
    run(engine.top_up())
    seq_id = engine.recorder.drafts[0]
    engine.approve(seq_id)
    run(engine.send_one())
    inbound_id = engine.db.insert(
        "INSERT INTO inbound(uid, from_email, subject, body, sequence_id, kind, classification, summary, handled)"
        " VALUES(1,'sarah@trailcam.example','Re: x','Let us talk','%d','reply','interested','Wants a call',1)" % seq_id)
    draft_id = engine.db.insert("INSERT INTO reply_drafts(inbound_id, body, created_at) VALUES(?,?,?)",
                                (inbound_id, "Which times suit you?", "2026-01-01"))
    embed = d.reply_embed(engine, inbound_id, draft_id)
    assert "Interested" in embed.description and embed.fields[0].value == "Which times suit you?"
    assert [i.custom_id for i in d.reply_view(draft_id).children] == [
        f"reply:{a}:{draft_id}" for a in ("send", "edit", "dismiss")]


def test_long_text_is_clipped():
    assert len(d.clip("x" * 5000, 4000)) == 4000


class FakeMessage:
    def __init__(self, content, author_id=456, channel_id=123):
        from types import SimpleNamespace
        self.content = content
        self.author = SimpleNamespace(id=author_id, bot=False)
        self.channel = SimpleNamespace(id=channel_id)
        self.guild = SimpleNamespace(id=1)
        self.mentions = []
        self.replies = []

    async def reply(self, content=None, embed=None):
        self.replies.append(embed.description if embed else content)


def test_text_commands(engine, bot, run):
    engine.cfg.brain.api_budget_usd = 100
    msg = FakeMessage("status")
    run(bot.on_message(msg))
    assert "Sent today" in msg.replies[0]
    assert "23% of this 5-hour session (resets Oct 17 at 12:50am)" in msg.replies[0]
    assert "API credits:" in msg.replies[0] and "of your $" in msg.replies[0]

    msg = FakeMessage("Pause")
    run(bot.on_message(msg))
    assert engine.paused and "paused" in msg.replies[0]
    run(bot.on_message(FakeMessage("resume")))
    assert not engine.paused

    msg = FakeMessage("cap 35")
    run(bot.on_message(msg))
    assert engine.daily_cap() == 35

    msg = FakeMessage("help")
    run(bot.on_message(msg))
    assert "find hardware 5" in msg.replies[0]

    msg = FakeMessage("find")
    run(bot.on_message(msg))
    assert "Which kind" in msg.replies[0]


def test_text_commands_ignore_strangers_and_chatter(engine, bot, run):
    stranger = FakeMessage("pause", author_id=999)
    run(bot.on_message(stranger))
    assert not engine.paused and stranger.replies == []
    other_channel = FakeMessage("pause", channel_id=777)
    run(bot.on_message(other_channel))
    assert not engine.paused
    chatter = FakeMessage("note to self: call stefano")
    run(bot.on_message(chatter))
    assert chatter.replies == []


def test_redraft_rewrites_waiting_drafts(engine, bot, run, monkeypatch):
    class FakeChannel:
        async def fetch_message(self, _id):
            raise d.discord.NotFound(type("R", (), {"status": 404, "reason": "x"})(), "gone")

    async def fake_channel():
        return FakeChannel()

    async def no_top_up():
        pass

    monkeypatch.setattr(bot, "channel", fake_channel)
    monkeypatch.setattr(bot, "top_up_when_free", no_top_up)
    run(engine.top_up())
    old = engine.recorder.drafts[0]
    engine.db.execute("UPDATE sequences SET discord_message_id='42' WHERE id=?", (old,))
    assert run(bot.redraft()) == 1
    assert engine.db.scalar("SELECT stop_reason FROM sequences WHERE id=?", (old,)) == "redrafted"
    assert engine.db.scalar("SELECT status FROM companies") == "qualified"
    run(engine.top_up())                               # the engine writes it again
    assert engine.db.scalar("SELECT COUNT(*) FROM sequences WHERE status='pending'") == 1


def test_help_is_pinned_and_topic_set(engine, bot, run):
    from types import SimpleNamespace

    class Msg:
        def __init__(self, content):
            self.content, self.id, self.pinned = content, 77, False

        async def pin(self):
            self.pinned = True

        async def edit(self, content):
            self.content = content

    class Channel:
        topic = ""
        sent = []

        async def edit(self, topic):
            self.topic = topic

        async def send(self, content):
            m = Msg(content); self.sent.append(m); return m

        async def fetch_message(self, _id):
            return self.sent[0]

    ch = Channel()
    run(bot.ensure_help(ch))
    assert ch.topic == d.topic_text(engine) and len(ch.sent) == 1 and ch.sent[0].pinned
    run(bot.ensure_help(ch))                    # a restart reuses the pinned message
    assert len(ch.sent) == 1


def test_help_falls_back_without_permissions(engine, bot, run):
    forbidden = d.discord.Forbidden(type("R", (), {"status": 403, "reason": "x"})(), "no")

    class Msg:
        content, id, pinned = d.help_text(engine), 5, False

        async def pin(self):
            raise forbidden

    class Channel:
        topic = ""
        notes = []

        async def edit(self, topic):
            raise forbidden

        async def send(self, content):
            self.notes.append(content); return Msg()

        async def fetch_message(self, _id):
            return Msg()

    ch = Channel()
    run(bot.ensure_help(ch))
    assert "Manage Channels" in ch.notes[-1] and "Pin Messages" in ch.notes[-1]
    run(bot.ensure_help(ch))                    # the permission note isn't repeated
    assert sum("Manage Channels" in n for n in ch.notes) == 1


def test_card_stays_under_discord_limit_with_huge_content(engine, bot, run):
    run(engine.top_up())
    seq_id = engine.recorder.drafts[0]
    big = "x" * 5000
    engine.db.execute("UPDATE companies SET name=?, summary=?, why_fit=?, observation=?, evidence=?, "
                      "evidence_url=?, decision_maker=?, dm_title=?", ("N" * 300, big, big, big, big,
                                                                        "https://" + "u" * 3000, big, big))
    engine.db.execute("UPDATE messages SET subject=?, body=?", (big, big))
    assert len(d.draft_embed(engine, seq_id)) <= 6000


def test_brain_switch_without_restart(engine, bot, run, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    msg = FakeMessage("brain api")
    run(bot.on_message(msg))
    assert engine.brain.provider == "api" and "API credits" in msg.replies[0]
    run(bot.on_message(FakeMessage("brain subscription")))
    assert engine.brain.provider == "cli"
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    msg = FakeMessage("brain api")
    run(bot.on_message(msg))
    assert engine.brain.provider == "cli" and "ANTHROPIC_API_KEY" in msg.replies[0]


def test_credits_calibrate_the_balance(engine, bot, run):
    msg = FakeMessage("credits $89.94")
    run(bot.on_message(msg))
    assert "89.94" in msg.replies[0]
    engine.db.log("api_usage", {"in": 0, "out": 0, "web_searches": 0, "cost": 1.20})
    msg = FakeMessage("status")
    run(bot.on_message(msg))
    assert "about **$88.74 left**" in msg.replies[0]
    bad = FakeMessage("credits lots")
    run(bot.on_message(bad))
    assert "number" in bad.replies[0]


def test_status_shows_work_in_progress(engine, bot, run):
    import asyncio

    async def scenario():
        gate = asyncio.Event()
        original = engine.brain.json

        async def slow_json(**kw):
            if kw["task"] == "discover":
                await gate.wait()
            return await original(**kw)

        engine.brain.json = slow_json
        task = asyncio.create_task(engine.pipeline.discover("hardware"))
        await asyncio.sleep(0)
        during = await d.status_embed(engine)
        gate.set()
        await task
        after = await d.status_embed(engine)
        return during.description, after.description

    during, after = run(scenario())
    assert "🔎 Searching for hardware prospects:" in during
    assert "Searching for" not in after.split("**Working on now:**")[1].split("-#")[0]
    assert "Nothing running" in after and "Last search" in after


def test_stop_and_start(engine, bot, run, monkeypatch):
    msg = FakeMessage("stop")
    run(bot.on_message(msg))
    assert engine.stopped and "Stopped" in msg.replies[0]

    run(engine.heartbeat())                                   # no searching or drafting while stopped
    assert engine.recorder.drafts == [] and engine.brain.calls == []

    status = FakeMessage("status")
    run(bot.on_message(status))
    assert "Stopped" in status.replies[0]

    async def noop():
        pass
    monkeypatch.setattr(bot, "top_up_when_free", noop)
    msg = FakeMessage("start")
    run(bot.on_message(msg))
    assert not engine.stopped and "Started" in msg.replies[0]
    run(engine.heartbeat())
    assert engine.recorder.drafts


def test_restart_closes_so_the_service_relaunches(engine, bot, run, monkeypatch):
    closed = []

    async def fake_close():
        closed.append(True)
    monkeypatch.setattr(bot, "close", fake_close)

    async def scenario():
        msg = FakeMessage("restart")
        await bot.on_message(msg)
        await __import__("asyncio").sleep(1.2)
        return msg
    msg = run(scenario())
    assert "Restarting" in msg.replies[0] and closed == [True]


def test_shared_bot_answers_only_in_its_own_channel(bot, run):
    """Two businesses on one bot: each copy receives every interaction and must ignore the other's."""
    from types import SimpleNamespace
    mine = SimpleNamespace(channel_id=123, client=bot)
    theirs = SimpleNamespace(channel_id=999, client=bot)
    button = d.DraftButton("approve", 1)
    assert run(button.interaction_check(mine)) and not run(button.interaction_check(theirs))
    assert run(d.ReplyButton("send", 1).interaction_check(mine))
    assert run(bot.tree.interaction_check(mine)) and not run(bot.tree.interaction_check(theirs))
