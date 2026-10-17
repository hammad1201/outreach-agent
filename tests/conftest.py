import asyncio
import shutil
from pathlib import Path

import pytest

from outreach_agent import engine as engine_mod
from outreach_agent import mailer as mailer_mod
from outreach_agent import pipeline as pipeline_mod
from outreach_agent import web
from outreach_agent.brain import Brain
from outreach_agent import config as config_mod
from outreach_agent.config import BrainCfg, Business, Config, Mail, Schedule, Sender, Targets, Volume
from outreach_agent.db import DB
from outreach_agent.engine import Engine, Notifier
from outreach_agent.mailer import Mailer

GOOD_EMAIL1 = ("Hi Sarah,\n\nYour setup guide still has people plugging the Trailcam Pro into a laptop to "
               "configure it. A phone app over Bluetooth would make that a lot easier.\n\nI run Kite Labs, a small "
               "studio that builds apps and firmware for connected devices, and we've done this for a bike-light "
               "maker in Leeds for a few years.\n\nWorth a quick chat?")


class FakeBrain(Brain):
    """Canned model answers keyed by task, so the pipeline runs with no model at all."""

    def __init__(self, cfg, db):
        super().__init__(cfg, db)
        self.calls = []
        self.answers = {
            "discover": {"companies": [
                {"name": "Trailcam Co", "domain": "https://www.trailcam.example/", "country": "us",
                 "why_fit": "Hardware with no app", "evidence": "Setup by USB", "evidence_url": "https://trailcam.example"},
                {"name": "Kickstarter thing", "domain": "kickstarter.com/projects/x", "country": "US",
                 "why_fit": "x", "evidence": "x", "evidence_url": "x"},
                {"name": "Berlin GmbH", "domain": "berlin.example", "country": "DE",
                 "why_fit": "x", "evidence": "x", "evidence_url": "x"},
            ]},
            "research": {"qualified": True, "fit_score": 8, "reason": "clear need",
                         "observation": "Your setup guide still walks buyers through a USB cable.",
                         "pitch_angle": "companion app", "summary": "Trail cameras.",
                         "decision_maker": "Sarah Lee", "dm_title": "Founder", "country": "US"},
            "write": {"subject": "companion app for the trailcam pro", "email1": GOOD_EMAIL1 + "\n\nBest,\nSam",
                      "followup1": "One example that may map to you: we built live telemetry over Bluetooth for a "
                                   "racing control unit. The 30 minutes stand if useful.",
                      "followup2": "I'll leave it there. If an app makes the roadmap, just reply."},
            "reply": {"classification": "interested", "summary": "Wants a call next week",
                      "draft": "Great to hear from you. Which times next week suit you?"},
        }

    async def subscription_usage(self):
        return {"session": (23, "Oct 17 at 12:50am"), "week": (20, "Oct 20 at 2pm")}

    async def json(self, *, task, system, prompt, schema, web=False, effort="medium", timeout=300):
        self.calls.append(task)
        self.db.incr(self._budget_key())
        return self.answers[task]


class Recorder(Notifier):
    def __init__(self):
        self.drafts, self.replies, self.alerts, self.batches, self.manual = [], [], [], [], []

    async def draft_ready(self, seq_id):
        self.drafts.append(seq_id)

    async def manual_ready(self, app_id):
        self.manual.append(app_id)

    async def drafts_batch(self, count):
        self.batches.append(count)

    async def reply_received(self, inbound_id, draft_id):
        self.replies.append((inbound_id, draft_id))

    async def alert(self, text):
        self.alerts.append(text)


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    """Every test runs in its own home folder holding the test profile."""
    monkeypatch.delenv("OUTREACH_HOME", raising=False)
    shutil.copytree(Path(__file__).parent / "profile", tmp_path / "profile")
    config_mod.set_home(tmp_path)
    yield tmp_path
    config_mod.set_home(None)


@pytest.fixture
def cfg():
    return Config(
        business=Business(name="Kite Labs", website="https://kitelabs.example"),
        sender=Sender(name="Sam Rivera", email="hello@kitelabs.example", title="Founder, Kite Labs",
                      postal_address="1 Test Street, Springfield"),
        brain=BrainCfg(provider="cli", max_calls_per_day=200),
        volume=Volume(start_daily_cap=20, min_gap_seconds=0, max_gap_seconds=0),
        schedule=Schedule(send_days=[0, 1, 2, 3, 4, 5, 6], send_start_hour=0, send_end_hour=24,
                          approval_queue_target=1),
        targets=Targets(allowed_countries=["US", "GB"], blocked_email_domains=["gmail.com"]),
        mail=Mail(),
    )


@pytest.fixture
def site(monkeypatch):
    async def fake_snapshot(domain, max_subpages=3, hints=()):
        return web.SiteSnapshot(domain=web.normalize_domain(domain),
                                pages=[(f"https://{domain}/", "Trailcam Pro. Setup with the USB configurator.")],
                                emails={"sarah@trailcam.example", "support@trailcam.example"}, has_mx=True)
    monkeypatch.setattr(web, "snapshot", fake_snapshot)
    monkeypatch.setattr(pipeline_mod.web, "snapshot", fake_snapshot)


@pytest.fixture
def engine(cfg, tmp_path, monkeypatch, site):
    db = DB(tmp_path / "test.db")
    rec = Recorder()
    eng = Engine(cfg, db, FakeBrain(cfg, db), Mailer(cfg, dry_run=True), rec)
    eng.recorder = rec
    eng.outbox = tmp_path / "data" / "outbox_dryrun"
    return eng


@pytest.fixture
def run():
    return lambda coro: asyncio.run(coro)
