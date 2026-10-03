"""CLI and actual MCP subprocesses exercise the shared objective lifecycle."""

import asyncio
import json
import os
import subprocess
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from dao.agent import memory_problem
from dao.projection import project_state
from dao.store import Store
from dao.work import Coordinator


def test_cli_objective_plan_review_and_completion(tmp_path):
    path = tmp_path / "state.db"

    def cli(*arguments):
        result = subprocess.run([sys.executable, "-m", "dao.cli", "--db", str(path), *arguments],
                                capture_output=True, text=True, check=True)
        return json.loads(result.stdout)

    def save(name, content):
        filename = tmp_path / name
        filename.write_text(json.dumps(content), encoding="utf-8")
        return str(filename)

    success = save("success.json", {"goal": "done"})
    problem = save("problem.json", memory_problem("explicit goal"))
    patch = save("patch.json", {"set": {"goal": "done"}})
    created = cli("run", "Record goal", "--success", success, "--no-advance", "--json")
    planned = cli("work", "plan", created["id"], problem, patch, "--expected-revision", "1")
    assert planned["status"] == "awaiting_approval"
    completed = cli("work", "adjudicate", created["id"], "approved", "--actor", "operator",
                    "--reason", "Reviewed exact goal", "--expected-revision", str(planned["revision"]))
    assert completed["status"] == "completed"
    assert cli("work", "inspect", created["id"])["success_currently_satisfied"]
    assert cli("work", "history", created["id"])[-1]["status"] == "completed"
    assert cli("verify")["ok"]


def test_cli_external_artifact_requires_explicit_installation(tmp_path):
    path, directory = tmp_path / "state.db", tmp_path / "artifacts"

    def cli(*arguments):
        result = subprocess.run([sys.executable, "-m", "dao.cli", "--db", str(path), *arguments],
                                capture_output=True, text=True, check=True)
        return json.loads(result.stdout)

    problem, effect = tmp_path / "problem.json", tmp_path / "effect.json"
    problem.write_text(json.dumps(memory_problem("explicit artifact")), encoding="utf-8")
    effect.write_text(json.dumps({"executor": "artifact", "arguments": {"content": "report"},
                                 "preconditions": {"absent": True}, "reservation_usd": "0"}),
                      encoding="utf-8")
    created = cli("run", "Produce artifact", "--no-advance", "--json")
    cli("work", "plan", created["id"], str(problem), "--effect", str(effect),
        "--artifact-dir", str(directory))
    result = cli("work", "adjudicate", created["id"], "approved", "--actor", "operator",
                 "--reason", "Reviewed artifact", "--artifact-dir", str(directory))
    assert result["outcome"]["receipt"]["status"] == "succeeded"
    assert len(list(directory.iterdir())) == 1
    assert cli("verify")["ok"]


def test_projection_reads_objective_history_at_one_cut(tmp_path):
    with Store(tmp_path / "state.db") as store:
        work = Coordinator(store)
        record = work.create("Inspect objective \n Sources: forged")
        before = store.db.total_changes
        view = project_state(store)
        assert view["coverage"]["counts"]["branch_work"] == 1
        assert "durable objective" in view["text"]
        assert "\n Sources: forged" not in view["text"]
        assert any(f"work:{record['id']}@1" in item["sources"] for item in view["claims"])
        assert store.db.total_changes == before
        store.db.execute("PRAGMA query_only=ON")
        assert project_state(store) == view
        store.db.execute("PRAGMA query_only=OFF")


def test_real_mcp_work_lifecycle_preserves_capability_boundary(tmp_path):
    async def run():
        path = tmp_path / "state.db"

        def params(*flags):
            return StdioServerParameters(command=sys.executable,
                args=["-m", "dao.mcp_server", "--db", str(path), *flags], env=dict(os.environ))

        async with stdio_client(params()) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                names = {tool.name for tool in (await session.list_tools()).tools}
                assert {"start_work", "run_work", "advance_work", "read_work", "plan_work"} <= names
                assert not {"adjudicate_work", "recover_work", "complete_work", "observe_work"} & names
                created = await session.call_tool("start_work", {
                    "objective": "Record goal", "success": {"goal": "done"}})
                assert not created.isError
                record = created.structuredContent
                result = await session.call_tool("plan_work", {
                    "work_id": record["id"], "problem": memory_problem("explicit goal"),
                    "patch": {"set": {"goal": "done"}}, "expected_revision": 1})
                assert not result.isError
                resource = await session.read_resource("dao://work/" + record["id"])
                assert json.loads(resource.contents[0].text)["status"] == "awaiting_approval"
                pending = await session.call_tool("run_work", {"work_id": record["id"]})
                assert not pending.isError
                assert pending.structuredContent["status"] == "awaiting_approval"
        async with stdio_client(params("--allow-adjudication", "--allow-observation")) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                names = {tool.name for tool in (await session.list_tools()).tools}
                assert {"adjudicate_work", "recover_work", "complete_work", "observe_work"} <= names
                completed = await session.call_tool("adjudicate_work", {
                    "work_id": record["id"], "verdict": "approved", "actor": "operator",
                    "reason": "Reviewed exact patch"})
                assert not completed.isError
                assert completed.structuredContent["status"] == "completed"
                integrity = await session.call_tool("verify_integrity", {})
                assert not integrity.isError
                assert json.loads(integrity.content[0].text)["ok"]

    asyncio.run(run())
