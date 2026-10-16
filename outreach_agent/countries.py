"""Country names and loose codes → ISO 3166-1 alpha-2. Models write "UK", "United Kingdom" or
"England" as often as "GB"; the targeting and time-zone logic only understand the real code."""

import re

ALIASES = {
    "GB": "GB", "UK": "GB", "UNITED KINGDOM": "GB", "GREAT BRITAIN": "GB", "BRITAIN": "GB", "ENGLAND": "GB",
    "SCOTLAND": "GB", "WALES": "GB", "NORTHERN IRELAND": "GB",
    "US": "US", "USA": "US", "U.S.": "US", "U.S.A.": "US", "UNITED STATES": "US", "UNITED STATES OF AMERICA": "US",
    "AMERICA": "US",
    "CA": "CA", "CANADA": "CA", "AU": "AU", "AUSTRALIA": "AU", "NZ": "NZ", "NEW ZEALAND": "NZ",
    "IE": "IE", "IRELAND": "IE", "REPUBLIC OF IRELAND": "IE", "EIRE": "IE",
    "NL": "NL", "NETHERLANDS": "NL", "THE NETHERLANDS": "NL", "HOLLAND": "NL",
    "SE": "SE", "SWEDEN": "SE", "DK": "DK", "DENMARK": "DK", "NO": "NO", "NORWAY": "NO", "FI": "FI", "FINLAND": "FI",
    "DE": "DE", "GERMANY": "DE", "AT": "AT", "AUSTRIA": "AT", "CH": "CH", "SWITZERLAND": "CH",
    "FR": "FR", "FRANCE": "FR", "BE": "BE", "BELGIUM": "BE", "ES": "ES", "SPAIN": "ES", "IT": "IT", "ITALY": "IT",
    "PT": "PT", "PORTUGAL": "PT", "PL": "PL", "POLAND": "PL", "SG": "SG", "SINGAPORE": "SG",
    "AE": "AE", "UAE": "AE", "UNITED ARAB EMIRATES": "AE", "IN": "IN", "INDIA": "IN", "ZA": "ZA", "SOUTH AFRICA": "ZA",
    "PK": "PK", "PAKISTAN": "PK", "IL": "IL", "ISRAEL": "IL", "JP": "JP", "JAPAN": "JP",
}
_US_STATE = re.compile(r",\s*(AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|"
                       r"NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY|DC)$")


def normalize_country(value: str | None) -> str:
    """Best ISO code for whatever the model wrote, or "" when it can't be told."""
    text = (value or "").strip().upper().strip(".")
    if not text:
        return ""
    if text in ALIASES:
        return ALIASES[text]
    # "London, United Kingdom", "Austin, TX", "Ireland (Dublin)"
    for part in re.split(r"[,(/)\-]", text):
        part = part.strip()
        if part in ALIASES and len(part) > 2:
            return ALIASES[part]
    if _US_STATE.search(text):
        return "US"
    return ""
