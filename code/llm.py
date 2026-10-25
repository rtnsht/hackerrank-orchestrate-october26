"""OpenAI-compatible LLM client for intent classification and evidence assist.

Deterministic code still owns identity, evidence grounding, policy, GlobalAssigner,
step ordering, and steps_json. The model never invents resource_ids for output and
never writes steps_json.

Env (secrets never committed; never log key values):
  GHOST_DATA_LLM_API_KEY or OPENAI_API_KEY  — required to enable LLM
  GHOST_DATA_LLM_BASE_URL                   — default https://openrouter.ai/api/v1
  GHOST_DATA_LLM_MODEL                      — default openai/gpt-4o-mini (OpenRouter id)
  GHOST_DATA_LLM_CACHE_DIR                  — default <code>/.cache/llm
  GHOST_DATA_LLM_EVIDENCE                   — 1/true to assist messy PDF/text snippets
  GHOST_DATA_LLM_DISABLE                    — 1/true force regex-only even if key set
  GHOST_DATA_LLM_HTTP_REFERER / GHOST_DATA_LLM_APP_TITLE — optional OpenRouter headers
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, ValidationError, field_validator

# Approximate OpenRouter list prices for openai/gpt-4o-mini (USD / 1M tokens).
# Used only for estimated-cost reporting; adjust if GHOST_DATA_LLM_MODEL changes.
_DEFAULT_INPUT_COST_PER_M = 0.15
_DEFAULT_OUTPUT_COST_PER_M = 0.60
_DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
_DEFAULT_MODEL = "openai/gpt-4o-mini"

INTENT_SYSTEM = """You classify privacy/support request messages for a commerce platform.
Decide whether the writer is authorizing account/personal-data DELETION.

DELETION: clear ask to delete, erase, remove, close permanently, wipe, purge account
or personal data (any language). Includes auditing whether deletion completed.

NOT_DELETION: access/export/download copies, correction/change of name/email/phone,
marketing opt-out, promotional stop, or explicit refusal/negation of deletion
(e.g. "I do not want my account deleted", "not asking for deletion", "don't delete").

