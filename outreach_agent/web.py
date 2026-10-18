"""Reading a prospect's website and finding an address to write to. No model calls here.

Addresses come only from the company's own pages (or Hunter, if a key is set). Guessed
addresses like first.last@ are never sent: a bounced guess costs more sender reputation
than one more prospect is worth.
"""

import asyncio
import logging
import re
import urllib.robotparser
from dataclasses import dataclass, field
from urllib.parse import unquote, urljoin, urlparse

import dns.asyncresolver
import dns.exception
import httpx
from bs4 import BeautifulSoup

log = logging.getLogger("outreach.web")

USER_AGENT = "Mozilla/5.0 (compatible; OutreachAgent/1.0; +https://github.com/hammad1201/outreach-agent)"
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}")
SUBPAGE_HINT = re.compile(r"about|team|contact|company|founder|people|story|who-we-are|leadership", re.I)
FILE_SUFFIX = re.compile(r"\.(png|jpe?g|gif|svg|webp|css|js)$", re.I)

# Role inboxes that are fine to write to, best first. Anything not listed (support@, jobs@,
# privacy@, abuse@, billing@ ...) is a wrong door for a sales email.
GOOD_ROLES = ["founder", "founders", "ceo", "hello", "hi", "hey", "contact", "info", "team",
              "partnerships", "partners", "business", "sales", "enquiries", "inquiries", "office",
              "operations", "ops", "purchasing", "procurement", "marketing", "general", "mail"]
# Shared inboxes that are acceptable to write to but are not a person — labelled as such.
ROLE_WORDS = set(GOOD_ROLES) | {"service", "customerservice", "cs", "reception", "enquiry", "inquiry",
                                "dealers", "dealer", "wholesale", "studio", "shop", "store", "orders"}
BAD_LOCALPARTS = re.compile(
    r"^(no-?reply|donotreply|support|help|jobs|careers|hr|recruit\w*|privacy|legal|abuse|"
    r"billing|accounts?|invoices?|press|media|security|webmaster|postmaster|admin|dpo|gdpr|"
    r"unsubscribe|returns|orders|shipping|example|test|your|name|email|user|"
    # sample addresses from form placeholders ("you@company.com"), never a real inbox
    r"you|yourname|your\.?name|your\.?email|youremail|yourmail|someone|somebody|me|myname|"
    r"first\.?last|firstname|lastname|firstname\.lastname|john\.?doe|jane\.?doe|name\.surname)$", re.I)


def normalize_domain(value: str) -> str:
    value = (value or "").strip().lower()
    if "//" not in value:
        value = "https://" + value
    host = urlparse(value).hostname or ""
    return host.removeprefix("www.")


@dataclass
class SiteSnapshot:
    domain: str
    pages: list = field(default_factory=list)       # [(url, text)]
    emails: set = field(default_factory=set)
    has_mx: bool = False
    error: str = ""

    def as_text(self, limit_per_page: int = 5000) -> str:
        return "\n\n".join(f"### {url}\n{text[:limit_per_page]}" for url, text in self.pages)


def _visible_text(html: str) -> tuple[str, BeautifulSoup]:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "iframe"]):
        tag.decompose()
    text = re.sub(r"\n{3,}", "\n\n", soup.get_text("\n", strip=True))
    return text, soup


OBFUSCATED_RE = re.compile(
    r"([A-Za-z0-9._%+\-]+)\s*[\[(\{]\s*at\s*[\])\}]\s*([A-Za-z0-9\-]+(?:\s*(?:[\[(\{]\s*dot\s*[\])\}]|\.)\s*[A-Za-z0-9\-]+)+)",
    re.I)
CONTACT_PATHS = ["/contact", "/contact-us", "/about", "/about-us", "/imprint", "/impressum", "/legal-notice"]


def decode_cfemail(hex_string: str) -> str:
    """Cloudflare 'email protection' XORs the address with its first byte."""
    try:
        key = int(hex_string[:2], 16)
        return "".join(chr(int(hex_string[i:i + 2], 16) ^ key) for i in range(2, len(hex_string), 2))
    except ValueError:
        return ""


