import re
from typing import Optional

SESSION_ID_RE = re.compile(r'^[a-zA-Z0-9_\-\+]{8,120}$')

# Real customers exist outside Nigeria (confirmed live), but the phone patterns
# below only ever covered Nigeria -- a Kenyan or Ugandan number was structurally
# invisible to extract_valid_phone() no matter what they typed, so CAPTURED
# STATE never flipped to "phone captured" and the bot just kept demanding a
# Nigerian number forever. Confirmed live: an ICARDA researcher requesting 2
# solar freezer systems for Kisumu, Kenya, got stonewalled across
# two separate attempts, including being told to find a Nigerian colleague's
# number instead, despite offering an email address as an alternative.
#
# Nigeria:  local 0 + 10 digits (11 total), national significant number
#           starts 7/8/9  ->  +234 7/8/9 XXXXXXXXX
# Kenya:    local 0 + 9 digits (10 total), NSN starts 7 or 1
#           ->  +254 7/1 XXXXXXXX
# Uganda:   local 0 + 9 digits (10 total), NSN starts 7
#           ->  +256 7 XXXXXXXX
PHONE_RE = re.compile(
    r'(?<!\d)('
    r'0[789]\d{9}|[789]\d{9}|\+234[789]\d{9}|\+2340[789]\d{9}|234[789]\d{9}|'   # Nigeria
    r'\+254[71]\d{8}|254[71]\d{8}|'                                            # Kenya (intl)
    r'\+256[7]\d{8}|256[7]\d{8}'                                               # Uganda (intl)
    r')(?!\d)'
)

# A bare local number with a leading 0 and 10 total digits (0 + 9) is Kenya or
# Uganda's shape -- Nigeria's local shape is 11 digits, so there's no ambiguity
# with Nigeria, only between these two. Matched separately from PHONE_RE above
# since it needs its own country default (see normalize_phone).
_LOCAL_KE_UG_RE = re.compile(r'(?<!\d)(0[71]\d{8})(?!\d)')


def normalize_phone(phone: str) -> str:
    """Normalize a Nigerian, Kenyan or Ugandan phone number to E.164.
    Handles, per country:
      Nigeria: 07037428227, 7037428227, 2347037428227, +2347037428227
      Kenya:   0712345678, 254712345678, +254712345678
      Uganda:  0771234567, 256771234567, +256771234567
    A bare local number (leading 0, 10 digits total) is Kenya/Uganda-shaped
    but genuinely ambiguous between the two without more context -- defaults
    to Kenya, the more common case; wrong-country display on a lead record is
    a minor cosmetic issue, unlike rejecting the number outright.
    Returns the input unchanged if it doesn't match any known pattern."""
    p = phone.strip().lstrip('+')

    if p.startswith('234') and len(p) == 13:
        return '+234' + p[3:]
    if p.startswith('0') and len(p) == 11 and p[1] in '789':
        return '+234' + p[1:]
    if len(p) == 10 and p[0] in '789':
        return '+234' + p

    if p.startswith('254') and len(p) == 12:
        return '+254' + p[3:]
    if p.startswith('256') and len(p) == 12:
        return '+256' + p[3:]
    if p.startswith('0') and len(p) == 10 and p[1] in '71':
        return '+254' + p[1:]  # ambiguous KE/UG local shape, defaults to Kenya

    return phone.strip()


def strict_normalize_phone(phone: str) -> str:
    """Like normalize_phone, but raises ValueError instead of silently
    handing back the input unchanged when it doesn't resolve to a real
    Nigerian, Kenyan or Ugandan E.164 number. normalize_phone's fallback
    behavior is right for its other callers (e.g. a WhatsApp webhook's
    `from`, which is already a real number just possibly in an odd shape)
    — it's wrong for a human typing a number into a form (invites,
    admin-edited contact info), where garbage needs to be rejected outright
    instead of stored verbatim. Strips spaces/dashes/parens first —
    "0902 995 1974" is a completely normal way to type a real number, not
    something to reject."""
    cleaned = re.sub(r"[\s\-()]", "", phone.strip())
    norm = normalize_phone(cleaned)
    if not re.fullmatch(r"\+234[789]\d{9}|\+254[71]\d{8}|\+256[7]\d{8}", norm):
        raise ValueError(f"Not a valid Nigerian, Kenyan or Ugandan phone number: {phone!r}")
    return norm


def extract_valid_phone(text: str) -> Optional[str]:
    m = PHONE_RE.search(text)
    if not m:
        m = _LOCAL_KE_UG_RE.search(text)
        if not m:
            return None
    number = m.group()
    # Repair the common "+234" + redundant leading "0" mistake some lead-gen
    # forms produce (e.g. +23408125474609) instead of rejecting a fixable number.
    if number.startswith('+2340'):
        number = '+234' + number[5:]
    if len(number) == 10 and not number.startswith('0') and not number.startswith('+') and number[0] in '789':
        number = '0' + number
    return number


def phone_from_history(history: list) -> str:
    for msg in history:
        m = re.search(r'VALID phone captured: (\S+)', msg.get("content", ""))
        if m:
            return m.group(1).rstrip('.')
    return ""
