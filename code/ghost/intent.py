"""Does the request text authorize deletion?  Keyword rules (fallback) + optional Claude classifier."""
from __future__ import annotations

import hashlib
import json
import re

from .llm import INTENT_MODEL, USAGE, structured_call
from .util import CACHE_DIR

DELETE_PATTERNS = [
    r"\bdelet", r"\beras", r"\bremov", r"\bclos(e|ing) (my|this|the) account", r"\bclose .*account",
    r"account.*\bclosed?\b", r"forget me", r"wipe", r"\bgone\b", r"still exist", r"fully removed",
    r"suppr", r"effac", r"elimin", r"borra", r"lösch", r"entfern", r"apag", r"exclu", r"cancel",
    r"rimuov", r"cancell", r"verwijder", r"usuń", r"usun", r"削除", r"退会", r"삭제", r"탈퇴", r"删除", r"注销",
    r"удал", r"sil(in|me)", r"hapus",
]
# Explicit statements that the person is NOT asking for erasure override any deletion keyword.
NEGATION_PATTERNS = [
    r"not (asking|requesting) (for )?(an? )?(deletion|erasure|removal)", r"not a request to (erase|delete|remove)",
    r"\b(do not|don't|dont|please don't|without) (close|closing|delete|deleting|remove|removing|eras)",
    r"\bstill (need|want|use)\b", r"\b(marketing|promotional|newsletter)\b.*\bonly\b",
    r"\bonly\b.*\b(marketing|promotional|newsletter)", r"stop (the|all)? ?(promotional|marketing) (emails|messages)",
    r"what personal information you hold", r"send me a copy", r"information request",
    r"reset(ting)? the login", r"\b(misspelled|typo)\b",
]
NON_DELETE_PATTERNS = [
    r"\bdownload my (account )?(information|data)\b", r"portable format", r"copy of last year",
    r"\binvoices?\b.*\brecords\b", r"\b(replace|update|change|correct)\b.*\b(number|phone|email address|address)\b",
    r"leave (my|the) (account|profile)", r"keep the account", r"continue using", r"leave the account active",
    r"\bunsubscribe\b", r"stop sending", r"opt[- ]out",
]


def keyword_intent(text: str) -> tuple[bool, str]:
    t = text.lower()
    neg = [p for p in NEGATION_PATTERNS if re.search(p, t)]
    if neg:
        return False, f"explicit non-deletion: {neg[0]}"
    non = [p for p in NON_DELETE_PATTERNS if re.search(p, t)]
    dele = [p for p in DELETE_PATTERNS if re.search(p, t)]
    if non and not re.search(r"\b(delete|erase|remove) (my|this|the) account", t):
        return False, f"non-deletion cue: {non[0]}"
    if dele:
        return True, f"deletion cue: {dele[0]}"
    return False, "no deletion cue"


INTENT_SYSTEM = (
    "You review privacy requests sent to an online shop. Decide whether the person is asking the shop "
    "to delete or erase their account / personal data (an erasure request). Requests to correct data, "
    "export or copy data, answer questions about held data, stop marketing, reset access, or that "
    "explicitly say they are not asking for deletion are NOT erasure requests. Requests that ask to verify "
    "or finish a deletion that was already requested ARE erasure requests. Requests may be in any language."
)
INTENT_SCHEMA = {
    "type": "object",
    "properties": {
        "deletion_authorized": {"type": "boolean"},
        "request_type": {"type": "string", "enum": [
            "ERASURE", "ACCESS_OR_EXPORT", "CORRECTION", "MARKETING_OPT_OUT", "ACCOUNT_SUPPORT", "OTHER"]},
        "rationale": {"type": "string"},
    },
    "required": ["deletion_authorized", "request_type", "rationale"],
    "additionalProperties": False,
}
INTENT_CACHE = CACHE_DIR / "intent_cache.json"
_cache: dict | None = None


def _load_cache() -> dict:
    global _cache
    if _cache is None:
        _cache = json.loads(INTENT_CACHE.read_text(encoding="utf-8")) if INTENT_CACHE.exists() else {}
    return _cache


def save_cache():
    if _cache is not None:
        INTENT_CACHE.parent.mkdir(parents=True, exist_ok=True)
        INTENT_CACHE.write_text(json.dumps(_cache, indent=1, ensure_ascii=False, sort_keys=True) + "\n",
                                encoding="utf-8")


def classify(text: str) -> tuple[bool, str]:
    """Claude classification (cached by text hash) with the keyword rules as offline fallback."""
    key = hashlib.sha256(text.encode("utf-8")).hexdigest()
    cache = _load_cache()
    if key in cache:
        USAGE.cache_hits["intent"] += 1
        e = cache[key]
        return e["deletion_authorized"], f"{e['source']}: {e['request_type']} - {e['rationale']}"
    result = structured_call(INTENT_MODEL, "intent", text, INTENT_SCHEMA, INTENT_SYSTEM, max_tokens=1024)
    if result is not None:
        rule, _ = keyword_intent(text)
        cache[key] = {**result, "source": INTENT_MODEL, "keyword_rules_agree": rule == result["deletion_authorized"]}
        return result["deletion_authorized"], f"{INTENT_MODEL}: {result['request_type']} - {result['rationale']}"
    return keyword_intent(text)
