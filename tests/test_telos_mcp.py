"""MCP exposes distinct capabilities for observations and adjudication."""

import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def test_telos_tools_and_capability_isolation(tmp_path):
    async def listing(flags):
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "dao.mcp_server", "--db", str(tmp_path / "telos.db"), *flags],
            env=dict(os.environ),
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return {tool.name for tool in (await session.list_tools()).tools}

    base = asyncio.run(listing([]))
    assert {
        "register_relationship", "relationship", "evaluate_relationship",
        "relationship_coherence",
        "plan_relationship", "propose_relationship_action", "propose_temporal_action",
        "propose_evidence", "apply_observation",
        "list_observations",
    } <= base
    assert {"record_observation", "adjudicate_action", "review_outcome"}.isdisjoint(base)

    with_observation = asyncio.run(listing(["--allow-observation"]))
    assert "record_observation" in with_observation
    assert "adjudicate_action" not in with_observation

    with_adjudication = asyncio.run(listing(["--allow-adjudication"]))
    assert "record_observation" not in with_adjudication
    assert {"adjudicate_action", "review_outcome"} <= with_adjudication


def test_telos_mcp_evidence_roundtrip(tmp_path):
    async def run():
        params = StdioServerParameters(
            command=sys.executable,
            args=[
                "-m", "dao.mcp_server", "--db", str(tmp_path / "roundtrip.db"),
                "--allow-adjudication", "--allow-observation",
            ],
            env=dict(os.environ),
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                async def call(name, arguments):
                    response = await session.call_tool(name, arguments)
                    assert not response.isError, (name, response.content)
                    return json.loads(response.content[0].text)

                state = await call("read_state", {"branch": "main"})
                registered = await call("register_relationship", {
                    "branch": "main", "relationship_id": "reminders",
                    "subject": "user", "object": "reminders", "dimension": "interruptions",
                    "prior": {"enhancing": 0.6, "neutral": 0, "degrading": 0.4},
                    "expected_head": state["head"],
                })
                coherence = await call("relationship_coherence", {
                    "branch": "main", "relationship_ids": ["reminders"],
                })
                assert coherence["expected_count_ratio"] == 0.6
                decision = await call("evaluate_relationship", {
                    "branch": "main", "relationship_id": "reminders",
                    "actions": [{
                        "id": "engage", "cost": 1,
                        "utilities": {"enhancing": 8, "neutral": 0, "degrading": -12},
                    }],
                    "signals": [
                        {"id": "positive", "likelihoods": {
                            "enhancing": 0.8, "neutral": 0.5, "degrading": 0.2,
                        }},
                        {"id": "negative", "likelihoods": {
                            "enhancing": 0.2, "neutral": 0.5, "degrading": 0.8,
                        }},
                    ],
                    "wait_cost": 1,
                })
                assert decision["recommendation"]["kind"] == "wait"
                states = ("enhancing", "neutral", "degrading")
                identity = {
                    state: {other: int(other == state) for other in states}
                    for state in states
                }
                temporal = await call("plan_relationship", {
                    "branch": "main", "relationship_id": "reminders",
                    "actions": [{
                        "id": "engage", "kind": "act",
                        "utilities": {"enhancing": 8, "neutral": 0, "degrading": -12},
                        "cost": 1, "resource_cost": 0,
                        "transitions": identity,
                        "observations": {
                            state: {"seen": 1} for state in states
                        },
                    }],
                    "horizon": 1, "budget": 0,
                })
                assert temporal["belief_head"] == registered["head"]
                assert temporal["first_action"] is None
                proposal = await call("propose_evidence", {
                    "branch": "main", "problem": decision["inputs"],
                    "evidence_plan": {
                        "relationship_id": "reminders", "source": "user feedback",
                        "question": "Will reminders help?",
                        "deadline": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
                        "max_cost_usd": 0,
                    },
                    "expected_head": registered["head"],
                })
                await call("adjudicate_action", {
                    "proposal_id": proposal["id"], "verdict": "approved",
                    "actor": "operator", "reason": "reviewed evidence plan",
                })
                execution = await call("execute_action", {
                    "proposal_id": proposal["id"], "expected_head": proposal["base"],
                })
                observation_args = {
                    "relationship_id": "reminders", "signal": "positive",
                    "likelihoods": {"enhancing": 0.8, "neutral": 0.5, "degrading": 0.2},
                    "source": "user feedback", "actor": "operator",
                    "source_event_id": "reply-1", "proposal_id": proposal["id"],
                }
                observation = await call("record_observation", observation_args)
                replay = await call("record_observation", observation_args)
                assert replay["id"] == observation["id"]
                applied = await call("apply_observation", {
                    "branch": "main", "observation_id": observation["id"],
                    "expected_head": execution["commit"],
                })
                assert applied["relationship"]["belief"]["enhancing"] > 0.85
                await call("review_outcome", {
                    "proposal_id": proposal["id"], "observation_ids": [observation["id"]],
                    "assessment": "supported", "actor": "operator",
                    "reason": "source reported a positive signal",
                })
                temporal_action = await call("propose_temporal_action", {
                    "branch": "main", "relationship_id": "reminders",
                    "actions": [{
                        "id": "remember_feedback", "kind": "act",
                        "utilities": {"enhancing": 10, "neutral": 0, "degrading": 0},
                        "cost": 0, "resource_cost": 0,
                        "transitions": identity,
                        "observations": {
                            state: {"unseen": 1} for state in states
                        },
                    }],
                    "patch": {"set": {"feedback": "positive"}},
                    "horizon": 1, "budget": 0,
                    "expected_head": applied["head"],
                })
                await call("adjudicate_action", {
                    "proposal_id": temporal_action["id"], "verdict": "approved",
                    "actor": "operator", "reason": "reviewed local memory update",
                })
                await call("execute_action", {
                    "proposal_id": temporal_action["id"],
                    "expected_head": temporal_action["base"],
                })
                integrity = await call("verify_integrity", {})
                assert integrity["ok"], integrity["issues"]

    asyncio.run(run())
