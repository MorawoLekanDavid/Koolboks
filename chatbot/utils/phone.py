import re
from typing import Optional

PHONE_RE = re.compile(r'(?<!\d)(0[789]\d{9}|[789]\d{9}|\+234[789]\d{9})(?!\d)')
SESSION_ID_RE = re.compile(r'^[a-zA-Z0-9_\-\+]{8,120}$')


def normalize_phone(phone: str) -> str:
    """Normalize any Nigerian phone format to +234XXXXXXXXXX (E.164).
    Handles: 07037428227, 7037428227, 2347037428227, +2347037428227
    Returns the input unchanged if it doesn't match any known pattern."""
    p = phone.strip().lstrip('+')
    if p.startswith('234') and len(p) == 13:
        digits = p[3:]
    elif p.startswith('0') and len(p) == 11:
        digits = p[1:]
    elif len(p) == 10 and p[0] in '789':
        digits = p
    else:
        return phone.strip()
    return '+234' + digits


def extract_valid_phone(text: str) -> Optional[str]:
    m = PHONE_RE.search(text)
    if not m:
        return None
    number = m.group()
    if len(number) == 10 and not number.startswith('0') and not number.startswith('+'):
        number = '0' + number
    return number


def phone_from_history(history: list) -> str:
    for msg in history:
        m = re.search(r'VALID phone captured: (\S+)', msg.get("content", ""))
        if m:
            return m.group(1).rstrip('.')
    return ""
