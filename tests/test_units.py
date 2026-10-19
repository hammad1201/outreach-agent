import pytest

from outreach_agent import gate, web
from outreach_agent.brain import BrainError, extract_json

from .conftest import GOOD_EMAIL1


# ── gate ──

def test_good_sequence_passes():
    assert gate.check_sequence("companion app for the trailcam pro",
                               [GOOD_EMAIL1, "One example that may map to you, the 30 minutes stand.",
                                "Closing the loop, I won't follow up again."], "Trailcam Co") == []


@pytest.mark.parametrize("subject,problem", [
    ("Re: quick chat", "pretends to be a reply"),
    ("BIG NEWS", "all caps"),
    ("you'll love this!", "exclamation"),
    ("x" * 80, "characters"),
])
def test_bad_subjects(subject, problem):
    assert any(problem in r for r in gate.check_subject(subject))


@pytest.mark.parametrize("insert,problem", [
    ("I hope this finds you well.", "i hope this finds you"),
    ("See https://calendly.com/me.", "another site"),
    ("Hi [First Name],", "placeholder"),
    ("We revolutionize hardware.", "revolutionize"),
])
def test_bad_bodies(insert, problem):
    assert any(problem in r for r in gate.check_body(GOOD_EMAIL1 + " " + insert, 1, "Trailcam Co"))


@pytest.mark.parametrize("insert,problem", [
    ("We're an AI-native studio.", "ai-native"),
    ("It works — really.", "dash"),
    ("Great news!", "exclamation"),
    ("I came across your site.", "i came across"),
    ("Book a discovery call.", "discovery call"),
])
def test_template_tells_are_rejected(insert, problem):
    assert any(problem in r for r in gate.check_body(GOOD_EMAIL1 + " " + insert, 1, "Trailcam Co"))


def test_own_link_allowed_and_both_names_must_appear():
    check = lambda body, company: gate.check_body(body, 1, company, "Kite Labs", "kitelabs.example")
    assert check(GOOD_EMAIL1 + " Details at kitelabs.example.", "Trailcam Co") == []
    assert any("another site" in r for r in check(GOOD_EMAIL1 + " See other.example.com.", "Trailcam Co"))
    assert any("never mentions" in r for r in check(GOOD_EMAIL1, "Acme Robotics"))
    no_intro = GOOD_EMAIL1.replace("I run Kite Labs, a small studio", "I run a small studio")
    assert any("who you are" in r for r in check(no_intro, "Trailcam Co"))
    assert gate.names("i run kitelabs, a studio", "Kite Labs")      # spacing and case don't matter
    assert gate.mentions("hi the kestrel team", "The Kestrel Co")


def test_length_limits():
    assert any("min" in r for r in gate.check_body("Too short.", 1))
    assert any("max" in r for r in gate.check_body("word " * 50, 2))
    assert any("max" in r for r in gate.check_body("word " * 105, 1))


def test_signoff_stripped():
    assert gate.strip_signoff("Body text here.\n\nBest regards,\nSam") == "Body text here."
    assert gate.strip_signoff("Thanks for reading this far, really.") == "Thanks for reading this far, really."


def test_hard_problems_only_block_broken_emails():
    assert gate.hard_problems("subj", ["fine body"]) == []
    assert gate.hard_problems("", ["x"]) and gate.hard_problems("s", ["Hi {name}"])


# ── addresses ──

def test_normalize_domain():
    assert web.normalize_domain("https://www.Acme.com/about?x=1") == "acme.com"
    assert web.normalize_domain("shop.acme.co.uk") == "shop.acme.co.uk"


def test_prefers_named_decision_maker():
    pick = web.choose_address({"hello@a.com", "sarah@a.com", "tom@a.com"}, "Sarah Lee", "Founder")
    assert (pick.email, pick.kind, pick.name) == ("sarah@a.com", "person", "Sarah Lee")


def test_founder_inbox_beats_unknown_person():
    pick = web.choose_address({"founder@a.com", "alex@a.com", "bea@a.com"})
    assert pick.email == "founder@a.com"


