"""Settings from config.toml, secrets from .env.

The program and a business's setup live apart. A *home* folder holds one business's
config.toml, .env, profile files and data; the code folder holds only the program.
One copy of the code can run several businesses, each from its own home.
"""

import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

CODE = Path(__file__).resolve().parent.parent
PROMPTS = CODE / "prompts"
EXAMPLE_PROFILE = CODE / "profiles" / "example"

_home: Path | None = None


def set_home(path: str | Path | None) -> Path:
    global _home
    _home = Path(path).expanduser().resolve() if path else None
    return home()


def home() -> Path:
    """The business's folder: --home, else $OUTREACH_HOME, else the code folder."""
    if _home is not None:
        return _home
    return Path(os.environ.get("OUTREACH_HOME") or CODE).expanduser().resolve()


def data_dir() -> Path:
    return home() / "data"


def load_env(path: Path | None = None) -> None:
    path = path or home() / ".env"
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.split(" #", 1)[0].strip().strip('"').strip("'")
        if value:
            os.environ.setdefault(key.strip(), value)


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


@dataclass
class Business:
    name: str = "Your Business"
    website: str = "example.com"
    # Profile files, relative to the home folder (or absolute).
    facts_file: str = "profile/facts.md"
    segments_file: str = "profile/segments.md"
    examples_file: str = "profile/examples.md"
    signature_file: str = "profile/signature.html"
    # Let emails mention the website once, as plain text (e.g. where to sign up). Off by default:
    # a first cold email without links reads more personal and delivers better.
    link_in_email: bool = False

    def path(self, value: str) -> Path | None:
        if not value:
            return None
        p = Path(value).expanduser()
        return (p if p.is_absolute() else home() / p).resolve()

    @property
    def domain(self) -> str:
        site = self.website.lower().strip().removeprefix("https://").removeprefix("http://")
        return site.removeprefix("www.").split("/")[0]


@dataclass
class Sender:
    name: str = "Your Name"
    email: str = "you@example.com"
    title: str = "Founder"
    postal_address: str = ""

    @property
    def first_name(self) -> str:
        return self.name.split()[0] if self.name.split() else self.name


@dataclass
class BrainCfg:
    provider: str = "cli"
    model: str = "claude-opus-5-5"
    max_calls_per_day: int = 150
    cli_path: str = ""
    api_budget_usd: float = 0.0


@dataclass
class Volume:
    start_daily_cap: int = 20
    ramp_step: int = 5
    max_daily_cap: int = 150
    min_gap_seconds: int = 120
    max_gap_seconds: int = 420
    hold_bounce_rate: float = 0.03
    pause_bounce_rate: float = 0.05


@dataclass
class Schedule:
    heartbeat_minutes: int = 10
    send_days: list = field(default_factory=lambda: [0, 1, 2, 3, 4])
    send_start_hour: int = 9
    send_end_hour: int = 16
    followup_days: list = field(default_factory=lambda: [4, 10])
    approval_queue_target: int = 12
    timezone: str = "UTC"
    digest_hour: int = 9


@dataclass
class Targets:
    # Empty means every segment in the segments file.
    segments: list = field(default_factory=list)
    allowed_countries: list = field(default_factory=lambda: ["US", "GB", "CA", "AU"])
    blocked_email_domains: list = field(default_factory=list)
    discovery_batch: int = 10


@dataclass
class Mail:
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    imap_host: str = "imap.gmail.com"
    imap_port: int = 993


def _build(cls, raw: dict):
    known = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in (raw or {}).items() if k in known})


# Countries whose anti-spam law requires the sender's postal address in every commercial email
# (US CAN-SPAM, Canada CASL). Without an address configured, these are skipped entirely.
ADDRESS_REQUIRED = {"US", "CA"}


@dataclass
class Config:
    business: Business
    sender: Sender
    brain: BrainCfg
    volume: Volume
    schedule: Schedule
    targets: Targets
    mail: Mail

    @property
    def has_postal_address(self) -> bool:
        return bool(self.sender.postal_address.strip())

    def target_countries(self) -> list[str]:
        countries = [c.upper() for c in self.targets.allowed_countries]
        if not self.has_postal_address:
            countries = [c for c in countries if c not in ADDRESS_REQUIRED]
        return countries

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        load_env()
        path = path or home() / "config.toml"
        raw = tomllib.loads(path.read_text()) if path.exists() else {}
        return cls(
            business=_build(Business, raw.get("business")),
            sender=_build(Sender, raw.get("sender")),
            brain=_build(BrainCfg, raw.get("brain")),
            volume=_build(Volume, raw.get("volume")),
            schedule=_build(Schedule, raw.get("schedule")),
            targets=_build(Targets, raw.get("targets")),
            mail=_build(Mail, raw.get("mail")),
        )
