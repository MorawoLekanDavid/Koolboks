"""Regression tests for chatbot.utils.phone.

Written after a real incident: rewriting this module as a full file (instead
of a targeted diff) silently dropped SESSION_ID_RE, which crashed the whole
API on startup (every router imports through chat_service, which imports this
module) -- caught only by a live health check after deploying, not before.
These tests exist so that class of mistake fails locally before it ever
reaches production again.
"""
import re

import pytest

from chatbot.utils.phone import (
    SESSION_ID_RE,
    extract_valid_phone,
    normalize_phone,
    strict_normalize_phone,
)


# ── Module surface — the exact thing that broke production ─────────────────

def test_session_id_re_exists_and_matches_a_real_session_id():
    """This is the regression test for the actual incident: a full-file
    rewrite of phone.py dropped this export, and chat_service.py's import
    line (`from chatbot.utils.phone import SESSION_ID_RE, ...`) crash-looped
    the API container on startup."""
    assert isinstance(SESSION_ID_RE, re.Pattern)
    assert SESSION_ID_RE.match("wa_2348012345678")
    assert not SESSION_ID_RE.match("short")


# ── normalize_phone — Nigeria ────────────────────────────────────────────────

@pytest.mark.parametrize("raw", [
    "07037428227",
    "7037428227",
    "2347037428227",
    "+2347037428227",
])
def test_normalize_phone_nigeria(raw):
    assert normalize_phone(raw) == "+2347037428227"


# ── normalize_phone — Kenya ──────────────────────────────────────────────────

@pytest.mark.parametrize("raw", [
    "0712345678",
    "254712345678",
    "+254712345678",
])
def test_normalize_phone_kenya(raw):
    assert normalize_phone(raw) == "+254712345678"


def test_normalize_phone_kenya_01_prefix():
    # Kenyan NSNs may also start with 1 (newer allocations), not just 7.
    assert normalize_phone("+254112345678") == "+254112345678"


# ── normalize_phone — Uganda ─────────────────────────────────────────────────

@pytest.mark.parametrize("raw", [
    "256771234567",
    "+256771234567",
])
def test_normalize_phone_uganda(raw):
    assert normalize_phone(raw) == "+256771234567"


def test_normalize_phone_bare_local_defaults_kenya():
    # 0 + 9 digits is Kenya/Uganda-shaped and genuinely ambiguous between the
    # two without more context (documented limitation, not a bug) -- defaults
    # to Kenya, the more common real-world case for this app's customers.
    assert normalize_phone("0771234567") == "+254771234567"


def test_normalize_phone_unrecognized_returns_input_unchanged():
    assert normalize_phone("not a phone") == "not a phone"


# ── extract_valid_phone ──────────────────────────────────────────────────────

@pytest.mark.parametrize("sentence,expected_contains", [
    ("My number is 07037428227 please call", "07037428227"),
    ("you can reach me on +254712345678 anytime", "+254712345678"),
    ("Uganda number: +256771234567", "+256771234567"),
])
def test_extract_valid_phone_finds_number_in_sentence(sentence, expected_contains):
    result = extract_valid_phone(sentence)
    assert result is not None
    assert expected_contains in result


def test_extract_valid_phone_returns_none_when_absent():
    assert extract_valid_phone("no phone number in this message at all") is None


# ── strict_normalize_phone ───────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("0902 995 1974", "+2349029951974"),   # spaces are a normal way to type a number
    ("+254712345678", "+254712345678"),
    ("+256771234567", "+256771234567"),
])
def test_strict_normalize_phone_accepts_real_numbers(raw, expected):
    assert strict_normalize_phone(raw) == expected


@pytest.mark.parametrize("raw", [
    "not a number",
    "12345",
    "",
])
def test_strict_normalize_phone_rejects_garbage(raw):
    with pytest.raises(ValueError):
        strict_normalize_phone(raw)