def test_department_inbox_is_a_role_not_a_person():
    pick = web.choose_address({"purchasing@a.com", "support@a.com"})
    assert (pick.email, pick.kind) == ("purchasing@a.com", "role")


def test_clean_company_name():
    from outreach_agent.pipeline import clean_name
    assert clean_name("Harbour Cuts (Harbour Grooming Ltd)") == "Harbour Cuts"
    assert clean_name("(Acme)") == "(Acme)"
    assert clean_name("Kestrel Foods") == "Kestrel Foods"


def test_falls_back_to_good_role_and_skips_bad_ones():
    assert web.choose_address({"support@a.com", "hello@a.com", "jobs@a.com"}).email == "hello@a.com"
    assert web.choose_address({"support@a.com", "noreply@a.com", "privacy@a.com"}) is None
    assert web.choose_address({"x@gmail.com"}, blocked_domains=("gmail.com",)) is None


def test_email_extraction_keeps_own_domain_only():
    html = ('<a href="mailto:hello@acme.com?subject=hi">x</a> sales@acme.com '
            'built by dev@webagency.com logo@2x.png team@eu.acme.com')
    _, soup = web._visible_text(html)
    assert web._emails_in(html, soup, "acme.com") == {"hello@acme.com", "sales@acme.com", "team@eu.acme.com"}


# ── model output parsing ──

def test_extract_json_variants():
    assert extract_json('Here you go:\n```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('noise {"a": [1, 2]} trailing') == {"a": [1, 2]}
    with pytest.raises(BrainError):
        extract_json("no json here")


def test_snapshot_survives_bad_links(monkeypatch):
    import asyncio
    import httpx
    html = ('<html><body><a href="contact@acme.example">contact@acme.example</a>'
            '<a href="/about">About</a><a href="tel:+123">call</a> hello@acme.example</body></html>')

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, headers={"content-type": "text/html"}, text=html)

    real_client = httpx.AsyncClient
    monkeypatch.setattr(web.httpx, "AsyncClient",
                        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))

    async def no_mx(domain):
        return True
    monkeypatch.setattr(web, "has_mx", no_mx)
    snap = asyncio.run(web.snapshot("acme.example"))
    assert [u for u, _ in snap.pages] == ["https://acme.example/", "https://acme.example/about"]
    assert "hello@acme.example" in snap.emails and "contact@acme.example" in snap.emails


@pytest.mark.parametrize("raw,code", [
    ("GB", "GB"), ("UK", "GB"), ("United Kingdom", "GB"), ("England", "GB"), ("London, United Kingdom", "GB"),
    ("Ireland", "IE"), ("New Zealand", "NZ"), ("Sweden", "SE"), ("USA", "US"), ("Austin, TX", "US"),
    ("the Netherlands", "NL"), ("", ""), ("Narnia", ""),
])
def test_country_names_become_iso_codes(raw, code):
    from outreach_agent.countries import normalize_country
    assert normalize_country(raw) == code


def test_cloudflare_protected_and_obfuscated_emails():
    # "hello@acme.example" encoded the way Cloudflare does it, key 0x42
    key = 0x42
    encoded = f"{key:02x}" + "".join(f"{ord(ch) ^ key:02x}" for ch in "hello@acme.example")
    assert web.decode_cfemail(encoded) == "hello@acme.example"
    html = (f'<span class="__cf_email__" data-cfemail="{encoded}">[email&#160;protected]</span>'
            f'<a href="/cdn-cgi/l/email-protection#{encoded}">mail</a>'
            '<p>Write to sales [at] acme [dot] example or info(at)acme.example</p>')
    _, soup = web._visible_text(html)
    assert web._emails_in(html, soup, "acme.example") == {"hello@acme.example", "sales@acme.example",
                                                           "info@acme.example"}


