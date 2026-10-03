"""Read-only, cited natural-language projection of one recorded store cut.

This is an orientation view, not a decision, integrity attestation, or authority
to execute an action. Optional subsystems are read only if their tables already
exist; constructing them here would change the state being described.
"""

from __future__ import annotations

from contextlib import contextmanager
import json
from typing import Any, Iterator

from .store import Store


_DETAIL_LIMIT = 4
_PREVIEW_LIMIT = 140
_CHAT_PREVIEW_LIMIT = 72
_CHAT_DETAIL_LIMIT = 2
PROJECTION_VERSION = "dao-projection/1"
_SNAPSHOT_KEYS = frozenset(
    {"messages", "memory", "decisions", "relationships", "evidence_plans"}
)
_STATUSES = ("pending", "deferred", "rejected", "approved", "approved_stale", "executed")


def _pointer(*parts: str) -> str:
    return "/" + "/".join(part.replace("~", "~0").replace("/", "~1") for part in parts)


def _preview(value: Any, limit: int = _PREVIEW_LIMIT) -> tuple[str, bool]:
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    if len(encoded) > limit:
        # Keep even a clipped preview a complete quoted string. This makes
        # stored, possibly adversarial text visibly data in a chat reply.
        return json.dumps(encoded[:limit] + "...", ensure_ascii=False), True
    return encoded, False


def _numbered(count: int, singular: str, plural: str | None = None) -> str:
    return f"{count} {singular if count == 1 else (plural or singular + 's')}"


def _usd(micros: int) -> str:
    sign = "-" if micros < 0 else ""
    whole, fraction = divmod(abs(micros), 1_000_000)
    return f"{sign}{whole}.{fraction:06d}"


def _table_names(db: Any) -> set[str]:
    return {
        row["name"]
        for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }


@contextmanager
def _read_cut(store: Store) -> Iterator[Any]:
    """Hold one SQLite snapshot without acquiring a write reservation.

    Reuse a caller's transaction if there is one. Otherwise BEGIN a deferred
    read transaction; its first SELECT fixes the WAL snapshot for every later
    read, including the branch head and terminal journal event.
    """

    with store.lock:
        db = store.db
        owns_transaction = not db.in_transaction
        if owns_transaction:
            db.execute("BEGIN")
        try:
            yield db
        finally:
            if owns_transaction:
                db.rollback()