Return ONLY a JSON object matching the schema. Do not invent facts beyond the text."""

EVIDENCE_SYSTEM = """Extract candidate identifiers and resource ids mentioned in the
snippet. Candidates must be literally present in the text. Resource ids look like
account_123, support_45, analytics_9, audit_record_1, backup_2, data_flow_3,
export_4, search_5, tombstone_6. Identifiers are emails or user_NNN tokens.
Return ONLY JSON matching the schema. Prefer empty lists over guesses."""


class IntentResponse(BaseModel):
    intent: Literal["DELETION", "NOT_DELETION"]
    confidence: float = Field(ge=0.0, le=1.0)

    @field_validator("intent", mode="before")
    @classmethod
    def _norm_intent(cls, v: Any) -> Any:
        if isinstance(v, str):
            return v.strip().upper().replace(" ", "_")
        return v


class EvidenceMentions(BaseModel):
    candidate_identifiers: List[str] = Field(default_factory=list)
    candidate_resource_ids: List[str] = Field(default_factory=list)

    @field_validator("candidate_identifiers", "candidate_resource_ids", mode="before")
    @classmethod
    def _ensure_list(cls, v: Any) -> Any:
        if v is None:
            return []
        if isinstance(v, str):
            return [v]
        return v


INTENT_JSON_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "intent": {"type": "string", "enum": ["DELETION", "NOT_DELETION"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": ["intent", "confidence"],
    "additionalProperties": False,
}

EVIDENCE_JSON_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "candidate_identifiers": {
            "type": "array",
            "items": {"type": "string"},
        },
        "candidate_resource_ids": {
            "type": "array",
            "items": {"type": "string"},
        },
    },
    "required": ["candidate_identifiers", "candidate_resource_ids"],
    "additionalProperties": False,
}


@dataclass
class UsageStats:
    provider: str = "none"
    model: str = ""
    call_count: int = 0
    cache_hits: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: float = 0.0
    errors: int = 0
    fallbacks: int = 0
    by_purpose: Dict[str, int] = field(default_factory=dict)

    def record(
        self,
        *,
        purpose: str,
        input_tokens: int,
        output_tokens: int,
        cached: bool = False,
    ) -> None:
        if cached:
            self.cache_hits += 1
            return
        self.call_count += 1
        self.input_tokens += max(0, input_tokens)
        self.output_tokens += max(0, output_tokens)
        self.by_purpose[purpose] = self.by_purpose.get(purpose, 0) + 1
        self.estimated_cost_usd += (
            self.input_tokens_cost(input_tokens) + self.output_tokens_cost(output_tokens)
        )

    @staticmethod
    def input_tokens_cost(n: int) -> float:
        return (n / 1_000_000.0) * _DEFAULT_INPUT_COST_PER_M

    @staticmethod
    def output_tokens_cost(n: int) -> float:
        return (n / 1_000_000.0) * _DEFAULT_OUTPUT_COST_PER_M

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def as_dict(self) -> Dict[str, Any]:
        avg = (self.total_tokens / self.call_count) if self.call_count else 0.0
        cost_per = (
            self.estimated_cost_usd / self.call_count if self.call_count else 0.0
        )
        return {
            "provider": self.provider,
            "model": self.model,
            "call_count": self.call_count,
            "cache_hits": self.cache_hits,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "avg_tokens_per_call": avg,
            "estimated_cost_usd": round(self.estimated_cost_usd, 6),
            "estimated_cost_per_call_usd": round(cost_per, 8),
            "errors": self.errors,
            "fallbacks": self.fallbacks,
            "by_purpose": dict(self.by_purpose),
        }

    def reset(self) -> None:
        self.call_count = 0
        self.cache_hits = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.estimated_cost_usd = 0.0
        self.errors = 0
        self.fallbacks = 0
        self.by_purpose.clear()


def _truthy(val: Optional[str]) -> bool:
    return (val or "").strip().lower() in {"1", "true", "yes", "on"}


def _env_api_key() -> str:
    return (
        os.environ.get("GHOST_DATA_LLM_API_KEY", "").strip()
        or os.environ.get("OPENAI_API_KEY", "").strip()
    )


def _cache_key(purpose: str, model: str, text: str) -> str:
    h = hashlib.sha256()
    h.update(purpose.encode("utf-8"))
    h.update(b"\0")
    h.update(model.encode("utf-8"))
    h.update(b"\0")
    h.update(text.encode("utf-8"))
    return h.hexdigest()


class LLMClient:
    """Thin OpenAI-compatible chat client with schema validation and disk cache."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        cache_dir: Optional[Path] = None,
        timeout_s: float = 30.0,
        http_post=None,
        evidence_assist: Optional[bool] = None,
    ):
        self.api_key = (api_key if api_key is not None else _env_api_key()).strip()
        self.base_url = (
            base_url
            or os.environ.get("GHOST_DATA_LLM_BASE_URL", "").strip()
            or _DEFAULT_BASE_URL
        ).rstrip("/")
        self.model = (
            model
            or os.environ.get("GHOST_DATA_LLM_MODEL", "").strip()
            or _DEFAULT_MODEL
        )
        disabled = _truthy(os.environ.get("GHOST_DATA_LLM_DISABLE"))
        self.enabled = bool(self.api_key) and not disabled
        if evidence_assist is None:
            evidence_assist = _truthy(os.environ.get("GHOST_DATA_LLM_EVIDENCE"))
        self.evidence_assist = bool(evidence_assist)
        default_cache = Path(__file__).resolve().parent / ".cache" / "llm"
        env_cache = os.environ.get("GHOST_DATA_LLM_CACHE_DIR", "").strip()
        self.cache_dir = Path(env_cache) if env_cache else default_cache
        self.timeout_s = timeout_s
        self._http_post = http_post  # injectable for tests
        self.http_referer = os.environ.get("GHOST_DATA_LLM_HTTP_REFERER", "").strip()
        self.app_title = (
            os.environ.get("GHOST_DATA_LLM_APP_TITLE", "").strip()
            or "Ghost Data Still Exists"
        )
        provider = "none"
        if self.enabled:
            provider = (
                "openrouter"
                if "openrouter.ai" in self.base_url
                else "openai-compatible"
            )
        self.usage = UsageStats(
            provider=provider,
            model=self.model if self.enabled else "",
        )
        if self.enabled:
            self.cache_dir.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_env(cls) -> "LLMClient":
        return cls()

    def classify_intent(self, request_text: str) -> Optional[IntentResponse]:
        """Return validated intent or None to trigger deterministic fallback."""
        if not self.enabled:
            self.usage.fallbacks += 1
            return None
        text = (request_text or "").strip()
        if not text:
            self.usage.fallbacks += 1
            return None
        payload = self._cached_or_call(
            purpose="intent",
            system=INTENT_SYSTEM,
            user=f"Request text:\n{text}",
            schema_name="intent_response",
            schema=INTENT_JSON_SCHEMA,
            validate=IntentResponse.model_validate,
        )
        if payload is None:
            self.usage.fallbacks += 1
            return None
        try:
            return IntentResponse.model_validate(payload)
        except ValidationError:
            self.usage.errors += 1
            self.usage.fallbacks += 1
            return None

    def extract_evidence_mentions(self, snippet: str) -> Optional[EvidenceMentions]:
        """Optional assist: extract candidates; caller must ground against text."""
        if not self.enabled or not self.evidence_assist:
            return None
        text = (snippet or "").strip()
        if not text or len(text) < 40:
            return None
        # Cap very large PDFs for cost control.
        clipped = text if len(text) <= 6000 else text[:6000]
        payload = self._cached_or_call(
            purpose="evidence",
            system=EVIDENCE_SYSTEM,
            user=f"Snippet:\n{clipped}",
            schema_name="evidence_mentions",
            schema=EVIDENCE_JSON_SCHEMA,
            validate=EvidenceMentions.model_validate,
        )
        if payload is None:
            return None
        try:
            return EvidenceMentions.model_validate(payload)
        except ValidationError:
            self.usage.errors += 1
            return None

    def ground_evidence_mentions(
        self, snippet: str, mentions: EvidenceMentions
    ) -> EvidenceMentions:
        """Keep only candidates that literally appear in the snippet."""
        resource_re = re.compile(
            r"\b((?:account|search|support|export|analytics|backup|data_flow|"
            r"audit_record|tombstone|marketing|object|legal_hold)_\w+)\b",
            re.I,
        )
        email_re = re.compile(
            r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}", re.I
        )
        user_re = re.compile(r"\buser_\d+\b", re.I)
        text = snippet or ""
        text_l = text.lower()

        ids: List[str] = []
        for raw in mentions.candidate_identifiers:
            tok = (raw or "").strip()
            if not tok:
                continue
            if tok.lower() not in text_l and tok not in text:
                continue
            if email_re.fullmatch(tok) or user_re.fullmatch(tok):
                ids.append(tok)
            elif email_re.search(tok) or user_re.search(tok):
                m = email_re.search(tok) or user_re.search(tok)
                if m:
                    ids.append(m.group(0))

        rids: List[str] = []
        for raw in mentions.candidate_resource_ids:
            tok = (raw or "").strip()
            if not tok:
                continue
            if tok.lower() not in text_l and tok not in text:
                continue
            m = resource_re.fullmatch(tok) or resource_re.search(tok)
            if m:
                rids.append(m.group(1) if m.lastindex else m.group(0))

        # Deduplicate preserving order
        def uniq(seq: List[str]) -> List[str]:
            seen = set()
            out = []
            for x in seq:
                k = x.lower()
                if k in seen:
                    continue
                seen.add(k)
                out.append(x)
            return out

        return EvidenceMentions(
            candidate_identifiers=uniq(ids),
            candidate_resource_ids=uniq(rids),
        )

    def _cached_or_call(
        self,
        *,
        purpose: str,
        system: str,
        user: str,
        schema_name: str,
        schema: Dict[str, Any],
        validate=None,
    ) -> Optional[Dict[str, Any]]:
        key = _cache_key(purpose, self.model, user)
        cache_path = self.cache_dir / f"{purpose}_{key}.json"
        if cache_path.exists():
            try:
                data = json.loads(cache_path.read_text(encoding="utf-8"))
                if isinstance(data, dict) and "response" in data:
                    resp = data["response"]
                    if validate is not None:
                        validate(resp)
                    self.usage.record(
                        purpose=purpose,
                        input_tokens=0,
                        output_tokens=0,
                        cached=True,
                    )
                    return resp
            except (OSError, json.JSONDecodeError, ValidationError, ValueError, TypeError):
                pass

        result = self._chat_json(
            system=system,
            user=user,
            schema_name=schema_name,
            schema=schema,
            purpose=purpose,
        )
        if result is None:
            return None
        if validate is not None:
            try:
                validate(result)
            except (ValidationError, ValueError, TypeError):
                self.usage.errors += 1
                return None
        try:
            cache_path.write_text(
                json.dumps(
                    {
                        "purpose": purpose,
                        "model": self.model,
                        "response": result,
                        "cached_at": time.time(),
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except OSError:
            pass
        return result

    def _chat_json(
        self,
        *,
        system: str,
        user: str,
        schema_name: str,
        schema: Dict[str, Any],
        purpose: str,
    ) -> Optional[Dict[str, Any]]:
        url = f"{self.base_url}/chat/completions"
        body = {
            "model": self.model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": schema_name,
                    "strict": True,
                    "schema": schema,
                },
            },
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        # OpenRouter optional ranking / app attribution headers (no secrets).
        if "openrouter.ai" in self.base_url:
            if self.http_referer:
                headers["HTTP-Referer"] = self.http_referer
            if self.app_title:
                headers["X-Title"] = self.app_title
        try:
            if self._http_post is not None:
                status, resp_json = self._http_post(url, headers, body)
            else:
                status, resp_json = self._default_http_post(url, headers, body)
        except Exception:  # noqa: BLE001
            self.usage.errors += 1
            return None

        if status is None or status >= 400 or not isinstance(resp_json, dict):
            self.usage.errors += 1
            return None

        usage = resp_json.get("usage") or {}
        in_tok = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
        out_tok = int(
            usage.get("completion_tokens") or usage.get("output_tokens") or 0
        )
        self.usage.record(
            purpose=purpose, input_tokens=in_tok, output_tokens=out_tok
        )

        try:
            content = resp_json["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            self.usage.errors += 1
            return None

        if isinstance(content, list):
            # Some providers return content parts
            parts = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    parts.append(part.get("text") or "")
                elif isinstance(part, str):
                    parts.append(part)
            content = "".join(parts)

        if not isinstance(content, str):
            self.usage.errors += 1
            return None

        content = content.strip()
        if content.startswith("```"):
            # Strip optional markdown fences
            content = re.sub(r"^```(?:json)?\s*", "", content)
            content = re.sub(r"\s*```$", "", content)

        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            self.usage.errors += 1
            return None
        if not isinstance(parsed, dict):
            self.usage.errors += 1
            return None
        return parsed

    def _default_http_post(
        self, url: str, headers: Dict[str, str], body: Dict[str, Any]
    ):
        import httpx

        with httpx.Client(timeout=self.timeout_s) as client:
            resp = client.post(url, headers=headers, json=body)
            try:
                data = resp.json()
            except Exception:  # noqa: BLE001
                data = None
            return resp.status_code, data


# Process-wide singleton used by auditor / evidence.
_CLIENT: Optional[LLMClient] = None


def get_llm_client() -> LLMClient:
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = LLMClient.from_env()
    return _CLIENT


def reset_llm_client(client: Optional[LLMClient] = None) -> LLMClient:
    """Replace singleton (tests / explicit wiring)."""
    global _CLIENT
    _CLIENT = client if client is not None else LLMClient.from_env()
    return _CLIENT


def write_usage_markdown(path: Path, stats: UsageStats, *, extra_notes: str = "") -> None:
    """Write evaluation/usage_report.md from a completed run."""
    d = stats.as_dict()
    live = d["call_count"] > 0
    if live:
        status_blurb = (
            "Live OpenRouter path was exercised; totals below are measured "
            "(uncached API calls only; cache hits are listed separately)."
        )
    else:
        status_blurb = (
            "LLM path was **not** exercised this run (no API key / disabled / all "
            "fallback). Totals below are genuine zeros — **do not invent** token "
            "counts. Re-run with `GHOST_DATA_LLM_API_KEY` to fill real usage."
        )
    lines = [
        "# Usage Report — Ghost Data Still Exists",
        "",
        "## Scope",
        "",
        "- Full run: `dataset/deletion_requests.csv` → root `output.csv` (518 rows).",
        "- Intent classification prefers **OpenRouter** (OpenAI-compatible); regex is the fallback.",
        "- Deterministic core still owns identity, evidence grounding, policy,",
        "  GlobalAssigner, step ordering, and `steps_json` emission.",
        "",
        "## Runtime status",
        "",
        status_blurb,
        "",
        "## Model providers and names",
        "",
        "| Provider | Model | Role |",
        "| --- | --- | --- |",
        f"| {d['provider']} | {d['model'] or 'n/a'} | Intent (+ optional evidence assist) |",
        "",
        f"Configured defaults: base `{_DEFAULT_BASE_URL}`, model `{_DEFAULT_MODEL}`.",
        "",
        "## Model-call counts",
        "",
        "| Metric | Value |",
        "| --- | --- |",
        f"| API calls (uncached) | **{d['call_count']}** |",
        f"| Cache hits | {d['cache_hits']} |",
        f"| Errors | {d['errors']} |",
        f"| Deterministic fallbacks | {d['fallbacks']} |",
        f"| By purpose | `{json.dumps(d['by_purpose'])}` |",
        "",
        "## Tokens",
        "",
        "| Metric | Value |",
        "| --- | --- |",
        f"| Input tokens | {d['input_tokens']} |",
        f"| Output tokens | {d['output_tokens']} |",
        f"| Total tokens | {d['total_tokens']} |",
        f"| Average tokens / API call | {d['avg_tokens_per_call']:.1f} |",
        f"| Estimated total cost | **${d['estimated_cost_usd']:.6f}** |",
        f"| Estimated cost / API call | ${d['estimated_cost_per_call_usd']:.8f} |",
        "",
        "## Cost assumptions",
        "",
        f"- Input ${_DEFAULT_INPUT_COST_PER_M}/1M tokens, output ${_DEFAULT_OUTPUT_COST_PER_M}/1M tokens",
        f"  (OpenRouter `{_DEFAULT_MODEL}` list pricing estimate; adjust if model changes).",
        "",
        "## Environment",
        "",
        "- Provider: **OpenRouter** (OpenAI-compatible HTTP)",
        "- `GHOST_DATA_LLM_API_KEY` or `OPENAI_API_KEY` (never commit / never log)",
        f"- `GHOST_DATA_LLM_BASE_URL` (default `{_DEFAULT_BASE_URL}`)",
        f"- `GHOST_DATA_LLM_MODEL` (default `{_DEFAULT_MODEL}`)",
        "- `GHOST_DATA_LLM_CACHE_DIR` (default `code/.cache/llm`)",
        "- `GHOST_DATA_LLM_EVIDENCE=1` optional PDF/text mention assist",
        "- `GHOST_DATA_LLM_DISABLE=1` force regex-only",
        "",
        "## Keyed re-run recipe",
        "",
        "```bash",
        "export GHOST_DATA_LLM_API_KEY && python3 -m pip install -q -r code/requirements.txt && python3 code/main.py --sample && python3 code/main.py && echo \"OK: sample_score.json + output.csv + code/evaluation/usage_report.md refreshed\"",
        "```",
        "",
    ]
    if extra_notes:
        lines.extend(["## Notes", "", extra_notes.rstrip(), ""])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