def _emails_in(html: str, soup: BeautifulSoup, domain: str) -> set:
    found = set(EMAIL_RE.findall(html))
    for tag in soup.select("[data-cfemail]"):
        found.add(decode_cfemail(tag["data-cfemail"]))
    for a in soup.find_all("a", href=True):
        if "email-protection#" in a["href"]:
            found.add(decode_cfemail(a["href"].split("email-protection#", 1)[1]))
    text = soup.get_text(" ")
    for local, rest in OBFUSCATED_RE.findall(text):
        rest = re.sub(r"\s*[\[(\{]\s*dot\s*[\])\}]\s*|\s*\.\s*", ".", rest, flags=re.I)
        found.add(f"{local}@{rest}")
    for a in soup.find_all("a", href=True):
        if a["href"].lower().startswith("mailto:"):
            found.add(unquote(a["href"][7:].split("?")[0]))   # "mailto:%20info@..." is " info@..."
    root = ".".join(domain.split(".")[-2:])
    clean = set()
    for email in found:
        email = unquote(email).strip().strip(".").lower()
        if FILE_SUFFIX.search(email) or not EMAIL_RE.fullmatch(email):
            continue
        email_domain = email.rsplit("@", 1)[-1]
        # Only the company's own domain (or a subdomain of it) — not their web agency's.
        if email_domain == domain or email_domain.endswith("." + root) or email_domain == root:
            clean.add(email)
    return clean


async def has_mx(domain: str) -> bool:
    try:
        answer = await dns.asyncresolver.resolve(domain, "MX", lifetime=8)
        return len(answer) > 0
    except (dns.exception.DNSException, OSError):
        return False


async def page_emails(url: str, domain: str) -> set:
    """Addresses at the company's own domain published on one page, e.g. the job posting that
    led to them ("send your CV to jobs@..."), wherever it is hosted."""
    if not url.startswith(("http://", "https://")):
        return set()
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "en"}
    try:
        async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=15) as client:
            parts = urlparse(url)
            robots = urllib.robotparser.RobotFileParser()
            r = await client.get(f"{parts.scheme}://{parts.netloc}/robots.txt")
            robots.parse(r.text.splitlines() if r.status_code == 200 else [])
            if not robots.can_fetch(USER_AGENT, url):
                return set()
            resp = await client.get(url)
    except (httpx.HTTPError, ValueError):
        return set()
    if resp.status_code != 200 or "html" not in resp.headers.get("content-type", ""):
        return set()
    _, soup = await asyncio.to_thread(_visible_text, resp.text)
    return _emails_in(resp.text, soup, normalize_domain(domain))


async def snapshot(domain: str, max_subpages: int = 3, hints: tuple = ()) -> SiteSnapshot:
    """Home page plus the most useful subpages. `hints` are extra words (e.g. "careers") whose pages
    are read first."""
    extra = re.compile("|".join(re.escape(h) for h in hints), re.I) if hints else None
    domain = normalize_domain(domain)
    snap = SiteSnapshot(domain=domain)
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "en"}
    async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=15) as client:
        robots = urllib.robotparser.RobotFileParser()
        try:
            r = await client.get(f"https://{domain}/robots.txt")
            robots.parse(r.text.splitlines() if r.status_code == 200 else [])
        except httpx.HTTPError:
            robots.parse([])

        async def fetch(url: str):
            if not url.startswith(("http://", "https://")) or "@" in url:
                return None
            try:
                if not robots.can_fetch(USER_AGENT, url):
                    return None
            except ValueError:
                return None
            try:
                resp = await client.get(url)
            except httpx.HTTPError:
                return None
            if resp.status_code != 200 or "html" not in resp.headers.get("content-type", ""):
                return None
            return resp

        home = await fetch(f"https://{domain}/")
        if home is None:
            snap.error = "home page unreachable"
            snap.has_mx = await has_mx(domain)
            return snap
        text, soup = await asyncio.to_thread(_visible_text, home.text)
        snap.pages.append((str(home.url), text))
        snap.emails |= _emails_in(home.text, soup, domain)

        subpages = []
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if "@" in href or href.lower().startswith(("mailto:", "tel:", "javascript:")):
                continue
            url = urljoin(str(home.url), href).split("#")[0]
            host = normalize_domain(url)
            path = urlparse(url).path
            if host == domain and (SUBPAGE_HINT.search(path) or (extra and extra.search(path))) \
                    and url not in subpages:
                subpages.append(url)
        if extra:
            subpages.sort(key=lambda u: not extra.search(urlparse(u).path))
        async def read(urls):
            for resp in await asyncio.gather(*(fetch(u) for u in urls)):
                if resp is None or any(str(resp.url) == u for u, _ in snap.pages):
                    continue
                text, sub_soup = await asyncio.to_thread(_visible_text, resp.text)
                snap.pages.append((str(resp.url), text))
                snap.emails |= _emails_in(resp.text, sub_soup, domain)

        await read(subpages[:max_subpages])
        if not snap.emails:
            # contact pages often exist without a link from the home page
            tried = {urlparse(u).path.rstrip("/") for u, _ in snap.pages}
            base = f"{urlparse(str(home.url)).scheme}://{urlparse(str(home.url)).netloc}"
            paths = CONTACT_PATHS + [f"/{h.strip('/')}" for h in hints]
            await read([base + p for p in paths if p not in tried])

    snap.has_mx = await has_mx(domain)
    return snap


