import re

INTERNAL_NOTE_PATTERNS = [
    r'\[VALID phone captured[^\]]*\]',
    r'\[INVALID phone[^\]]*\]',
    r'\[DELIVERY confirmed[^\]]*\]',
    r'\[SYSTEM NOTE:[^\]]*\]',
    r'\[conversation started\]',
    r'\(waiting for[^)]*\)',
    r'─── CAPTURED STATE ───.*?───────────────────',
]


def strip_internal_notes(text: str) -> str:
    for pattern in INTERNAL_NOTE_PATTERNS:
        text = re.sub(pattern, '', text, flags=re.IGNORECASE)
    return text.strip()


def clean_name(raw: str) -> str:
    name = raw.strip()
    patterns = [
        r"(?i)^(hel+o+|hi+|hey+)[,!]?\s*(i'?m|i am|my name is|this is|am)?\s*",
        r"(?i)^(good\s+(morning|evening|afternoon))[,!]?\s*",
        r"(?i)^(i'?m|i am|my name is|this is)\s+",
        r"(?i)^'?m\s+",
    ]
    for p in patterns:
        name = re.sub(p, "", name).strip()
    name = name.lstrip("',-. ")
    name = re.sub(r"[^\w\s-]", "", name).strip()
    result = " ".join(w.capitalize() for w in name.split() if w)
    return result or raw.strip()
