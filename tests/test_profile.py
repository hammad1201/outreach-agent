"""A business is described by its home folder: config, profile files, data. Nothing about it is in the code."""

from outreach_agent import cli, config
from outreach_agent.config import Config
from outreach_agent.pipeline import load_segments, render


def test_example_profile_parses():
    segments = load_segments(config.EXAMPLE_PROFILE / "segments.md")
    assert list(segments) == ["booking", "websites"]
    booking = segments["booking"]
    assert booking.label == "Booking" and "salons" in booking.aliases
    assert "barbershops" in booking.niches
    # the Label/Aliases/Niches lines and the editing notes never reach the model
    assert "Niches:" not in booking.brief and "Label:" not in booking.brief and "<!--" not in booking.brief
    assert "Skip:" in booking.brief


def test_segments_resolve_by_key_label_or_alias(engine):
    p = engine.pipeline
    assert p.active_segments() == ["hardware"]
    assert p.resolve_segment("IoT") == p.resolve_segment("hardware") == "hardware"
    assert p.resolve_segment("bakeries") is None


def test_prompts_get_the_business_not_a_hardcoded_one(engine):
    system = engine.pipeline.system
    assert "Kite Labs" in system and "connected devices" in system and "{" not in system


def test_render_leaves_json_and_unknown_slots_alone():
    out = render('Find {count}. {"companies": [{"name": "..."}]} {unknown}', count=3)
    assert out == 'Find 3. {"companies": [{"name": "..."}]} {unknown}'


def test_init_creates_a_working_home(tmp_path):
    target = tmp_path / "Hair Book"
    assert cli.init_home(str(target)) == 0
    for name in ("config.toml", ".env", "profile/facts.md", "profile/segments.md", "Setup.command", "Stop.command"):
        assert (target / name).exists(), name
    assert (target / ".env").stat().st_mode & 0o077 == 0          # secrets readable by you only
    assert cli.init_home(str(target)) == 1                          # never overwrites a home
    config.set_home(target)
    cfg = Config.load()
    assert cfg.business.name == "Kite Labs" and cfg.business.domain == "kitelabs.example"
    assert cli.service_label() == "dev.outreach-agent.kite-labs"     # named after the business


def test_email_link_is_opt_in(engine, run):
    run(engine.top_up())
    c = engine.db.one("SELECT * FROM companies WHERE status='drafted'")
    contact = engine.db.one("SELECT * FROM contacts WHERE company_id=?", (c["id"],))
    assert "include links or web addresses" in engine.pipeline._write_prompt(c, contact)
    engine.cfg.business.link_in_email = True
    assert "other than kitelabs.example" in engine.pipeline._write_prompt(c, contact)


def test_facts_can_come_from_several_files(engine, home):
    (home / "more.md").write_text("Founded after the founder started treatment himself.")
    engine.cfg.business.facts_file = ["profile/facts.md", "more.md", "missing.md"]
    system = engine.pipeline.system
    assert "connected devices" in system and "started treatment" in system
