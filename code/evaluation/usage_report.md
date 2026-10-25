# Usage Report: Final Full-Dataset Run

## Where models are used

The audit core (parsing, identity resolution, timestamp attribution, state reconstruction, policy, step
planning) is deterministic Python and makes **no model calls**. Claude is used only at two edges, and both are
cached on disk:

| Purpose | Provider | Model | When it is called | Cache |
|---|---|---|---|---|
| Request intent (does the text authorize deletion?) | Anthropic | `claude-haiku-5-5` (`GHOST_INTENT_MODEL`) | once per **unique** request text not yet cached | `code/cache/intent_cache.json` (key: sha256 of text) |
| Screenshot / PDF extraction | Anthropic | `claude-sonnet-5-5` (`GHOST_VISION_MODEL`) | `python3 code/main.py --extract`, once per uncached PNG/PDF | `code/cache/evidence_extractions.json` |

Every run writes its live token accounting to `code/cache/usage_last_run.json` (from `response.usage`).

## Final run that produced `output.csv`

The final run was executed in the development container, which had **no `ANTHROPIC_API_KEY`**. It therefore
used the offline path:

* intent came from the negation-aware rule classifier (`ghost/intent.py::keyword_intent`);
* the 11 screenshots and 4 PDFs came from `code/cache/evidence_extractions.json`. Claude (the coding agent)
  produced these records with vision while reading the images during development, in the schema
  `ghost/extract.py` asks the API for.

| Model | Calls | Input tokens | Output tokens | Total tokens | Cost (USD) |
|---|---|---|---|---|---|
| `claude-haiku-5-5` | 0 | 0 | 0 | 0 | $0.00 |
| `claude-sonnet-5-5` | 0 | 0 | 0 | 0 | $0.00 |
| **Overall** | **0** | **0** | **0** | **0** | **$0.00** |

* Requests processed: 518
* Average tokens per request: 0
* Cost per request: $0.00
* Runtime: about 1 s on a laptop-class CPU for all 518 cases

## Expected cost of a run with Claude enabled

These are estimates for a cold-cache run with `ANTHROPIC_API_KEY` set, at Anthropic list prices: Haiku 5.5
$0.10 / $0.50 per MTok, Sonnet 5.5 $2 / $10 per MTok. Token counts are rough: about 600 input and 150 output
tokens per intent call (system prompt, text and schema, low effort), and about 2,200 input and 1,500 output
tokens per screenshot or PDF.

| Model | Calls | Input tokens | Output tokens | Cost (USD) |
|---|---|---|---|---|
| `claude-haiku-5-5` (intent, 375 unique texts across the 518 requests) | 375 | ~225,000 | ~56,000 | ~$0.05 |
| `claude-sonnet-5-5` (15 multimodal artifacts, one-time) | 15 | ~33,000 | ~22,500 | ~$0.29 |
| **Overall** | **390** | **~258,000** | **~78,500** | **~$0.34** |

That is about 650 tokens and $0.0007 per request on a cold cache. Warm-cache re-runs make zero calls.

## Why the usage is low

* Repeated texts are deduplicated: 518 requests contain only 375 unique texts.
* Images are extracted once, not once per case.
* The model never sees whole evidence files. A per-case LLM agent would have needed tens of millions of
  tokens per run (see the approach comparison in the README).
