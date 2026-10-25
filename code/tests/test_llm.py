"""Unit tests for the OpenAI-compatible LLM client (mocked HTTP)."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from auditor import is_deletion_request, is_deletion_request_regex
from llm import EvidenceMentions, IntentResponse, LLMClient, reset_llm_client


class TestIntentRegexFallback(unittest.TestCase):
    def test_multilingual_deletion(self):
        self.assertTrue(is_deletion_request_regex("Please delete my account."))
        self.assertTrue(
            is_deletion_request_regex(
                "Je demande la suppression définitive du compte."
            )
        )
        self.assertTrue(
            is_deletion_request_regex(
                "このメールアドレスに関連するアカウントの完全な削除を依頼します。"
            )
        )

    def test_negated_and_non_deletion(self):
        self.assertFalse(
            is_deletion_request_regex(
                "Please stop the promotional emails. I still need the account "
                "and my order history, so I am not asking for deletion."
            )
        )
        self.assertFalse(
            is_deletion_request_regex(
                "Could you send me a copy of the information held on my account? "
                "I still use it, so please don't delete or change anything."
            )
        )
        self.assertFalse(
            is_deletion_request_regex(
                "I do not want my account deleted; just stop marketing emails."
            )
        )
        self.assertFalse(
            is_deletion_request_regex(
                "I'd like to download my account information in a portable format."
            )
        )
        self.assertFalse(
            is_deletion_request_regex(
                "Please change the email address used for this account."
            )
        )


class TestLLMClientMock(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()
        reset_llm_client(LLMClient(api_key="", cache_dir=self.cache))

    def test_disabled_without_key(self):
        client = LLMClient(api_key="", cache_dir=self.cache)
        self.assertFalse(client.enabled)
        self.assertIsNone(client.classify_intent("Please delete my account"))
        self.assertEqual(client.usage.call_count, 0)
        self.assertGreaterEqual(client.usage.fallbacks, 1)

    def test_schema_validated_intent_and_cache(self):
        calls = {"n": 0}

        def fake_post(url, headers, body):
            calls["n"] += 1
            self.assertIn("/chat/completions", url)
            self.assertEqual(headers.get("Authorization"), "Bearer test-key")
            return 200, {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {"intent": "NOT_DELETION", "confidence": 0.91}
                            )
                        }
                    }
                ],
                "usage": {"prompt_tokens": 120, "completion_tokens": 20},
            }

        client = LLMClient(
            api_key="test-key",
            model="gpt-4o-mini",
            cache_dir=self.cache,
            http_post=fake_post,
        )
        r1 = client.classify_intent(
            "I am not asking for deletion; just stop promotional emails."
        )
        self.assertIsInstance(r1, IntentResponse)
        self.assertEqual(r1.intent, "NOT_DELETION")
        self.assertEqual(client.usage.call_count, 1)
        self.assertEqual(client.usage.input_tokens, 120)
        self.assertEqual(client.usage.output_tokens, 20)

        r2 = client.classify_intent(
            "I am not asking for deletion; just stop promotional emails."
        )
        self.assertEqual(r2.intent, "NOT_DELETION")
        self.assertEqual(calls["n"], 1)  # second hit served from cache
        self.assertEqual(client.usage.call_count, 1)
        self.assertEqual(client.usage.cache_hits, 1)

    def test_invalid_schema_triggers_fallback(self):
        def fake_post(url, headers, body):
            return 200, {
                "choices": [
                    {"message": {"content": json.dumps({"intent": "MAYBE"})}}
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            }

        client = LLMClient(
            api_key="test-key", cache_dir=self.cache, http_post=fake_post
        )
        self.assertIsNone(client.classify_intent("delete me"))
        # Wired auditor path falls back to regex
        reset_llm_client(client)
        self.assertTrue(is_deletion_request("Please delete my account", llm=client))

    def test_http_error_fallback(self):
        def fake_post(url, headers, body):
            return 500, {"error": "boom"}

        client = LLMClient(
            api_key="test-key", cache_dir=self.cache, http_post=fake_post
        )
        self.assertIsNone(client.classify_intent("erase everything"))
        self.assertGreaterEqual(client.usage.errors, 1)

    def test_evidence_grounding_rejects_hallucinated_ids(self):
        client = LLMClient(api_key="", cache_dir=self.cache)
        mentions = EvidenceMentions(
            candidate_identifiers=["alice@example.com", "ghost@nowhere.test"],
            candidate_resource_ids=["account_001", "account_999"],
        )
        snippet = "Customer alice@example.com linked to account_001 in support notes."
        grounded = client.ground_evidence_mentions(snippet, mentions)
        self.assertEqual(grounded.candidate_identifiers, ["alice@example.com"])
        self.assertEqual(grounded.candidate_resource_ids, ["account_001"])

    def test_llm_intent_overrides_regex_when_valid(self):
        def fake_post(url, headers, body):
            return 200, {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {"intent": "NOT_DELETION", "confidence": 0.88}
                            )
                        }
                    }
                ],
                "usage": {"prompt_tokens": 50, "completion_tokens": 10},
            }

        client = LLMClient(
            api_key="test-key", cache_dir=self.cache, http_post=fake_post
        )
        # Text contains "delete" so regex alone would say True; LLM says no.
        text = "Please audit whether anything was deleted, but do not remove my account."
        # Strengthen: explicit non-deletion via LLM even if regex is unsure
        self.assertFalse(is_deletion_request(text, llm=client))


if __name__ == "__main__":
    unittest.main()
