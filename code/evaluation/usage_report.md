# Usage Report — Ghost Data Still Exists

## Scope

- Full run: `dataset/deletion_requests.csv` → root `output.csv` (518 rows).
- Intent classification prefers **OpenRouter** (OpenAI-compatible); regex is the fallback.
- Deterministic core still owns identity, evidence grounding, policy,
  GlobalAssigner, step ordering, and `steps_json` emission.

## Runtime status

LLM path was **not** exercised this run (no API key / disabled / all fallback). Totals below are genuine zeros — **do not invent** token counts. Re-run with `GHOST_DATA_LLM_API_KEY` to fill real usage.

## Model providers and names

| Provider | Model | Role |
| --- | --- | --- |
| none | n/a | Intent (+ optional evidence assist) |

Configured defaults: base `https://openrouter.ai/api/v1`, model `openai/gpt-4o-mini`.

## Model-call counts

| Metric | Value |
| --- | --- |
| API calls (uncached) | **0** |
| Cache hits | 0 |
| Errors | 0 |
| Deterministic fallbacks | 1036 |
| By purpose | `{}` |

## Tokens

| Metric | Value |
| --- | --- |
| Input tokens | 0 |
| Output tokens | 0 |
| Total tokens | 0 |
| Average tokens / API call | 0.0 |
| Estimated total cost | **$0.000000** |
| Estimated cost / API call | $0.00000000 |

## Cost assumptions

- Input $0.15/1M tokens, output $0.6/1M tokens
  (OpenRouter `openai/gpt-4o-mini` list pricing estimate; adjust if model changes).

## Environment

- Provider: **OpenRouter** (OpenAI-compatible HTTP)
- `GHOST_DATA_LLM_API_KEY` or `OPENAI_API_KEY` (never commit / never log)
- `GHOST_DATA_LLM_BASE_URL` (default `https://openrouter.ai/api/v1`)
- `GHOST_DATA_LLM_MODEL` (default `openai/gpt-4o-mini`)
- `GHOST_DATA_LLM_CACHE_DIR` (default `code/.cache/llm`)
- `GHOST_DATA_LLM_EVIDENCE=1` optional PDF/text mention assist
- `GHOST_DATA_LLM_DISABLE=1` force regex-only

## Keyed re-run recipe

```bash
export GHOST_DATA_LLM_API_KEY && python3 -m pip install -q -r code/requirements.txt && python3 code/main.py --sample && python3 code/main.py && echo "OK: sample_score.json + output.csv + code/evaluation/usage_report.md refreshed"
```

## Notes

Requests file: `deletion_requests.csv`. LLM enabled at runtime: False. OCR images: False. This run used regex intent fallback (0 API calls). Re-run with GHOST_DATA_LLM_API_KEY to populate non-zero usage.
