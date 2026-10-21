"""Minimal phone number normalization.

This is deliberately simple (regex + a US-centric heuristic), not a real
i18n phone library (e.g. libphonenumber) -- appropriate for an MVP with
stdlib-only dependencies and US-formatted seed data, but it's a known
simplification: a real deployment serving non-US shops would need a proper
library here instead. The point of normalizing at all is that two different
string spellings of the same number (`+19175552003` vs `(917) 555-2003`)
must resolve to the same customer identity for the cooldown guardrail to do
its job.
"""
from __future__ import annotations

import re

_MIN_DIGITS = 7  # loose floor for "plausibly a phone number"


def normalize_phone(raw: object) -> str | None:
    """Return an E.164-ish canonical form (e.g. '+19175552003'), or None if
    `raw` isn't a string that plausibly contains a phone number."""
    if not isinstance(raw, str):
        return None
    had_plus = raw.strip().startswith("+")
    digits = re.sub(r"\D", "", raw)
    if len(digits) < _MIN_DIGITS:
        return None
    if had_plus:
        return "+" + digits
    if len(digits) == 10:
        return "+1" + digits  # bare 10-digit US number
    if len(digits) == 11 and digits[0] == "1":
        return "+" + digits  # US number with leading 1, no +
    return "+" + digits  # best effort for anything else
