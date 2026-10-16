"""Command line. Every command works on one business's home folder, given with --home
(or $OUTREACH_HOME; default: the current folder if it has a config.toml, else the code folder).

  python -m outreach_agent init ~/MyBiz          create a new home folder with a starter profile
  python -m outreach_agent --home ~/MyBiz doctor            check every connection and setting
  python -m outreach_agent --home ~/MyBiz pipeline          find, research and draft — printed, nothing sent
  python -m outreach_agent --home ~/MyBiz run               the full system, with Discord approvals
  python -m outreach_agent --home ~/MyBiz status            numbers at a glance
  python -m outreach_agent --home ~/MyBiz send-test         send yourself a test email and check its authentication
  python -m outreach_agent --home ~/MyBiz copy-sent         file copies of already-sent emails in your Sent folder
  python -m outreach_agent --home ~/MyBiz install-service   run in the background (macOS), start at login
  python -m outreach_agent --home ~/MyBiz uninstall-service stop the background service
"""

import argparse
import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .brain import Brain, BrainError, _cli_binary
from . import config
from .config import CODE, EXAMPLE_PROFILE, Business, Config, data_dir, env, home
from .db import DB
from .engine import Engine
from .mailer import MailError, Mailer, compose

OK, WARN, BAD = "✅", "⚠️ ", "❌"


def setup_logging(verbose: bool = False) -> None:
    data_dir().mkdir(parents=True, exist_ok=True)
    handlers = [logging.StreamHandler(sys.stdout),
                RotatingFileHandler(data_dir() / "outreach.log", maxBytes=2_000_000, backupCount=3)]
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("discord").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def build(dry_run: bool = False) -> Engine:
    cfg = Config.load()
    db = DB(data_dir() / "outreach.db")
    return Engine(cfg, db, Brain(cfg, db), Mailer(cfg, dry_run=dry_run))


# ── doctor ──

def _dns_txt(name: str) -> list[str]:
    import dns.resolver
    try:
        return [b"".join(r.strings).decode() for r in dns.resolver.resolve(name, "TXT", lifetime=8)]
    except Exception:
        return []


async def doctor(live: bool) -> int:
    cfg = Config.load()
    problems = 0
    print(f"{cfg.business.name} outreach — setup check\n   home: {home()}\n")

    print("Profile")
    if not (home() / "config.toml").exists():
        print(f"  {BAD} no config.toml in {home()} — create a home with `init`, or pass --home")
        problems += 1
    files = [("facts", p) for p in cfg.business.facts_paths()] or [("facts", None)]
    files += [(label, cfg.business.path(value)) for label, value in (
        ("segments", cfg.business.segments_file), ("examples", cfg.business.examples_file),
        ("signature", cfg.business.signature_file))]
    for label, path in files:
        found = bool(path and path.exists())
        required = label in ("facts", "segments")
        print(f"  {OK if found else (BAD if required else WARN)} {label}: {path or '(none)'}"
              + ("" if found else " — missing" + ("" if required else ", optional")))
        problems += 0 if found or not required else 1
    from .pipeline import load_segments
    segments = load_segments(cfg.business.path(cfg.business.segments_file))
    if segments:
        print(f"  {OK} segments: {', '.join(segments)}")


    print("Sender")
    if cfg.sender.postal_address.strip():
        print(f"  {OK} postal address set")
    else:
        print(f"  {WARN} no postal address — US and Canadian companies are skipped (their law requires one).")
        print(f"      Sending to: {', '.join(cfg.target_countries())}")
    domain = cfg.sender.email.split("@")[1]
    spf = [t for t in _dns_txt(domain) if t.startswith("v=spf1")]
    dmarc = [t for t in _dns_txt(f"_dmarc.{domain}") if t.startswith("v=DMARC1")]
    # common DKIM selectors: Google, iCloud, Microsoft 365, and generic ones
    selectors = ("google", "sig1", "selector1", "selector2", "default", "k1", "s1")
    dkim = next((s for s in selectors if _dns_txt(f"{s}._domainkey.{domain}")), None)
    print(f"  {OK if spf else BAD} SPF {'found: ' + spf[0] if spf else 'missing'}")
    print(f"  {OK if dkim else WARN} DKIM {f'found ({dkim} selector)' if dkim else 'not found under the common selectors'}")
    print(f"  {OK if dmarc else WARN} DMARC {'found: ' + dmarc[0] if dmarc else 'missing'}")

    print(f"\nBrain — provider '{cfg.brain.provider}', model {cfg.brain.model}")
    if cfg.brain.provider == "cli":
        try:
            binary = _cli_binary(cfg)
            out = subprocess.run([binary, "auth", "status"], capture_output=True, text=True, timeout=30).stdout
            logged_in = json.loads(out or "{}").get("loggedIn")
            print(f"  {OK if logged_in else BAD} Claude Code {'logged in' if logged_in else 'NOT logged in — run `claude` once and log in'}")
            problems += 0 if logged_in else 1
        except (BrainError, subprocess.SubprocessError, json.JSONDecodeError) as e:
            print(f"  {BAD} {e}")
            problems += 1
    else:
        has_key = bool(env("ANTHROPIC_API_KEY"))
        print(f"  {OK if has_key else BAD} ANTHROPIC_API_KEY {'set' if has_key else 'missing from .env'}")
        problems += 0 if has_key else 1
    if live:
        db = DB(data_dir() / "outreach.db")
        try:
            result = await Brain(cfg, db).json(
                task="doctor", system="Reply with JSON only.", prompt='Return {"ok": true}.',
                schema={"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"],
                        "additionalProperties": False}, effort="low", timeout=120)
            print(f"  {OK if result.get('ok') else BAD} live model call returned {result}")
        except BrainError as e:
            print(f"  {BAD} live model call failed: {e}")
            problems += 1

    print("\nMailbox")
    mailer = Mailer(cfg)
    if not mailer.configured():
        print(f"  {BAD} SMTP_USER / SMTP_PASSWORD not set in .env")
        problems += 1
    else:
        try:
            await asyncio.to_thread(mailer.check_login)
            print(f"  {OK} SMTP and IMAP login work for {mailer.user}")
        except (MailError, Exception) as e:
            print(f"  {BAD} login failed: {e}")
            problems += 1

    print("\nDiscord")
    for key in ("DISCORD_BOT_TOKEN", "DISCORD_CHANNEL_ID", "DISCORD_OWNER_ID"):
        print(f"  {OK if env(key) else BAD} {key} {'set' if env(key) else 'missing from .env'}")
        problems += 0 if env(key) else 1

    print(f"\n{'All good.' if not problems else f'{problems} thing(s) to fix before `run`.'}")
    return 1 if problems else 0


