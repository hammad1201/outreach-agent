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
    assert cli.service_label() == "dev.outreach-agent.hair-book"
