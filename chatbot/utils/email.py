import re

# Structural check only (has @, a domain with a real TLD, no spaces/illegal
# chars) — deliberately not a full RFC 5322 parser. It catches "missing the
# @" or "no dot in the domain" typos; it can't catch a misspelled-but-
# otherwise-valid domain like "koolbks.fr" for "koolboks.fr" — that class of
# typo is caught by having the person type the email twice (see the
# "confirm email" field on the invite form) instead.
_EMAIL_RE = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$"
)


def is_valid_email(email: str) -> bool:
    return bool(email) and bool(_EMAIL_RE.match(email.strip()))