# ── pipeline (dry) ──

async def pipeline(segment: str | None, count: int, discover: bool) -> None:
    engine = build(dry_run=True)
    p, db = engine.pipeline, engine.db
    segments = [segment] if segment else p.active_segments()
    if discover:
        for seg in segments:
            print(f"\n🔎 Discovering {count} {seg} prospects (this can take a few minutes)…")
            added = await p.discover(seg, count)
            print(f"   added {added}")
    fresh = db.all("SELECT id, name, domain FROM companies WHERE status='new' ORDER BY id")
    for row in fresh:
        print(f"\n🔬 Researching {row['name']} ({row['domain']})…")
        status = await engine._safely(p.research, row["id"]) or db.scalar(
            "SELECT status FROM companies WHERE id=?", (row["id"],))
        reason = db.scalar("SELECT status_reason FROM companies WHERE id=?", (row["id"],), "")
        print(f"   → {status}{': ' + reason if reason else ''}")
    for row in db.all("SELECT id, name FROM companies WHERE status='qualified' ORDER BY fit_score DESC"):
        print(f"\n✍️  Drafting for {row['name']}…")
        seq_id = await engine._safely(p.write, row["id"])
        if seq_id:
            print_sequence(engine, seq_id)
        else:
            print("   draft failed: " + (db.scalar("SELECT status_reason FROM companies WHERE id=?", (row["id"],), "") or "?"))


def print_sequence(engine: Engine, seq_id: int) -> None:
    db = engine.db
    seq = db.one("SELECT * FROM sequences WHERE id=?", (seq_id,))
    c = db.one("SELECT * FROM companies WHERE id=?", (seq["company_id"],))
    ct = db.one("SELECT * FROM contacts WHERE id=?", (seq["contact_id"],))
    print("─" * 72)
    print(f"#{seq_id}  {c['name']} ({c['domain']}, {c['country']}) — {c['segment']}, fit {c['fit_score']}/10")
    print(f"To: {ct['email']} ({ct['kind']}{', ' + ct['name'] if ct['name'] else ''})")
    print(f"Noticed: {c['observation']}")
    for m in db.all("SELECT * FROM messages WHERE sequence_id=? ORDER BY step", (seq_id,)):
        print(f"\n[{m['step']}] Subject: {m['subject']}\n{m['body']}")
    print("─" * 72)


# ── send-test ──

