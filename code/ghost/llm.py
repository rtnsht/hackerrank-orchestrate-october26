"""Thin Claude client wrapper: structured JSON calls, disk cache helpers and token/cost accounting.

The pipeline only calls Claude where code is weak (multilingual intent, screenshot/PDF
extraction). Every call is cached on disk, so re-runs are deterministic and free.
Credentials come from the environment (ANTHROPIC_API_KEY or a local .env), never from code.
"""
from __future__ import annotations

import base64
import json
import os
from collections import defaultdict

from .util import CACHE_DIR, REPO_ROOT

INTENT_MODEL = os.environ.get("GHOST_INTENT_MODEL", "claude-haiku-5-5")
VISION_MODEL = os.environ.get("GHOST_VISION_MODEL", "claude-sonnet-5-5")
# USD per 1M tokens (input, output) - Anthropic first-party list prices
PRICING = {
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
}
USAGE_PATH = CACHE_DIR / "usage_last_run.json"


def _load_dotenv():
    env = REPO_ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


class Usage:
    def __init__(self):
        self.models = defaultdict(lambda: {"calls": 0, "input_tokens": 0, "output_tokens": 0, "purpose": set()})
        self.cache_hits = defaultdict(int)

    def record(self, model: str, purpose: str, response):
        m = self.models[model]
        m["calls"] += 1
        m["input_tokens"] += response.usage.input_tokens
        m["output_tokens"] += response.usage.output_tokens
        m["purpose"].add(purpose)

    def summary(self, requests: int) -> dict:
        out = {"requests": requests, "models": {}, "cache_hits": dict(self.cache_hits)}
        tot_in = tot_out = tot_calls = 0
        tot_cost = 0.0
        for model, m in sorted(self.models.items()):
            pin, pout = PRICING.get(model, (0.0, 0.0))
            cost = m["input_tokens"] / 1e6 * pin + m["output_tokens"] / 1e6 * pout
            out["models"][model] = {**m, "purpose": sorted(m["purpose"]), "cost_usd": round(cost, 6)}
            tot_in, tot_out, tot_calls, tot_cost = (tot_in + m["input_tokens"], tot_out + m["output_tokens"],
                                                    tot_calls + m["calls"], tot_cost + cost)
        out["total"] = {"calls": tot_calls, "input_tokens": tot_in, "output_tokens": tot_out,
                        "tokens": tot_in + tot_out, "cost_usd": round(tot_cost, 6),
                        "tokens_per_request": round((tot_in + tot_out) / requests, 2) if requests else 0,
                        "cost_per_request_usd": round(tot_cost / requests, 8) if requests else 0}
        return out

    def save(self, requests: int):
        USAGE_PATH.parent.mkdir(parents=True, exist_ok=True)
        USAGE_PATH.write_text(json.dumps(self.summary(requests), indent=2) + "\n", encoding="utf-8")


USAGE = Usage()
_client = None
_client_checked = False


def client():
    """Anthropic client when credentials are configured, else None (offline mode)."""
    global _client, _client_checked
    if _client_checked:
        return _client
    _client_checked = True
    if os.environ.get("GHOST_OFFLINE") == "1":
        return None
    _load_dotenv()
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        return None
    try:
        import anthropic
        _client = anthropic.Anthropic()
    except Exception:
        _client = None
    return _client


def structured_call(model: str, purpose: str, content, schema: dict, system: str, max_tokens: int = 2048,
                    effort: str = "low") -> dict | None:
    """One Messages API call constrained to a JSON schema. Returns parsed JSON or None on failure."""
    c = client()
    if c is None:
        return None
    import anthropic
    try:
        resp = c.messages.create(
            model=model, max_tokens=max_tokens, system=system,
            messages=[{"role": "user", "content": content}],
            output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
        )
    except (anthropic.APIStatusError, anthropic.APIConnectionError) as exc:
        print(f"[llm] {purpose}: {type(exc).__name__}: {exc}")
        return None
    USAGE.record(model, purpose, resp)
    if resp.stop_reason == "refusal":
        return None
    text = next((b.text for b in resp.content if b.type == "text"), None)
    try:
        return json.loads(text) if text else None
    except json.JSONDecodeError:
        return None


def image_block(path) -> dict:
    data = base64.standard_b64encode(path.read_bytes()).decode("utf-8")
    return {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}}


def pdf_block(path) -> dict:
    data = base64.standard_b64encode(path.read_bytes()).decode("utf-8")
    return {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": data}}
