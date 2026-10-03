"""Offline objective continuity and crash recovery of a real external effect."""

from pathlib import Path
from tempfile import TemporaryDirectory

from dao.agent import memory_problem
from dao.executors import ArtifactExecutor
from dao.store import Store
from dao.work import Coordinator


def main():
    with TemporaryDirectory() as directory:
        root = Path(directory)
        path = root / "dao.db"
        with Store(path) as store:
            work = Coordinator(store)
            original = store.head()
            item = work.create("Remember the requested goal", success={"goal": "done"})
            item = work.plan(item["id"], memory_problem("explicit local request"),
                             {"set": {"goal": "done"}})
            assert item["status"] == "awaiting_approval"
            work_id = item["id"]
        with Store(path) as store:
            work = Coordinator(store)
            item = work.adjudicate(work_id, "approved", "operator", "Reviewed exact local patch")
            assert item["status"] == "completed"
            revision = item["revision"]
            store.revert("main", original, store.head())
            assert work.get(work_id)["revision"] == revision
            assert not work.get(work_id)["success_currently_satisfied"]

            class InterruptedArtifact(ArtifactExecutor):
                def execute(self, effect, key):
                    super().execute(effect, key)
                    raise KeyboardInterrupt("Simulated exit after filesystem effect")

            executor = InterruptedArtifact(root / "artifacts")
            work = Coordinator(store, executors={"artifact": executor})
            external = work.create("Produce one verified artifact")
            external = work.plan(external["id"], memory_problem("explicit artifact request"),
                                 effect={"executor": "artifact", "arguments": {"content": "report"},
                                         "preconditions": {"absent": True}, "reservation_usd": "0"})
            work.audit.adjudicate(external["proposal_id"], "approved", "operator", "Reviewed artifact")
            external_id = external["id"]
            try:
                work.advance(external_id)
            except KeyboardInterrupt:
                pass
            assert work.get(external_id)["status"] == "executing"
        with Store(path) as store:
            work = Coordinator(store, executors={"artifact": ArtifactExecutor(root / "artifacts")})
            item = work.reconcile(external_id, "operator", "Worker stopped; inspect original key",
                                  {"dispatch_quiescent": True})
            assert item["outcome"]["receipt"]["status"] == "succeeded"
            assert len(list((root / "artifacts").iterdir())) == 1
            work.complete(external_id, "operator", "Artifact inspected",
                          {"artifact": item["outcome"]["receipt"]["evidence"]})
            assert work.verify_integrity()["ok"]
            assert work.agent.ledger.summary()["reserved_usd"] == "0.000000"
        print("Work demo passed: independent review, durable progress, reconciled external effect.")


if __name__ == "__main__":
    main()
