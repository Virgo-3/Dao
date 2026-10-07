"""Audit as adjudication of submitted evidence, never fabricated verification.

The rule is deliberately inspectable: every contradiction contests the claim;
otherwise enough distinct supporting sources must meet a reliability threshold.
Reliability is caller supplied. Multiple entries from one source count once,
at that source's lowest supporting reliability, to prevent duplicate inflation.
Passing this gate is permission under this rule, not proof of the claim.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any


def _object(value: Any, field: str, allowed: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    if any(not isinstance(key, str) for key in value):
        raise ValueError(f"{field} field names must be strings")
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(f"{field} contains unknown fields: {', '.join(sorted(unknown))}")
    return value


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a nonempty string")
    return value.strip()


def _unit_number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a finite number between 0 and 1")
    try:
        number = float(value)
    except (OverflowError, ValueError) as exc:
        raise ValueError(f"{field} must be a finite number between 0 and 1") from exc
    if not math.isfinite(number) or not 0 <= number <= 1:
        raise ValueError(f"{field} must be a finite number between 0 and 1")
    return number


def _normalize(payload: Any) -> dict[str, Any]:
    payload = _object(payload, "audit", {"claim", "evidence", "threshold", "require_sources"})
    claim = _text(payload.get("claim"), "claim")
    raw_evidence = payload.get("evidence")
    if not isinstance(raw_evidence, list):
        raise ValueError("evidence must be an array")
    evidence = []
    for index, raw in enumerate(raw_evidence):
        item = _object(raw, f"evidence[{index}]", {"source", "content", "stance", "reliability"})
        stance = item.get("stance")
        if not isinstance(stance, str) or stance not in {"support", "contradict", "neutral"}:
            raise ValueError(f"evidence[{index}].stance must be support, contradict, or neutral")
        evidence.append({
            "source": _text(item.get("source"), f"evidence[{index}].source"),
            "content": _text(item.get("content"), f"evidence[{index}].content"),
            "stance": stance,
            "reliability": _unit_number(item.get("reliability"), f"evidence[{index}].reliability"),
        })
    require_sources = payload.get("require_sources", 2)
    if isinstance(require_sources, bool) or not isinstance(require_sources, int) or require_sources < 1:
        raise ValueError("require_sources must be a positive integer")
    return {"claim": claim, "evidence": evidence,
            "threshold": _unit_number(payload.get("threshold", 0.75), "threshold"),
            "require_sources": require_sources}


def adjudicate(payload: Any) -> dict[str, Any]:
    """Return a deterministic verdict and SHA-256 of the normalized input.

    Sources are distinct by trimmed, casefolded label. The score is the mean
    of per-source minimum supporting reliability. Neutral evidence cannot
    support a claim. Any contradiction vetoes approval, even at reliability 0,
    because unresolved contradictory submissions require adjudication rather
    than being silently discarded. Evidence order is retained in the digest.
    """
    model = _normalize(payload)
    canonical = json.dumps(model, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    verdict_id = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    support_sources: dict[str, float] = {}
    contradictions = []
    neutral_count = 0
    for item in model["evidence"]:
        source_key = item["source"].casefold()
        if item["stance"] == "support":
            support_sources[source_key] = min(support_sources.get(source_key, 1.0), item["reliability"])
        elif item["stance"] == "contradict":
            contradictions.append(item)
        else:
            neutral_count += 1
    support_score = (math.fsum(support_sources.values()) / len(support_sources)
                     if support_sources else 0.0)
    source_count = len(support_sources)
    enough_sources = source_count >= model["require_sources"]
    enough_reliability = support_score >= model["threshold"]
    reasons = []
    if contradictions:
        verdict = "contested"
        reasons.append("Contradictory evidence is unresolved and vetoes approval under this rule.")
    elif enough_sources and enough_reliability:
        verdict = "supported"
        reasons.append("Distinct supporting sources meet both the source-count and reliability requirements.")
    else:
        verdict = "insufficient"
    if not enough_sources:
        reasons.append(f"Only {source_count} distinct supporting sources were submitted; {model['require_sources']} are required.")
    if not enough_reliability:
        reasons.append(f"Supporting reliability {support_score:.3f} is below the required {model['threshold']:.3f}.")
    return {
        "verdict_id": verdict_id,
        "claim": model["claim"],
        "verdict": verdict,
        "allowed": verdict == "supported",
        "reasons": reasons,
        "metrics": {
            "evidence_count": len(model["evidence"]),
            "distinct_source_count": len({item["source"].casefold() for item in model["evidence"]}),
            "supporting_source_count": source_count,
            "support_score": support_score,
            "contradiction_count": len(contradictions),
            "neutral_count": neutral_count,
            "threshold": model["threshold"],
            "require_sources": model["require_sources"],
        },
        "normalized_payload": model,
        "assumptions": [
            "Evidence content, stance, source labels, and reliability are caller supplied; this rule does not verify their truth.",
            "Distinct labels do not prove source independence; sources are compared after trimming and casefolding.",
            "Supporting reliability is the mean of each distinct source's minimum submitted supporting reliability.",
            "Every contradiction vetoes approval; neutral evidence supplies no support.",
            "A supported verdict authorizes the local policy gate and is not factual proof or external action authorization.",
        ],
    }
