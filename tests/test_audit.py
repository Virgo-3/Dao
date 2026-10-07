import copy
import hashlib
import json
import math
import unittest

from dao.audit import adjudicate


def supported_payload():
    return {"claim": "The release passed its acceptance tests.", "evidence": [
        {"source": "test report", "content": "All acceptance checks passed.", "stance": "support", "reliability": 0.9},
        {"source": "review report", "content": "The review confirms the checks.", "stance": "support", "reliability": 0.8},
    ]}


class AuditTests(unittest.TestCase):
    def test_supported_requires_count_and_threshold(self):
        result = adjudicate(supported_payload())
        self.assertEqual(result["verdict"], "supported")
        self.assertTrue(result["allowed"])
        self.assertEqual(result["metrics"]["supporting_source_count"], 2)
        self.assertAlmostEqual(result["metrics"]["support_score"], 0.85)

    def test_contradiction_vetoes_even_with_zero_reliability(self):
        for reliability in (0, 0.9):
            payload = supported_payload()
            payload["evidence"].append({"source": "failure log", "content": "A required check failed.",
                                        "stance": "contradict", "reliability": reliability})
            result = adjudicate(payload)
            self.assertEqual(result["verdict"], "contested")
            self.assertFalse(result["allowed"])
            self.assertEqual(result["metrics"]["contradiction_count"], 1)

    def test_duplicate_source_labels_do_not_inflate_support(self):
        payload = supported_payload()
        payload["evidence"][1]["source"] = " TEST REPORT "
        result = adjudicate(payload)
        self.assertEqual(result["verdict"], "insufficient")
        self.assertEqual(result["metrics"]["supporting_source_count"], 1)
        self.assertEqual(result["metrics"]["support_score"], 0.8)

    def test_neutral_sources_do_not_count_as_support(self):
        payload = supported_payload()
        payload["evidence"][1]["stance"] = "neutral"
        result = adjudicate(payload)
        self.assertFalse(result["allowed"])
        self.assertEqual(result["metrics"]["neutral_count"], 1)
        self.assertEqual(result["metrics"]["supporting_source_count"], 1)

    def test_reliability_threshold_and_exact_boundary(self):
        payload = supported_payload()
        payload["threshold"] = 0.86
        self.assertEqual(adjudicate(payload)["verdict"], "insufficient")
        payload["threshold"] = 0.85
        self.assertTrue(adjudicate(payload)["allowed"])

    def test_empty_evidence_is_insufficient_even_at_threshold_zero(self):
        result = adjudicate({"claim": "Ready", "evidence": [], "threshold": 0})
        self.assertEqual(result["verdict"], "insufficient")
        self.assertFalse(result["allowed"])

    def test_hash_covers_exact_normalized_payload_and_changes_with_evidence(self):
        payload = supported_payload()
        before = copy.deepcopy(payload)
        first = adjudicate(payload)
        canonical = json.dumps(first["normalized_payload"], sort_keys=True, separators=(",", ":"),
                               ensure_ascii=False, allow_nan=False)
        self.assertEqual(first["verdict_id"], hashlib.sha256(canonical.encode()).hexdigest())
        self.assertEqual(first["verdict_id"], adjudicate(payload)["verdict_id"])
        self.assertEqual(payload, before)
        payload["evidence"][0]["content"] += " New detail."
        self.assertNotEqual(first["verdict_id"], adjudicate(payload)["verdict_id"])

    def test_hash_uses_defaults_and_preserves_evidence_order(self):
        payload = supported_payload()
        first = adjudicate(payload)
        payload.update(threshold=0.75, require_sources=2)
        self.assertEqual(first["verdict_id"], adjudicate(payload)["verdict_id"])
        payload["evidence"].reverse()
        self.assertNotEqual(first["verdict_id"], adjudicate(payload)["verdict_id"])

    def test_rejects_invalid_reliability_and_threshold(self):
        for value in (True, math.nan, math.inf, -math.inf, -0.1, 1.1, "0.9", 10 ** 1000):
            for location in ("reliability", "threshold"):
                with self.subTest(value=str(value)[:30], location=location):
                    payload = supported_payload()
                    if location == "threshold":
                        payload["threshold"] = value
                    else:
                        payload["evidence"][0]["reliability"] = value
                    with self.assertRaises(ValueError):
                        adjudicate(payload)

    def test_rejects_invalid_required_count_text_and_stance(self):
        for value in (True, 0, -1, 2.0, "2"):
            payload = supported_payload()
            payload["require_sources"] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                adjudicate(payload)
        changes = [lambda p: p.update(claim=" "),
                   lambda p: p.update(evidence="report"),
                   lambda p: p["evidence"][0].update(source=""),
                   lambda p: p["evidence"][0].update(content=" "),
                   lambda p: p["evidence"][0].update(stance="verified"),
                   lambda p: p.update(factual_proof=True)]
        for change in changes:
            payload = supported_payload()
            change(payload)
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                adjudicate(payload)


if __name__ == "__main__":
    unittest.main()
