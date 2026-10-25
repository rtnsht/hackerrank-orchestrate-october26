"""Does the request text authorize deletion?  Keyword rules (fallback) + optional Claude classifier."""
from __future__ import annotations

import re

DELETE_PATTERNS = [
    r"\bdelet", r"\beras", r"\bremov", r"\bclos(e|ing) (my|this|the) account", r"\bclose .*account",
    r"account.*\bclosed?\b", r"forget me", r"wipe",
    r"supprim", r"effac", r"elimin", r"borra", r"lösch", r"entfern", r"apag", r"exclu", r"cancel",
    r"rimuov", r"cancell", r"verwijder", r"usuń", r"usun", r"削除", r"退会", r"삭제", r"탈퇴", r"删除", r"注销",
    r"удал", r"sil(in|me)", r"hapus",
]
NON_DELETE_PATTERNS = [
    r"\bdownload my (account )?(information|data)\b", r"portable format", r"copy of (my|last year)",
    r"\binvoices?\b.*\brecords\b", r"\b(replace|update|change|correct)\b.*\b(number|phone|email address|address)\b",
    r"leave (my|the) (account|profile)", r"keep the account", r"continue using", r"leave the account active",
    r"\bunsubscribe\b", r"stop sending", r"opt[- ]out",
]


def keyword_intent(text: str) -> tuple[bool, str]:
    t = text.lower()
    non = [p for p in NON_DELETE_PATTERNS if re.search(p, t)]
    dele = [p for p in DELETE_PATTERNS if re.search(p, t)]
    if non and not re.search(r"\b(delete|erase|remove) (my|this|the) account", t):
        return False, f"non-deletion cue: {non[0]}"
    if dele:
        return True, f"deletion cue: {dele[0]}"
    return False, "no deletion cue"


def classify(text: str) -> tuple[bool, str]:
    return keyword_intent(text)
