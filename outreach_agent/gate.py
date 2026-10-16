"""Fixed checks every email passes before it can be approved or sent. No model involved:
prompt rules are a request, these are a guarantee."""

import re

BANNED = (
    "i hope this finds you", "hope this email finds you", "hope you're doing well", "hope you are well",
    "quick question", "just checking in", "circle back", "touch base", "game-changer", "game changer",
    "cutting-edge", "cutting edge", "revolutionize", "revolutionise", "unlock your", "supercharge",
    "synergy", "leverage our", "act now", "limited time", "risk-free", "100% free", "click here",
    "guaranteed", "no-brainer", "picture this", "what if i told you", "in today's fast-paced",
    "as an ai", "as a language model", "i'm an ai", "dear sir", "dear madam", "to whom it may concern",
    # reads like a template rather than a person
    "i came across", "i wanted to reach out", "reaching out", "genuinely", "no obligation", "a quick no",
    "saves us both time", "the door is open", "door's open", "ai-native", "end-to-end", "end to end",
    "full-stack", "discovery call", "fixed quote", "weekly demo", "solutions", "5.0 rating",
    "delivered projects", "i'd happily", "happy to hop on",
)
URL_RE = re.compile(r"https?://\S+|www\.\S+|\b[a-z0-9\-]+\.(?:com|io|co|net|org|app|ai)\b/?\S*", re.I)
PLACEHOLDER_RE = re.compile(r"[\[\]{}<>]|\b(?:xxx|lorem|tbd|first name|company name|your name)\b", re.I)
SIGNOFF_RE = re.compile(
    r"\n\s*(best|best regards|regards|kind regards|warm regards|cheers|thanks|thank you|sincerely|talk soon)"
    r"[,!.]?\s*(\n.*)?$", re.I | re.S)

LIMITS = {1: (30, 100), 2: (6, 45), 3: (4, 35)}


FILLER = {"the", "a", "an", "and", "of", "inc", "ltd", "llc", "co", "company", "corp", "gmbh", "limited", "group"}


def mentions(text_lower: str, company: str) -> bool:
    tokens = [t for t in re.findall(r"[a-z0-9]+", company.lower()) if t not in FILLER and len(t) >= 3]
    return not tokens or any(t in text_lower for t in tokens)


def names(text_lower: str, business: str) -> bool:
    """Whether the text names the business: 'Hair Book', 'HairBook' and 'hair-book' all count."""
    squash = lambda t: re.sub(r"[^a-z0-9]", "", t.lower())
    return squash(business) in squash(text_lower)


def strip_signoff(body: str) -> str:
    """Models like to sign off; the signature is added separately, so drop theirs."""
    return SIGNOFF_RE.sub("", body.strip()).strip()


def words(text: str) -> int:
    return len(re.findall(r"\b[\w'’-]+\b", text))


def check_subject(subject: str) -> list[str]:
    reasons = []
    s = (subject or "").strip()
    if not s:
        reasons.append("subject is empty")
    if len(s) > 70:
        reasons.append(f"subject is {len(s)} characters (max 70)")
    if re.match(r"^\s*(re|fwd?)\s*:", s, re.I):
        reasons.append("subject pretends to be a reply or forward")
    if "!" in s:
        reasons.append("subject has an exclamation mark")
    if s and s.upper() == s and any(c.isalpha() for c in s):
        reasons.append("subject is all caps")
    return reasons


def check_body(body: str, step: int, company: str = "", business: str = "", site: str = "") -> list[str]:
    """`business` must be named in the first email; `site` is the one domain it may link to."""
    reasons = []
    text = (body or "").strip()
    low = text.lower()
    lo, hi = LIMITS.get(step, LIMITS[2])
    n = words(text)
    if n < lo:
        reasons.append(f"email {step} is {n} words (min {lo})")
    if n > hi:
        reasons.append(f"email {step} is {n} words (max {hi})")
    for phrase in BANNED:
        if phrase in low:
            reasons.append(f'contains "{phrase}"')
    if PLACEHOLDER_RE.search(text):
        reasons.append("contains a placeholder, bracket or brace")
    links = URL_RE.findall(text)
    foreign = [link for link in links if not site or site.lower() not in link.lower()]
    if foreign:
        reasons.append(f"links to another site: {foreign[0]}")
    if len(links) > 1:
        reasons.append(f"{len(links)} links (max 1)")
    if step == 1 and company and not mentions(low, company):
        reasons.append(f"first email never mentions {company}")
    if step == 1 and business and not names(low, business):
        reasons.append(f"first email never says who you are (name {business} once)")
    if "<html" in low or "<p>" in low:
        reasons.append("contains HTML")
    if "—" in text or " – " in text or " - " in text:
        reasons.append("uses a dash (reads as machine-written)")
    if ";" in text:
        reasons.append("uses a semicolon")
    if "!" in text:
        reasons.append("uses an exclamation mark")
    return reasons


def check_sequence(subject: str, bodies: list[str], company: str = "", business: str = "",
                   site: str = "") -> list[str]:
    reasons = check_subject(subject)
    for step, body in enumerate(bodies, start=1):
        reasons += check_body(body, step, company, business, site)
    return reasons


def hard_problems(subject: str, bodies: list[str]) -> list[str]:
    """The subset that blocks sending even when you wrote or edited the email yourself."""
    reasons = []
    if not (subject or "").strip():
        reasons.append("subject is empty")
    for step, body in enumerate(bodies, start=1):
        if not (body or "").strip():
            reasons.append(f"email {step} is empty")
        elif PLACEHOLDER_RE.search(body):
            reasons.append(f"email {step} still has a placeholder, bracket or brace")
    return reasons