def test_unlinked_contact_page_is_tried(monkeypatch):
    import asyncio
    import httpx
    pages = {"/": "<html><body>Welcome. No email here.</body></html>",
             "/contact": "<html><body>Email us: team@acme.example</body></html>"}

    def handler(request):
        if request.url.path in pages:
            return httpx.Response(200, headers={"content-type": "text/html"}, text=pages[request.url.path])
        return httpx.Response(404)

    real_client = httpx.AsyncClient
    monkeypatch.setattr(web.httpx, "AsyncClient",
                        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))

    async def mx(domain):
        return True
    monkeypatch.setattr(web, "has_mx", mx)
    snap = asyncio.run(web.snapshot("acme.example"))
    assert snap.emails == {"team@acme.example"}



def test_html_email_escapes_text_and_carries_signature():
    from outreach_agent import mailer
    from outreach_agent.config import Config, Sender
    cfg = Config.load()
    cfg.sender = Sender(name="Sam Rivera", postal_address="2 Sample Road, Springfield")
    msg = mailer.compose(cfg, to="a@b.example", subject="hi", body="Hi Sam,\n\nUse <b> & stuff.\nLine two.")
    rich = msg.get_body(preferencelist=("html",)).get_content()
    plain = msg.get_body(preferencelist=("plain",)).get_content()
    assert "&lt;b&gt; &amp; stuff.<br>Line two." in rich            # their text can't inject markup
    assert "kite-mark.png" in rich and "<!--" not in rich         # signature in, instructions out
    assert "2 Sample Road, Springfield" in rich and "unsubscribe" in rich
    assert "Sam Rivera" in plain and "<table" not in plain     # plain copy stays plain



def test_usage_text_from_claude_code_is_parsed():
    from outreach_agent.brain import parse_usage
    text = ("You are currently using your subscription to power your Claude Code usage\n\n"
            "Current session: 23% used · resets Oct 17 at 12:50am (Europe/London)\n"
            "Current week (all models): 20% used · resets Oct 20 at 2pm (Europe/London)\n"
            "Current week (Fable): 2% used · resets Oct 20 at 2pm (Europe/London)\n")
    assert parse_usage(text) == {"session": (23, "Oct 17 at 12:50am"), "week": (20, "Oct 20 at 2pm")}
    assert parse_usage("something else entirely") == {}


def test_api_cost_and_spend(tmp_path):
    from outreach_agent.brain import Brain, call_cost
    from outreach_agent.config import Config
    from outreach_agent.db import DB
    assert call_cost("claude-opus-5-5", 1_000_000, 100_000, 10) == pytest.approx(4 + 2 + 0.10)
    # a million tokens read from cache costs $0.20 instead of $4; writing them costs 1.25x
    assert call_cost("claude-opus-5-5", 0, 0, 0, cache_read=1_000_000) == pytest.approx(0.20)
    assert call_cost("claude-opus-5-5", 0, 0, 0, cache_write=1_000_000) == pytest.approx(5.0)
    db = DB(tmp_path / "t.db")
    brain = Brain(Config.load(), db)
    db.log("api_usage", {"in": 5000, "out": 1000, "web_searches": 0, "cost": 0.04})
    db.log("api_usage", {"in": 1_000_000, "out": 0, "web_searches": 0})      # older entry without cost
    assert brain.api_spend(db.start_of_utc_day()) == pytest.approx(0.04 + 4.0)