def project_state(store: Store, branch: str = "main") -> dict[str, Any]:
    """Narrate recorded state at one atomic read cut without changing the store.

    ``store`` must already be open. A fresh ``Store`` creates its own genesis;
    this function does not create audit, experience, or ledger tables. All
    database reads occur inside one read transaction so branch heads, snapshot,
    global records, and terminal event refer to the same cut.
    """

    with _read_cut(store) as db:
        head = store.head(branch)
        heads = store.branches()
        state_row = db.execute("SELECT state FROM commits WHERE id = ?", (head,)).fetchone()
        # Store.head guarantees a referenced commit in a healthy store.
        if state_row is None:
            raise ValueError(f"Branch {branch} points to a missing commit")
        state = json.loads(state_row["state"])
        terminal = db.execute("SELECT seq, hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
        event_seq = 0 if terminal is None else terminal["seq"]
        event_hash = None if terminal is None else terminal["hash"]
        tables = _table_names(db)
        work_records = None
        if "work_revisions" in tables:
            work_records = [json.loads(row[0]) for row in db.execute(
                "SELECT w.payload FROM work_revisions w JOIN "
                "(SELECT work_id, MAX(revision) AS revision FROM work_revisions GROUP BY work_id) "
                "latest ON latest.work_id=w.work_id AND latest.revision=w.revision "
                "ORDER BY w.rowid DESC")]
            work_records = [item for item in work_records if item["branch"] == branch]

        audit_tables = {"proposals", "rulings", "executions"}
        if audit_tables <= tables:
            proposals = list(
                db.execute("SELECT id, branch, base, payload FROM proposals ORDER BY rowid DESC")
            )
            rulings = list(db.execute("SELECT seq, proposal_id, verdict FROM rulings ORDER BY seq"))
            executions = list(db.execute("SELECT proposal_id FROM executions"))
        else:
            proposals = rulings = executions = None
        external_claims = {
            row[0] for row in db.execute("SELECT proposal_id FROM external_claims")
        } if "external_claims" in tables else set()
        external_results = {
            row["proposal_id"]: json.loads(row["payload"])["receipt"]["status"]
            for row in db.execute("SELECT proposal_id, payload FROM external_results")
        } if "external_results" in tables else {}
        review_count = (
            db.execute("SELECT COUNT(*) FROM outcome_reviews").fetchone()[0]
            if "outcome_reviews" in tables
            else None
        )

        if "observations" in tables:
            observation_count = db.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
            observation_rows = list(
                db.execute(
                    "SELECT id, payload FROM observations ORDER BY rowid DESC LIMIT ?",
                    (_DETAIL_LIMIT,),
                )
            )
        else:
            observation_count = None
            observation_rows = []

        ledger_tables = {"ledger_budget", "ledger_usage", "ledger_reservations"}
        if ledger_tables <= tables:
            budget_row = db.execute(
                "SELECT amount_micros FROM ledger_budget WHERE id = 1"
            ).fetchone()
            usage_count = 0
            spent = 0
            # SQLite SUM can overflow when lifetime micro-USD totals exceed
            # a signed 64-bit integer; stream rows into Python integers.
            for row in db.execute("SELECT cost_micros FROM ledger_usage"):
                usage_count += 1
                spent += row["cost_micros"]
            reservation_count = pending = unknown = settled_or_released = reserved = 0
            obligation_details = []
            for row in db.execute(
                "SELECT request_id, branch, amount_micros, status "
                "FROM ledger_reservations ORDER BY request_id"
            ):
                reservation_count += 1
                if row["status"] in ("pending", "unknown"):
                    reserved += row["amount_micros"]
                    if row["status"] == "pending":
                        pending += 1
                    else:
                        unknown += 1
                    if len(obligation_details) < _DETAIL_LIMIT:
                        obligation_details.append(dict(row))
                else:
                    settled_or_released += 1
        else:
            budget_row = None
            usage_count = reservation_count = None
            spent = pending = unknown = settled_or_released = reserved = None
            obligation_details = []

    counts: dict[str, Any] = {
        "branches": len(heads),
        "messages": len(state["messages"]),
        "memory": len(state["memory"]),
        "decisions": len(state["decisions"]),
        "relationships": None,
        "evidence_plans": None,
        "other_state_keys": len(set(state) - _SNAPSHOT_KEYS),
        "proposals": None,
        "branch_proposals": None,
        "rulings": None,
        "executions": None,
        "outcome_reviews": review_count,
        "observations": observation_count,
        "ledger_usage": None,
        "ledger_reservations": None,
        "ledger_pending": None,
        "ledger_unknown": None,
        "proposal_statuses": None,
        "branch_work": None if work_records is None else len(work_records),
    }
    omissions = [
        "Conversation message bodies are omitted; only their count is reported.",
        "Historical commits, full event payloads, decision inputs, and complete relationship evidence are not narrated.",
        "This projection does not verify event-chain, audit, observation, or ledger integrity.",
    ]
    unavailable = [
        "Model internals, provider-side records, and facts never recorded in this store are unavailable."
    ]
    claims: list[dict[str, Any]] = []
    event_ref = f"event:{event_seq}:{event_hash}" if event_seq else "events:empty"

    def add(sentence: str, sources: list[str]) -> None:
        claims.append({"text": sentence, "sources": sources})

    def snapshot_ref(*parts: str) -> str:
        return f"commit:{head}#{_pointer(*parts)}"

    add(
        f"At journal event {event_seq}, branch {json.dumps(branch)} points to commit {head}.",
        [f"branch:{branch}@{head}", event_ref],
    )
    shown_heads = sorted(heads.items())[:_DETAIL_LIMIT]
    heads_text = ", ".join(f"{name} → {commit}" for name, commit in shown_heads)
    add(
        f"The store has {len(heads)} branch head(s): {heads_text}"
        + ("; more branch heads are omitted here." if len(heads) > len(shown_heads) else "."),
        [f"branch:{name}@{commit}" for name, commit in shown_heads] + [event_ref],
    )
    if len(heads) > len(shown_heads):
        omissions.append(f"{len(heads) - len(shown_heads)} additional branch heads are omitted from prose.")

    add(
        f"This branch snapshot contains {counts['messages']} message(s), "
        f"{counts['memory']} memory item(s), and {counts['decisions']} recorded decision(s).",
        [snapshot_ref("messages"), snapshot_ref("memory"), snapshot_ref("decisions")],
    )
    if work_records is not None:
        add(f"This branch has {len(work_records)} durable objective record(s); "
            "their progress is operational history and is not rewound by snapshot restoration.",
            [event_ref])
        for work in work_records[:_DETAIL_LIMIT]:
            objective, clipped = _preview(work["objective"])
            add(f"Work {json.dumps(work['id'])} at revision {work['revision']} records "
                f"status {json.dumps(work['status'])} and objective {objective}.",
                [f"work:{work['id']}@{work['revision']}", event_ref])
            if clipped:
                omissions.append("A work objective is previewed, not shown in full.")
        if len(work_records) > _DETAIL_LIMIT:
            omissions.append(f"{len(work_records) - _DETAIL_LIMIT} work records are omitted.")
    for key, value in sorted(state["memory"].items())[:_DETAIL_LIMIT]:
        preview, truncated = _preview(value)
        label, label_truncated = _preview(key)
        add(
            f"Memory {label} has {'a value preview of ' if truncated else 'value '}{preview}.",
            [snapshot_ref("memory", key)],
        )
        if truncated or label_truncated:
            omissions.append(f"Memory item at {snapshot_ref('memory', key)} is previewed, not shown in full.")
    if counts["memory"] > _DETAIL_LIMIT:
        omissions.append(f"{counts['memory'] - _DETAIL_LIMIT} memory values are omitted from prose.")

    for key, value in sorted(state["decisions"].items())[:_DETAIL_LIMIT]:
        recommendation = value.get("recommendation") if isinstance(value, dict) else None
        preview, truncated = _preview(recommendation)
        add(
            f"Decision {json.dumps(key)} records a recommendation"
            f"{' preview' if truncated else ''} of {preview}.",
            [snapshot_ref("decisions", key)],
        )
    if counts["decisions"] > _DETAIL_LIMIT:
        omissions.append(f"{counts['decisions'] - _DETAIL_LIMIT} decisions are omitted from prose.")

    relationships = state.get("relationships", {})
    if isinstance(relationships, dict):
        counts["relationships"] = len(relationships)
        evidence_ids = {
            evidence_id
            for relation in relationships.values()
            if isinstance(relation, dict) and isinstance(relation.get("evidence_ids"), list)
            for evidence_id in relation["evidence_ids"]
            if isinstance(evidence_id, str)
        }
        counts["relationship_evidence_references"] = len(evidence_ids)
        add(
            f"The selected branch records {len(relationships)} relationship(s) and references "
            f"{len(evidence_ids)} distinct observation ID(s) as evidence; global reports do not "
            "automatically change this branch's beliefs.",
            [snapshot_ref("relationships"), event_ref],
        )
        for key, relation in sorted(relationships.items())[:_DETAIL_LIMIT]:
            if isinstance(relation, dict):
                description = {
                    field: relation.get(field)
                    for field in ("subject", "object", "dimension", "basis", "belief")
                    if field in relation
                }
            else:
                description = relation
            preview, truncated = _preview(description)
            add(
                f"Relationship {json.dumps(key)} records"
                f"{' a preview of' if truncated else ''} {preview}.",
                [snapshot_ref("relationships", key)],
            )
            if truncated:
                omissions.append(f"Relationship at {snapshot_ref('relationships', key)} is previewed.")
        if len(relationships) > _DETAIL_LIMIT:
            omissions.append(
                f"{len(relationships) - _DETAIL_LIMIT} relationships are omitted from prose."
            )
    else:
        unavailable.append("The selected snapshot's relationships field is not an object.")

    plans = state.get("evidence_plans", {})
    if isinstance(plans, dict):
        counts["evidence_plans"] = len(plans)
        add(
            f"The selected branch records {len(plans)} evidence plan(s).",
            [snapshot_ref("evidence_plans")],
        )
        for key, plan in sorted(plans.items())[:_DETAIL_LIMIT]:
            status = plan.get("status") if isinstance(plan, dict) else None
            preview, truncated = _preview(status)
            add(
                f"Evidence plan {json.dumps(key)} has recorded status"
                f"{' preview' if truncated else ''} {preview}.",
                [snapshot_ref("evidence_plans", key)],
            )
        if len(plans) > _DETAIL_LIMIT:
            omissions.append(f"{len(plans) - _DETAIL_LIMIT} evidence plans are omitted from prose.")
    else:
        unavailable.append("The selected snapshot's evidence_plans field is not an object.")

    other_keys = sorted(set(state) - _SNAPSHOT_KEYS)
    if other_keys:
        labels = ", ".join(json.dumps(key) for key in other_keys[:_DETAIL_LIMIT])
        add(
            f"The selected snapshot has {len(other_keys)} additional top-level field(s): {labels}."
            " Their contents are not interpreted by this projection.",
            [snapshot_ref(key) for key in other_keys[:_DETAIL_LIMIT]],
        )
        omissions.append("Values in additional top-level snapshot fields are not narrated.")
        if len(other_keys) > _DETAIL_LIMIT:
            omissions.append(
                f"{len(other_keys) - _DETAIL_LIMIT} additional top-level field names are omitted."
            )

    if proposals is None:
        missing = sorted(audit_tables - tables)
        unavailable.append(f"Audit status is unavailable; uninitialized table(s): {', '.join(missing)}.")
        add("Audit proposal, ruling, and execution status is unavailable in this store.", [event_ref])
    else:
        latest_ruling = {row["proposal_id"]: row for row in rulings}
        executed_ids = {row["proposal_id"] for row in executions}
        statuses = dict.fromkeys(_STATUSES, 0)
        branch_rows: list[tuple[Any, str]] = []
        for proposal in proposals:
            proposal_id = proposal["id"]
            ruling = latest_ruling.get(proposal_id)
            if proposal_id in executed_ids:
                status = "executed"
            elif proposal_id in external_results:
                status = "external_not_executed"
            elif proposal_id in external_claims:
                status = "external_pending"
            elif ruling is None:
                status = "pending"
            elif ruling["verdict"] == "approved" and heads.get(proposal["branch"]) != proposal["base"]:
                status = "approved_stale"
            else:
                status = ruling["verdict"]
            statuses[status] = statuses.get(status, 0) + 1
            if proposal["branch"] == branch:
                branch_rows.append((proposal, status))
        counts.update(
            proposals=len(proposals),
            branch_proposals=len(branch_rows),
            rulings=len(rulings),
            executions=len(executions),
            proposal_statuses=statuses,
        )
        add(
            f"Across all branches, the audit records {len(proposals)} proposal(s), "
            f"{len(rulings)} ruling(s), {len(executions)} execution(s), "
            f"and {review_count if review_count is not None else 'an unavailable number of'} outcome review(s). "
            + ", ".join(f"{status.replace('_', ' ')}: {count}" for status, count in statuses.items())
            + ".",
            [
                f"proposals@{event_ref}",
                f"rulings@{event_ref}",
                f"executions@{event_ref}",
                f"outcome_reviews@{event_ref}" if review_count is not None else event_ref,
            ],
        )
        add(
            f"{len(branch_rows)} of those proposal(s) were created on branch {json.dumps(branch)}.",
            [f"proposals:branch:{branch}@{event_ref}"],
        )
        for proposal, status in branch_rows[:_DETAIL_LIMIT]:
            proposal_id = proposal["id"]
            ruling = latest_ruling.get(proposal_id)
            source = [f"proposal:{proposal_id}"]
            if ruling is not None:
                source.append(f"ruling:{ruling['seq']}")
            if status == "executed":
                source.append(f"execution:{proposal_id}")
            if status in {"external_pending", "external_not_executed"}:
                source.append(f"external_claim:{proposal_id}")
                if status == "external_not_executed":
                    source.append(f"external_result:{proposal_id}")
            if status == "approved_stale":
                source.append(f"branch:{branch}@{head}")
            kind = json.loads(proposal["payload"]).get("kind", "action")
            note = (
                " (branch head moved; this approval no longer matches its base)"
                if status == "approved_stale"
                else ""
            )
            add(
                f"Proposal {proposal_id} is {status.replace('_', ' ')}{note}; "
                f"its recorded kind is {json.dumps(kind)}.",
                source,
            )
        if len(branch_rows) > _DETAIL_LIMIT:
            omissions.append(
                f"{len(branch_rows) - _DETAIL_LIMIT} branch proposal statuses are omitted from prose."
            )
        if review_count is None:
            unavailable.append("Outcome review count is unavailable; outcome_reviews is uninitialized.")
        elif review_count:
            omissions.append("Outcome review details are not narrated.")

    if observation_count is None:
        unavailable.append("Observation records are unavailable; observations is uninitialized.")
        add("The observation record table is uninitialized, so its report count is unknown.", [event_ref])
    else:
        add(
            f"The global observation record contains {observation_count} report(s). "
            "Reports are claims from recorded sources, not independently verified facts.",
            [f"observations@{event_ref}"],
        )
        for row in observation_rows:
            record = json.loads(row["payload"])
            signal, truncated = _preview(record.get("signal"))
            add(
                f"Observation {row['id']} reports signal"
                f"{' preview' if truncated else ''} {signal} for relationship "
                f"{json.dumps(record.get('relationship_id'))}.",
                [f"observation:{row['id']}"],
            )
            if truncated:
                omissions.append(f"Signal in observation {row['id']} is previewed.")
        if observation_count > len(observation_rows):
            omissions.append(
                f"{observation_count - len(observation_rows)} observations are omitted from prose."
            )

    if budget_row is None or usage_count is None or reservation_count is None:
        missing = sorted(ledger_tables - tables)
        reason = (
            f"uninitialized table(s): {', '.join(missing)}"
            if missing
            else "the budget row is absent"
        )
        unavailable.append(f"Global usage accounting is unavailable; {reason}.")
        add("Global budget, settled spend, and outstanding reservations are unavailable.", [event_ref])
    else:
        budget = budget_row["amount_micros"]
        remaining = budget - spent - reserved
        counts.update(
            ledger_usage=usage_count,
            ledger_reservations=reservation_count,
            ledger_pending=pending,
            ledger_unknown=unknown,
        )
        add(
            f"Global usage accounting records a ${_usd(budget)} budget, ${_usd(spent)} "
            f"settled spend across {usage_count} usage entry/entries, and ${_usd(reserved)} "
            f"still reserved across {pending} pending and {unknown} unknown request(s). "
            f"Remaining budget capacity is ${_usd(max(remaining, 0))}"
            + (f"; the account is ${_usd(-remaining)} over budget." if remaining < 0 else "."),
            [f"ledger_budget:1@{event_ref}", f"ledger_usage@{event_ref}",
             f"ledger_reservations@{event_ref}"],
        )
        for row in obligation_details:
            add(
                f"Request {json.dumps(row['request_id'])} on branch "
                f"{json.dumps(row['branch'])} remains {row['status']} with "
                f"${_usd(row['amount_micros'])} reserved.",
                [f"ledger_reservation:{row['request_id']}@{event_ref}"],
            )
        if pending + unknown > _DETAIL_LIMIT:
            omissions.append(
                f"{pending + unknown - _DETAIL_LIMIT} outstanding reservations are omitted from prose."
            )
        if usage_count:
            omissions.append("Individual settled usage entries are omitted from prose.")
        if settled_or_released:
            omissions.append(
                f"{settled_or_released} settled or released reservation details are omitted."
            )

    relationship_count = counts["relationships"]
    plan_count = counts["evidence_plans"]
    text_parts = [
        f"On branch {json.dumps(branch)} at head {head[:12]}... (journal event {event_seq}), "
        f"Dao records {_numbered(len(heads), 'branch')}. This branch has "
        f"{_numbered(counts['messages'], 'message')}, "
        f"{_numbered(counts['memory'], 'memory item')}, and "
        f"{_numbered(counts['decisions'], 'decision')}."
    ]
    if relationship_count is not None or plan_count is not None:
        text_parts[0] += (
            f" It also has {_numbered(relationship_count, 'relationship') if relationship_count is not None else 'an unavailable relationship count'}"
            f" and {_numbered(plan_count, 'evidence plan') if plan_count is not None else 'an unavailable evidence-plan count'}."
        )
    other_heads = [(name, commit) for name, commit in sorted(heads.items()) if name != branch]
    if other_heads:
        head_examples = ", ".join(
            f"{json.dumps(name)} at {commit[:12]}..."
            for name, commit in other_heads[:_CHAT_DETAIL_LIMIT]
        )
        text_parts.append(
            f"Other branch heads include {head_examples}"
            + ("; additional heads are omitted here." if len(other_heads) > _CHAT_DETAIL_LIMIT else ".")
        )

    memory_items = sorted(state["memory"].items())[:_CHAT_DETAIL_LIMIT]
    if memory_items:
        examples = ", ".join(
            f"{_preview(key, _CHAT_PREVIEW_LIMIT)[0]} = {_preview(value, _CHAT_PREVIEW_LIMIT)[0]}"
            for key, value in memory_items
        )
        text_parts.append(
            f"Recorded memory includes {examples}"
            + ("; other values are omitted here." if counts["memory"] > len(memory_items) else ".")
        )
    if counts["decisions"]:
        decision_id, decision = sorted(state["decisions"].items())[0]
        recommendation = decision.get("recommendation") if isinstance(decision, dict) else None
        short_decision_id = decision_id[:12] + ("..." if len(decision_id) > 12 else "")
        text_parts.append(
            f"A recorded decision, {_preview(short_decision_id, _CHAT_PREVIEW_LIMIT)[0]}, "
            f"recommends {_preview(recommendation, _CHAT_PREVIEW_LIMIT)[0]}."
        )
    if isinstance(relationships, dict) and relationships:
        relationship_id, relation = sorted(relationships.items())[0]
        basis = relation.get("basis") if isinstance(relation, dict) else None
        text_parts.append(
            f"Relationship {_preview(relationship_id, _CHAT_PREVIEW_LIMIT)[0]} records "
            f"basis {_preview(basis, _CHAT_PREVIEW_LIMIT)[0]} and the branch cites "
            f"{_numbered(counts['relationship_evidence_references'], 'observation ID')} as evidence."
        )
    if other_keys:
        labels = ", ".join(
            _preview(key, _CHAT_PREVIEW_LIMIT)[0] for key in other_keys[:_CHAT_DETAIL_LIMIT]
        )
        text_parts.append(
            f"Additional state fields include {labels}; their values are not interpreted."
        )

    global_start = len(text_parts)
    if work_records:
        text_parts.append(f"This branch has {_numbered(len(work_records), 'durable objective')}; "
                          "work progress remains recorded across branch restoration.")
        for work in work_records[:_DETAIL_LIMIT]:
            objective, _ = _preview(work["objective"])
            text_parts.append(f"Work {json.dumps(work['id'])} is recorded as "
                              f"{json.dumps(work['status'])} at revision {work['revision']}: "
                              f"{objective}.")
    unavailable_summaries = []
    if proposals is None:
        unavailable_summaries.append("audit status")
    elif proposals:
        status_labels = {
            "pending": "pending proposal",
            "deferred": "deferred proposal",
            "rejected": "rejected proposal",
            "approved": "approved proposal",
            "approved_stale": "stale approval",
            "executed": "executed proposal",
            "external_pending": "external dispatch awaiting reconciliation",
            "external_not_executed": "external effect verified as not executed",
        }
        status_phrases = [
            _numbered(count, status_labels[status])
            for status, count in statuses.items()
            if count
        ]
        text_parts.append(
            f"Across all branches, audit has {_numbered(len(proposals), 'proposal')}: "
            + ", ".join(status_phrases)
            + f". Of those, {len(branch_rows)} came from this branch."
        )
        if statuses["approved_stale"]:
            text_parts.append("A stale approval cannot execute against the moved branch head.")
    else:
        text_parts.append("The audit has no recorded proposals.")
    if review_count is None and proposals is not None:
        text_parts.append("Outcome review count is unavailable.")

    if observation_count is None:
        unavailable_summaries.append("global observations")
    else:
        text_parts.append(
            f"The global observation record has {_numbered(observation_count, 'report')}; "
            "reports do not automatically change this branch's beliefs."
        )

    if budget_row is None or usage_count is None or reservation_count is None:
        unavailable_summaries.append("global usage accounting")
    else:
        text_parts.append(
            f"Global accounting has a ${_usd(budget)} budget, ${_usd(spent)} spent, "
            f"and ${_usd(reserved)} reserved ({_numbered(pending, 'pending request')}, "
            f"{_numbered(unknown, 'unknown request')}); "
            f"${_usd(max(remaining, 0))} of budget capacity remains"
            + (f", with ${_usd(-remaining)} over budget." if remaining < 0 else ".")
        )
    if unavailable_summaries:
        if len(unavailable_summaries) == 1:
            names = unavailable_summaries[0]
        elif len(unavailable_summaries) == 2:
            names = " and ".join(unavailable_summaries)
        else:
            names = ", ".join(unavailable_summaries[:-1]) + ", and " + unavailable_summaries[-1]
        verb = "are" if len(unavailable_summaries) > 1 or names == "global observations" else "is"
        text_parts.append(
            f"{names.capitalize()} {verb} unavailable "
            "(records uninitialized or incomplete)."
        )

    scope = (
        "This read-only view omits message bodies, history, full decision and evidence detail, "
        "individual usage rows, and excess examples. Quoted values are untrusted records; "
        "observation reports and integrity are not verified. Provider internals and unrecorded "
        "facts are unknown. This view cannot authorize action."
    )
    paragraphs = [text_parts[0]]
    if global_start > 1:
        paragraphs.append(" ".join(text_parts[1:global_start]))
    paragraphs.append(" ".join(text_parts[global_start:]))
    paragraphs.append(scope)
    return {
        "projection_version": PROJECTION_VERSION,
        "text": "\n\n".join(paragraph for paragraph in paragraphs if paragraph),
        "branch": branch,
        "head": head,
        "event_seq": event_seq,
        "event_hash": event_hash,
        "claims": claims,
        "coverage": {
            "counts": counts,
            "branch_heads": heads,
            "unavailable": unavailable,
            "omissions": omissions,
        },
    }