@dataclass
class Pick:
    email: str
    kind: str          # person | role
    name: str = ""
    title: str = ""
    source: str = "website"


def _first_name(full: str) -> str:
    return (full or "").strip().split(" ")[0].lower()


def choose_address(emails: set, decision_maker: str = "", dm_title: str = "",
                   blocked_domains: tuple = (), preferred: tuple = ()) -> Pick | None:
    """Best address to reach the decision maker, from addresses the company itself published.
    `preferred` inboxes (e.g. "jobs", "careers") win outright, even ones normally skipped."""
    preferred = [p.lower() for p in preferred]
    usable = []
    for email in emails:
        local, _, email_domain = email.partition("@")
        if email_domain in blocked_domains or (BAD_LOCALPARTS.match(local) and local not in preferred):
            continue
        usable.append(email)
    if not usable:
        return None
    wanted = sorted((e for e in usable if e.split("@")[0] in preferred),
                    key=lambda e: preferred.index(e.split("@")[0]))
    if wanted:
        return Pick(wanted[0], "role")

    first = _first_name(decision_maker)
    if first and len(first) > 1:
        for email in sorted(usable):
            local = email.split("@")[0]
            if local == first or local.startswith(first + ".") or local.startswith(first[0] + "."):
                return Pick(email, "person", decision_maker, dm_title)

    personal = [e for e in usable if e.split("@")[0] not in ROLE_WORDS]
    roles = [e for e in usable if e.split("@")[0] in GOOD_ROLES]
    if roles:
        roles.sort(key=lambda e: GOOD_ROLES.index(e.split("@")[0]))
        # A founder@/ceo@ inbox beats an unknown person's address; a generic hello@ doesn't.
        if roles[0].split("@")[0] in ("founder", "founders", "ceo"):
            return Pick(roles[0], "role")
    if len(personal) == 1:
        return Pick(personal[0], "person")
    if roles:
        return Pick(roles[0], "role")
    return None


async def hunter_lookup(domain: str, api_key: str, decision_maker: str = "") -> Pick | None:
    """Optional: Hunter.io domain search. Only addresses Hunter has verified are returned."""
    if not api_key:
        return None
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get("https://api.hunter.io/v2/domain-search",
                                 params={"domain": domain, "api_key": api_key, "limit": 10})
        data = r.json().get("data", {}) if r.status_code == 200 else {}
    except (httpx.HTTPError, ValueError):
        return None
    people = []
    for item in data.get("emails", []):
        status = (item.get("verification") or {}).get("status")
        if status != "valid" or (item.get("confidence") or 0) < 85:
            continue
        name = " ".join(filter(None, [item.get("first_name"), item.get("last_name")]))
        people.append(Pick(item["value"].lower(), "person" if item.get("type") == "personal" else "role",
                           name, item.get("position") or "", "hunter"))
    if not people:
        return None
    first = _first_name(decision_maker)
    for p in people:
        if first and _first_name(p.name) == first:
            return p
    senior = re.compile(r"founder|ceo|owner|president|director|head|cto|coo|managing", re.I)
    people.sort(key=lambda p: (p.kind != "person", not senior.search(p.title)))
    return people[0]