async def send_test(to: str | None) -> int:
    cfg = Config.load()
    mailer = Mailer(cfg)
    to = to or mailer.user
    msg = compose(cfg, to=to, subject=f"{cfg.business.name.lower()} outreach test",
                  body=f"This is a test of the {cfg.business.name} outreach mailer. If it arrived in your inbox rather "
                       "than spam, and the headers below pass, sending is set up correctly.")
    message_id = await asyncio.to_thread(mailer.send, msg)
    print(f"Sent to {to} ({message_id}). Waiting 20s, then checking how it was authenticated…")
    time.sleep(20)
    import imaplib
    conn = mailer._imap()
    try:
        typ, data = conn.search(None, "HEADER", "Message-ID", message_id)
        ids = (data[0] or b"").split()
        if not ids:
            print("Not in the inbox yet (or it went to spam). Check Gmail → Show original on the test email.")
            return 1
        typ, parts = conn.fetch(ids[-1], "(BODY.PEEK[HEADER])")
        headers = parts[0][1].decode(errors="replace")
    finally:
        conn.logout()
    from_line = re.search(r"^From:.*$", headers, re.M | re.I)
    from_line = from_line.group(0) if from_line else ""
    if cfg.sender.email.lower() in from_line.lower():
        print(f"  {OK} arrived from {cfg.sender.email}")
    else:
        print(f"  {BAD} Your mail provider replaced the sender: {from_line.strip()}\n"
              f"     Allow {cfg.sender.email} as a sending address (Gmail: Settings → Accounts and Import → "
              "Send mail as; iCloud: it must be one of your custom-domain addresses).")
    results = re.findall(r"^Authentication-Results:.*(?:\n[ \t].*)*", headers, re.M)
    text = " ".join(results).replace("\n", " ")
    for check in ("spf", "dkim", "dmarc"):
        m = re.search(rf"{check}=(\w+)", text)
        verdict = m.group(1) if m else "not reported"
        print(f"  {OK if verdict == 'pass' else WARN} {check.upper()}: {verdict}")
    return 0


# ── copy-sent ──

def copy_sent() -> int:
    """File copies of everything already sent in the mailbox's Sent folder (skips ones already there)."""
    engine = build()
    messages = engine.rebuild_sent()
    if not messages:
        print("Nothing has been sent yet.")
        return 0
    added = skipped = failed = 0
    for msg in messages:
        try:
            if engine.mail.save_copy(msg):
                added += 1
            else:
                skipped += 1
        except Exception as e:
            failed += 1
            print(f"  {BAD} {msg['To']} — {msg['Subject']}: {e}")
    print(f"{OK} Filed {added} in Sent; {skipped} were already there" + (f"; {failed} failed." if failed else "."))
    return 1 if failed else 0


# ── new home ──

CONFIG_EXAMPLE = CODE / "config.example.toml"
ENV_EXAMPLE = CODE / ".env.example"
LAUNCHER = """#!/bin/zsh
# Double-click in Finder. {what}
exec "{script}" "$(cd "$(dirname "$0")" && pwd)"
"""


def init_home(target: str) -> int:
    dest = Path(target).expanduser().resolve()
    if (dest / "config.toml").exists():
        print(f"{dest} already has a config.toml; leaving it alone.")
        return 1
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copy(CONFIG_EXAMPLE, dest / "config.toml")
    if not (dest / ".env").exists():
        shutil.copy(ENV_EXAMPLE, dest / ".env")
        os.chmod(dest / ".env", 0o600)
    if not (dest / "profile").exists():
        shutil.copytree(EXAMPLE_PROFILE, dest / "profile")
    for name, script, what in (
            ("Setup.command", CODE / "scripts/setup.sh", "Checks the setup, then runs outreach in the background."),
            ("Stop.command", CODE / "scripts/stop.sh", "Stops outreach, including at future logins.")):
        (dest / name).write_text(LAUNCHER.format(script=script, what=what))
        os.chmod(dest / name, 0o755)
    (dest / ".gitignore").write_text(".env\ndata/\n")
    print(f"""Created {dest}

Next:
  1. Describe your business in {dest}/profile/ (facts.md, segments.md, examples.md, signature.html)
  2. Fill in {dest}/config.toml and {dest}/.env
  3. Double-click {dest}/Setup.command  (or run: python -m outreach_agent --home {dest} doctor --live)""")
    return 0


# ── service ──

def service_label() -> str:
    """Named after the business (falling back to the folder), so each home gets its own service."""
    name = Config.load().business.name if (home() / "config.toml").exists() else ""
    if not name or name == Business.name:
        name = home().name
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "default"
    return f"dev.outreach-agent.{slug}"


def service_plist() -> Path:
    return Path.home() / f"Library/LaunchAgents/{service_label()}.plist"


def _plists_for_home() -> list[Path]:
    """Services already installed for this home, whatever they were named (e.g. before a rename)."""
    marker = f"<string>{home()}</string>"
    return [p for p in (Path.home() / "Library/LaunchAgents").glob("dev.outreach-agent.*.plist")
            if marker in p.read_text(errors="replace")]


