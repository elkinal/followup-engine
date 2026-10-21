"""Deterministic, template-based message drafting.

Templates are keyed by the policy's reason_code so the draft text matches
why the quote was surfaced. Kept intentionally plain/short (SMS-length) --
a real system would A/B these or let the owner customize per-tech tone.
"""
from __future__ import annotations

from .policy import Candidate

_TEMPLATES = {
    "reply_pending": (
        "Hi {customer_name}, this is {tech_name} following up on your "
        "${amount:,} quote -- sorry for the delay getting back to you! "
        "Happy to answer any questions."
    ),
    "viewed_no_response": (
        "Hi {customer_name}, it's {tech_name}. Just checking in on the "
        "${amount:,} quote I sent over -- let me know if you have any "
        "questions or want to move forward."
    ),
    "stale_big": (
        "Hi {customer_name}, {tech_name} here. Wanted to check in on your "
        "${amount:,} quote -- still happy to get this scheduled whenever "
        "works for you."
    ),
    "stale_general": (
        "Hi {customer_name}, this is {tech_name}. Following up on your "
        "open quote (${amount:,}) -- let me know if you'd like to move "
        "forward or have questions."
    ),
}

_DEFAULT_TEMPLATE = (
    "Hi {customer_name}, this is {tech_name} following up on your "
    "${amount:,} quote."
)


def _first_name(name: str) -> str:
    parts = name.split()
    return parts[0] if parts else "there"  # blank/whitespace-only name: generic greeting, don't crash


def draft_message(candidate: Candidate) -> str:
    template = _TEMPLATES.get(candidate.reason_code, _DEFAULT_TEMPLATE)
    return template.format(
        customer_name=_first_name(candidate.customer_name),
        tech_name=candidate.tech_name,
        amount=candidate.amount,
    )
