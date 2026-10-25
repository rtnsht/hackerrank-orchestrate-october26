"""Shared helpers: paths, timestamps, identifier normalization."""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path

CODE_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = CODE_DIR.parent
DATASET_DIR = REPO_ROOT / "dataset"
CACHE_DIR = CODE_DIR / "cache"

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
USER_RE = re.compile(r"^user_\d+$")
MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def norm_ts(value: str | None) -> str | None:
    """Normalize ISO-8601 timestamps to YYYY-MM-DDTHH:MM:SSZ so string order == time order."""
    if not value:
        return None
    v = value.strip()
    try:
        return iso(datetime.fromisoformat(v.replace("Z", "+00:00")))
    except ValueError:
        return None


def parse_human_date(text: str, year: int | None = None) -> str | None:
    """Parse forms like '26 Apr 2026 02:00 UTC' or '25 Aug 04:03'."""
    m = re.search(r"(\d{1,2}) ([A-Za-z]{3})[a-z]* ?(\d{4})?(?: (\d{1,2}):(\d{2}))?", text)
    if not m:
        return None
    day, mon, yr, hh, mm = m.groups()
    yr = int(yr) if yr else year
    if not yr or mon.lower() not in MONTHS:
        return None
    return iso(datetime(yr, MONTHS[mon.lower()], int(day), int(hh or 0), int(mm or 0), tzinfo=timezone.utc))


def norm_email(value: str) -> str:
    return value.strip().lower()


def email_hash(email: str) -> str:
    return hashlib.sha256(norm_email(email).encode("utf-8")).hexdigest()


def norm_phone(value: str) -> str:
    digits = re.sub(r"\D", "", value)
    return "+" + digits if digits else ""


def norm_name(value: str) -> str:
    import unicodedata
    v = unicodedata.normalize("NFKD", value)
    v = "".join(c for c in v if not unicodedata.combining(c))
    return re.sub(r"[^a-z]", "", v.lower())


def is_null(value) -> bool:
    return value is None or (isinstance(value, str) and value.strip().lower() in ("", "null", "none", "-", "—"))


def identifier_tokens(value) -> set[str]:
    """Turn one raw field value into normalized identifier tokens.

    Tokens are prefixed by kind so emails, hashes, phones, names and user refs never collide:
    ``e:<email>``, ``h:<sha256>``, ``p:<+digits>``, ``u:<user_id>``, ``n:<name>``.
    """
    out: set[str] = set()
    if is_null(value) or not isinstance(value, (str, int)):
        return out
    s = str(value).strip()
    for em in EMAIL_RE.findall(s):
        out.add("e:" + norm_email(em))
    low = s.lower()
    if HASH_RE.match(low):
        out.add("h:" + low)
    if USER_RE.match(low):
        out.add("u:" + low)
    if s.startswith("+") and len(re.sub(r"\D", "", s)) >= 7:
        out.add("p:" + norm_phone(s))
    return out