def test_api_call_logs_every_resumed_request_and_uses_caching(tmp_path, monkeypatch):
    import asyncio
    import json as _json
    from types import SimpleNamespace as NS
    from outreach_agent.brain import Brain
    from outreach_agent.config import Config
    from outreach_agent.db import DB

    def resp(stop, text, tin, tout, cw, cr, searches):
        usage = NS(input_tokens=tin, output_tokens=tout, cache_creation_input_tokens=cw,
                   cache_read_input_tokens=cr, server_tool_use=NS(web_search_requests=searches))
        return NS(stop_reason=stop, content=[NS(type="text", text=text)], usage=usage)

    calls = []
    replies = [resp("pause_turn", "", 50_000, 500, 40_000, 0, 10),
               resp("end_turn", '{"companies": []}', 2_000, 800, 10_000, 90_000, 6)]

    class FakeMessages:
        async def create(self, **kw):
            calls.append(kw)
            return replies[len(calls) - 1]

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    db = DB(tmp_path / "t.db")
    cfg = Config.load(); cfg.brain.provider = "api"; cfg.brain.api_budget_usd = 0
    brain = Brain(cfg, db)
    brain._api = NS(beta=NS(messages=FakeMessages()))
    db.set("brain_provider", "api")
    out = asyncio.run(brain.json(task="discover", system="s", prompt="p", schema={}, web=True))
    assert out == {"companies": []}
    assert len(calls) == 2 and all(c["cache_control"] == {"type": "ephemeral"} for c in calls)
    logged = _json.loads(db.scalar("SELECT detail FROM events WHERE kind='api_usage'"))
    assert (logged["in"], logged["out"], logged["cache_write"], logged["cache_read"], logged["web_searches"],
            logged["requests"]) == (52_000, 1_300, 50_000, 90_000, 16, 2)
    expected = (52_000 * 4 + 50_000 * 1.25 * 4 + 90_000 * 0.20 + 1_300 * 20) / 1e6 + 16 * 0.01
    assert logged["cost"] == pytest.approx(expected, abs=1e-5)


class FakeImap:
    """Just enough IMAP to file a copy in Sent."""

    def __init__(self, listing, existing=()):
        self.listing, self.existing, self.appended = listing, set(existing), []

    def list(self):
        return "OK", self.listing

    def select(self, box, readonly=False):
        self.box = box
        return "OK", [b"1"]

    def search(self, charset, *criteria):
        return "OK", [b"7" if criteria[-1] in self.existing else b""]

    def append(self, box, flags, when, data):
        self.appended.append((box, flags, data))
        return "OK", [b"done"]

    def logout(self):
        pass


def test_sent_copy_goes_to_the_flagged_sent_folder_once(cfg, monkeypatch):
    from outreach_agent import mailer
    listing = [b'(\\HasNoChildren) "/" "INBOX"', b'(\\HasNoChildren \\Sent) "/" "Sent Messages"']
    imap = FakeImap(listing)
    m = mailer.Mailer(cfg)
    monkeypatch.setattr(m, "_imap", lambda: imap)
    msg = mailer.compose(cfg, to="a@b.example", subject="hi", body="Hello there.")
    assert m.save_copy(msg) is True
    box, flags, data = imap.appended[0]
    assert box == '"Sent Messages"' and "Seen" in flags and b"Hello there." in data
    imap.existing.add(msg["Message-ID"])
    assert m.save_copy(msg) is False and len(imap.appended) == 1      # never filed twice


def test_sent_folder_found_by_name_without_flags(cfg, monkeypatch):
    from outreach_agent import mailer
    m = mailer.Mailer(cfg)
    assert m._sent_folder(FakeImap([b'() "/" "INBOX"', b'() "/" "Sent Items"'])) == '"Sent Items"'


def test_save_to_sent_is_off_for_gmail_only():
    from outreach_agent.config import Mail
    assert not Mail().saves_sent                                          # Gmail files sent mail itself
    assert Mail(smtp_host="smtp.mail.me.com").saves_sent
    assert not Mail(smtp_host="smtp.mail.me.com", save_to_sent=False).saves_sent


def test_already_sent_emails_rebuild_with_their_real_ids(engine, run):
    run(engine.top_up())
    seq_id = engine.recorder.drafts[0]
    engine.approve(seq_id)
    run(engine.send_one())
    sent = engine.db.one("SELECT * FROM messages WHERE sequence_id=? AND step=1", (seq_id,))
    rebuilt = engine.rebuild_sent()
    assert len(rebuilt) == 1
    assert rebuilt[0]["Message-ID"] == sent["message_id"] and rebuilt[0]["To"] == "sarah@trailcam.example"