def _remove_service(plist: Path) -> bool:
    result = subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(plist)], capture_output=True)
    plist.unlink(missing_ok=True)
    return result.returncode == 0


PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key><array><string>{run_sh}</string><string>{home}</string></array>
  <key>WorkingDirectory</key><string>{home}</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>60</integer>
  <key>StandardOutPath</key><string>{data}/service.log</string>
  <key>StandardErrorPath</key><string>{data}/service.log</string>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>{path}</string></dict>
</dict></plist>
"""


def install_service() -> int:
    if sys.platform != "darwin":
        print("install-service uses launchd, so it's macOS only. Elsewhere, run "
              f"`{CODE}/run.sh {home()}` under systemd, a container or tmux.")
        return 1
    target = service_plist()
    target.parent.mkdir(parents=True, exist_ok=True)
    data_dir().mkdir(parents=True, exist_ok=True)
    try:
        claude_dir = str(Path(_cli_binary(Config.load())).parent)
    except BrainError:
        claude_dir = str(Path.home() / ".local/bin")
    path = f"{claude_dir}:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
    plist_text = PLIST.format(label=service_label(), run_sh=CODE / "run.sh", home=home(),
                              data=data_dir(), path=path)
    for old in _plists_for_home():
        _remove_service(old)
    target.write_text(plist_text)
    uid = os.getuid()
    result = subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(target)], capture_output=True, text=True)
    if result.returncode != 0:
        print(f"Wrote {target} but launchctl refused it: {result.stderr.strip()}")
        return 1
    print(f"Installed as {service_label()}. It starts now and at every login. Logs: {data_dir()}/service.log\n"
          f"To stop it for good: python -m outreach_agent --home {home()} uninstall-service")
    return 0


def uninstall_service() -> int:
    stopped = [_remove_service(p) for p in {service_plist(), *_plists_for_home()} if p.exists()]
    print(f"Stopped {service_label()}; it won't start at login." if any(stopped)
          else f"{service_label()} wasn't running.")
    return 0


def _default_home() -> str | None:
    if os.environ.get("OUTREACH_HOME"):
        return None
    return str(Path.cwd()) if (Path.cwd() / "config.toml").exists() else None


def main() -> None:
    parser = argparse.ArgumentParser(prog="outreach_agent")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--home", help="the business's folder (config.toml, .env, profile/, data/)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("init", help="create a new home folder with a starter profile")
    i.add_argument("path")
    d = sub.add_parser("doctor")
    d.add_argument("--live", action="store_true", help="also make one real model call")
    p = sub.add_parser("pipeline")
    p.add_argument("--segment", help="one segment from the profile (default: all)")
    p.add_argument("--count", type=int, default=3)
    p.add_argument("--no-discover", action="store_true", help="only research and draft what's already found")
    r = sub.add_parser("run")
    r.add_argument("--dry-run", action="store_true", help="write emails to data/outbox_dryrun instead of sending")
    sub.add_parser("status")
    t = sub.add_parser("send-test")
    t.add_argument("--to")
    sub.add_parser("pause")
    sub.add_parser("resume")
    sub.add_parser("copy-sent", help="file copies of already-sent emails in your mailbox's Sent folder")
    sub.add_parser("install-service")
    sub.add_parser("uninstall-service")
    sub.add_parser("claude-path", help="print the Claude Code binary this will use")
    args = parser.parse_args()

    if args.cmd == "init":
        sys.exit(init_home(args.path))
    config.set_home(args.home or _default_home())
    setup_logging(args.verbose)

    if args.cmd == "doctor":
        sys.exit(asyncio.run(doctor(args.live)))
    if args.cmd == "pipeline":
        asyncio.run(pipeline(args.segment, args.count, not args.no_discover))
    elif args.cmd == "run":
        from .discord_bot import run_bot
        asyncio.run(run_bot(build(dry_run=args.dry_run)))
    elif args.cmd == "status":
        print(json.dumps(build().status(), indent=2))
    elif args.cmd == "send-test":
        sys.exit(asyncio.run(send_test(args.to)))
    elif args.cmd == "pause":
        build().pause("paused from the command line")
        print("Paused.")
    elif args.cmd == "resume":
        build().resume()
        print("Resumed.")
    elif args.cmd == "copy-sent":
        sys.exit(copy_sent())
    elif args.cmd == "install-service":
        sys.exit(install_service())
    elif args.cmd == "uninstall-service":
        sys.exit(uninstall_service())
    elif args.cmd == "claude-path":
        try:
            print(_cli_binary(Config.load()))
        except BrainError as e:
            print(e, file=sys.stderr)
            sys.exit(1)
