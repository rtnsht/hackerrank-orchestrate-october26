# Usage Report — Ghost Data Still Exists

## Scope

- Full run: `dataset/deletion_requests.csv` → root `output.csv` (518 rows).
- Intent classification prefers an OpenAI-compatible LLM; regex is the fallback.
- Deterministic core still owns identity, evidence grounding, policy,
  GlobalAssigner, step ordering, and `steps_json` emission.

## Model providers and names

| Provider | Model | Role |
| --- | --- | --- |
| none | n/a | Intent (+ optional evidence assist) |

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
  (gpt-4o-mini list pricing; adjust if using another model).

## Environment

- `GHOST_DATA_LLM_API_KEY` or `OPENAI_API_KEY`
- `GHOST_DATA_LLM_BASE_URL` (default `https://api.openai.com/v1`)
- `GHOST_DATA_LLM_MODEL` (default `gpt-4o-mini`)
- `GHOST_DATA_LLM_CACHE_DIR` (default `code/.cache/llm`)
- `GHOST_DATA_LLM_EVIDENCE=1` optional PDF/text mention assist
- `GHOST_DATA_LLM_DISABLE=1` force regex-only

Set `GHOST_DATA_LLM_API_KEY` (or `OPENAI_API_KEY`) to enable live calls. Without a key the pipeline uses the deterministic regex intent fallback.

## Notes

Requests file: `deletion_requests.csv`. LLM enabled at runtime: False. OCR images: False. This run used regex intent fallback (0 API calls). Re-run with GHOST_DATA_LLM_API_KEY to populate non-zero usage.
