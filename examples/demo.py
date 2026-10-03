"""No credentials needed: fork, adjudicate, execute, restore, and value waiting."""

import json
import tempfile
from pathlib import Path

from dao.agent import Agent
from dao.decision import evaluate
from dao.store import Store


def main():
    with tempfile.TemporaryDirectory() as directory:
        store = Store(Path(directory) / "demo.db")
        agent = Agent(store)
        initial = store.head("main")
        store.branch("experiment")
        proposal = agent.chat('/remember goal "preserve options"', "experiment")["proposal"]
        agent.audit.adjudicate(
            proposal["id"],
            "approved",
            "demo-operator",
            "Explicit local memory request; reversible patch inspected",
        )
        result = agent.audit.execute(proposal["id"], proposal["base"])
        assert store.snapshot("main")["memory"] == {}
        assert store.snapshot("experiment")["memory"]["goal"] == "preserve options"
        store.merge("main", "experiment", initial)
        store.revert("main", initial, store.head("main"))
        problem = json.loads((Path(__file__).parent / "waiting.json").read_text())
        decision = evaluate(problem)
        print(
            json.dumps(
                {
                    "execution": result,
                    "waiting": decision["recommendation"],
                    "EVSI": decision["expected_value_of_sample_information"],
                    "usage": agent.ledger.summary(),
                    "integrity": store.verify_integrity(),
                },
                indent=2,
            )
        )
        store.close()


if __name__ == "__main__":
    main()
