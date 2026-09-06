import json
import unittest

import freemodels_web2api as adapter


class AdapterTests(unittest.TestCase):
    def test_model_aliases_are_canonicalized(self):
        self.assertEqual(adapter.resolve_model("terra"), ("gpt-5.6-terra", "terra"))
        self.assertEqual(adapter.resolve_model("gpt-5.6-sol"), ("gpt-5.6-sol", "sol"))

    def test_unknown_model_is_rejected(self):
        with self.assertRaises(adapter.RequestError):
            adapter.resolve_model("definitely-not-a-model")

    def test_build_upstream_request(self):
        model, payload = adapter.build_upstream_request({
            "model": "gpt-5.6-terra",
            "messages": [{"role": "user", "content": "hello"}],
            "thinking": True,
            "deep_search": True,
        })
        self.assertEqual(model, "gpt-5.6-terra")
        self.assertEqual(payload["modelId"], "terra")
        self.assertTrue(payload["thinking"])
        self.assertTrue(payload["deepSearch"])
        self.assertTrue(payload["stream"])

    def test_parse_sse_and_collect_completion(self):
        source = [
            ": PROCESSING",
            "",
            'data: {"id":"gen-test","choices":[{"delta":{"reasoning":"think "},"finish_reason":null}]}',
            "",
            'data: {"id":"gen-test","choices":[{"delta":{"content":"O"},"finish_reason":null}]}',
            "",
            'data: {"id":"gen-test","choices":[{"delta":{"content":"K"},"finish_reason":"stop"}],"usage":{"total_tokens":3}}',
            "",
            "data: [DONE]",
            "",
        ]
        items = list(adapter.parse_sse_data(source))
        response = adapter.collect_completion(items, "gpt-5.6-terra")
        self.assertEqual(response["id"], "gen-test")
        self.assertEqual(response["model"], "gpt-5.6-terra")
        self.assertEqual(response["choices"][0]["message"]["content"], "OK")
        self.assertEqual(response["choices"][0]["message"]["reasoning"], "think ")
        self.assertEqual(response["usage"]["total_tokens"], 3)

    def test_chunk_normalization_sets_requested_model(self):
        chunk = adapter.normalize_chunk(
            {"id": "gen-test", "choices": [{"delta": {"content": "x"}}]},
            "glm-5.2",
        )
        self.assertEqual(chunk["model"], "glm-5.2")
        self.assertEqual(chunk["object"], "chat.completion.chunk")


if __name__ == "__main__":
    unittest.main()