def test_smtp_falls_back_to_another_address_when_one_times_out(cfg, monkeypatch):
    from outreach_agent import mailer
    tried = []

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            tried.append(host)
            if host == cfg.mail.smtp_host:
                raise TimeoutError("timed out")
            self._host = host

    monkeypatch.setattr(mailer.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(mailer, "_other_addresses", lambda host: ["10.0.0.1", "10.0.0.2"])
    smtp = mailer.Mailer(cfg)._smtp()
    assert tried == [cfg.mail.smtp_host, "10.0.0.1"]
    assert smtp._host == cfg.mail.smtp_host          # TLS still verifies the real server name

    monkeypatch.setattr(mailer, "_other_addresses", lambda host: [])
    with pytest.raises(TimeoutError):
        mailer.Mailer(cfg)._smtp()


def test_preferred_inboxes_win_even_when_normally_skipped():
    emails = {"jobs@acme.example", "hello@acme.example", "sam@acme.example"}
    assert web.choose_address(emails).email != "jobs@acme.example"         # skipped by default
    assert web.choose_address(emails, preferred=("careers", "jobs")).email == "jobs@acme.example"
    assert web.choose_address({"support@acme.example"}, preferred=("jobs",)) is None


def test_non_commercial_mail_has_no_optout_and_carries_the_attachment(cfg, home):
    from outreach_agent import mailer
    cv = home / "cv.pdf"
    cv.write_bytes(b"%PDF-1.4 test")
    cfg.business.commercial = False
    cfg.sender.signature = "Sam Rivera\niOS Engineer\nlinkedin.com/in/sam"
    msg = mailer.compose(cfg, to="jobs@acme.example", subject="ios role", body="Hi there.", attachments=(cv,))
    plain = msg.get_body(preferencelist=("plain",)).get_content()
    assert "unsubscribe" not in plain and msg["List-Unsubscribe"] is None
    assert plain.rstrip().endswith("linkedin.com/in/sam")
    files = [p for p in msg.iter_attachments() if p.get_filename()]
    assert [f.get_filename() for f in files] == ["cv.pdf"] and files[0].get_content() == b"%PDF-1.4 test"


def test_job_search_skips_the_postal_address_rule(cfg):
    cfg.sender.postal_address = ""
    cfg.targets.allowed_countries = ["US", "CA", "GB"]
    assert cfg.target_countries() == ["GB"]
    cfg.business.commercial = False
    assert cfg.target_countries() == ["US", "CA", "GB"]


def test_exclude_list_matches_names_and_domains(engine):
    engine.cfg.targets.exclude = ["N26", "sumup.com", "Whoop"]
    p = engine.pipeline
    assert p.excluded("N26 GmbH", "n26.example")
    assert p.excluded("Anything", "jobs.sumup.com")
    assert p.excluded("WHOOP (Whoop Inc.)", "whoop.example")
    assert not p.excluded("Swoop Labs", "swoop.example")


def test_mailto_links_with_encoded_spaces_give_clean_addresses():
    from bs4 import BeautifulSoup
    html = '<a href="mailto:%20info@acme.example">Email</a> <a href="mailto:Sales%40acme.example?subject=hi">x</a>'
    found = web._emails_in(html, BeautifulSoup(html, "html.parser"), "acme.example")
    assert found == {"info@acme.example", "sales@acme.example"}


def test_placeholder_addresses_from_forms_are_never_used():
    for fake in ("you@acme.example", "your.name@acme.example", "firstname.lastname@acme.example", "john.doe@acme.example"):
        assert web.choose_address({fake}) is None, fake
    assert web.choose_address({"you@acme.example", "info@acme.example"}).email == "info@acme.example"


def test_has_mx_says_dont_know_when_dns_is_unreachable(monkeypatch):
    import asyncio
    import dns.resolver
    async def timeout(*a, **k):
        raise dns.resolver.LifetimeTimeout(timeout=8, errors=[])
    async def nxdomain(*a, **k):
        raise dns.resolver.NXDOMAIN()
    monkeypatch.setattr(web.dns.asyncresolver, "resolve", timeout)
    assert asyncio.run(web.has_mx("acme.example")) is None
    monkeypatch.setattr(web.dns.asyncresolver, "resolve", nxdomain)
    assert asyncio.run(web.has_mx("acme.example")) is False
