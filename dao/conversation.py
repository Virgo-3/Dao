"""Small grounded conversational actions, independent of storage and providers.

Only the current user message can authorize a conversational capture. Semantic
notes describe what the user said; they do not verify a claim or adjudicate an
action. Unrecognized language is left for the conversational provider.
"""

from copy import deepcopy
import json
import re
import uuid

from .relationships import apply
from .store import ConflictError
from .structure import (MAX_OPERATIONS, MAX_PROPOSAL_BYTES, dependency_index,
                        grounded_operation, message_records, review)

_CHOICE_PREFIX = (r"(?:we|i)\s+(?:chose|choose|have chosen|decided on|have decided on|decided to use|have decided to use|are using|am using|will use)"
                  r"|(?:we've|i've)\s+(?:chosen|decided on|decided to use)"
                  r"|(?:we're|i'm)\s+using|(?:we'll|i'll|let's)\s+use")


def _current(state):
    records = message_records(state)
    if not records or records[-1].get("role") != "user" or records[-1].get("status") == "failed":
        raise ValueError("Conversational capture requires a current user message")
    return records[-1]


def _plain(value):
    return value.replace("’", "'").replace("‘", "'").strip()


def _conflicting_decision(text):
    """Conservatively defer multiple commitments or a later retraction."""
    commitments = list(re.finditer(r"\b(?:" + _CHOICE_PREFIX + r")\b", text, re.I))
    if not commitments:
        return False
    tail = text[commitments[0].end():]
    return len(commitments) > 1 or bool(re.search(
        r"\b(?:actually|on second thought|scratch that|never mind|changed (?:our|my) minds?"
        r"|(?:haven't|have not) decided|(?:didn't|did not) choose"
        r"|(?:won't|will not|no longer) use|(?:aren't|are not|am not) using)\b", tail, re.I))


def _flags(content):
    text = _plain(content)
    # Evaluate the commitment clause, rather than mistaking an uncertain reason
    # ("we chose X because it might ...") for an uncertain decision.
    text = re.sub(r"^remember that\s+", "", text, flags=re.I)
    text = re.sub(r"^actually[, :]?\s+", "", text, flags=re.I)
    clause = re.split(r"\bbecause\b", text, maxsplit=1, flags=re.I)[0]
    quoted = bool(re.match(r"^[\"'“”‘’>`]", text)
                  or re.match(r"^(?:[A-Za-z][A-Za-z'-]*(?:\s+[A-Za-z][A-Za-z'-]*)?\s+(?:said|says|wrote|mentioned)|the\s+(?:document|example|user)\s+(?:said|says|contains))\b", text, re.I))
    hypothetical = bool(re.match(r"^(?:if\b|what if\b|imagine\b|suppos(?:e|ing)\b|hypothetical(?:ly)?\b)", text, re.I)
                        or re.search(r"\b(?:if|hypothetical(?:ly)?)\b", clause, re.I))
    tentative = bool(re.search(r"\b(?:maybe|perhaps|might|may|could|would|considering|possibly|undecided|tentative)\b|\bnot sure\b", clause, re.I))
    negative = bool(re.search(r"\b(?:didn't|did not|don't|do not|haven't|have not|hasn't|has not|never|not|no)\b", clause, re.I))
    affirmative = bool(re.match(r"^(?:" + _CHOICE_PREFIX + r")\b", text, re.I))
    conflicting = _conflicting_decision(text)
    return {"quoted": quoted, "hypothetical": hypothetical, "tentative": tentative,
            "negative": negative, "conflicting_decision": conflicting,
            "decided": affirmative and not (quoted or hypothetical or tentative or negative or conflicting)}


def _stance(record, content):
    flags = _flags(content)
    supplied = record.get("stance")
    if "stance" in record and supplied not in ("mentioned", "believed", "considering", "decided"):
        raise ValueError("Node stance must be mentioned, believed, considering, or decided")
    if record.get("kind") == "decision" or supplied == "decided":
        if not flags["decided"]:
            raise ValueError("A decided record requires an affirmative decision in the current user message")
    if flags["conflicting_decision"]:
        if supplied in ("believed", "considering"):
            raise ValueError("Conflicting choices require clarification before capture")
        return "mentioned"
    if supplied in ("believed", "considering") and (flags["quoted"] or flags["hypothetical"]):
        raise ValueError("Quoted or hypothetical statements can be captured only as mentioned")
    if supplied == "believed" and flags["tentative"]:
        raise ValueError("A tentative statement must remain considering or mentioned")
    if supplied is not None:
        return supplied
    if record.get("kind") == "decision":
        return "decided"
    if flags["quoted"] or flags["hypothetical"]:
        return "mentioned"
    if flags["tentative"]:
        return "considering"
    if flags["decided"] or re.match(r"^(?:remember that\b|actually\b)", _plain(content), re.I):
        return "believed"
    return "mentioned"


def _citations(records, maximum=12):
    found, seen = [], set()
    for record in records:
        for citation in record.get("citations", []):
            key = (citation["message_id"], citation["start"], citation["end"], citation["quote"])
            if key not in seen:
                seen.add(key)
                found.append(deepcopy(citation))
    return found[:maximum]


def _activity(kind, label, node_ids, citations, **extra):
    return {"id": uuid.uuid4().hex, "kind": kind, "label": label,
            "node_ids": list(node_ids), "citations": deepcopy(citations), **extra}


def _active_nodes(graph):
    graph = graph or {}
    superseded = {relation["target"] for relation in graph.get("relations", {}).values()
                  if relation["kind"] == "supersedes"}
    return sorted((node for node in graph.get("nodes", {}).values() if node["id"] not in superseded),
                  key=lambda node: (node.get("created_at", ""), node["id"]), reverse=True)


def _latest_decision(graph):
    return next((node for node in _active_nodes(graph)
                 if node["kind"] == "decision" and node.get("stance") == "decided"), None)


def _dependencies(graph, identifier):
    nodes = (graph or {}).get("nodes", {})
    outgoing = {}
    for relation in (graph or {}).get("relations", {}).values():
        if relation["kind"] == "depends_on":
            outgoing.setdefault(relation["source"], set()).add(relation["target"])
    seen, pending = {identifier}, list(outgoing.get(identifier, set()))
    found = []
    while pending:
        current = pending.pop()
        if current not in seen:
            seen.add(current)
            found.append(nodes[current])
            pending.extend(sorted(outgoing.get(current, set()) - seen))
    return sorted(found, key=lambda node: (node.get("created_at", ""), node["id"]))


def _correction_context(graph, content):
    decision = _latest_decision(graph)
    if decision is None:
        return [], [], []
    reasons = [node for node in _dependencies(graph, decision["id"])
               if node["kind"] in {"assumption", "requirement"}]
    # Recency alone is insufficient evidence that a correction changes the
    # decision. Shared substantive wording or an explicit storage concurrency
    # constraint gives the assistant a reason to suggest reconsideration.
    stop = {"actually", "because", "that", "this", "with", "have", "need", "needs", "must", "should", "would", "could",
            "there", "their", "they", "them", "using", "chose", "decision", "instead", "enough", "from", "work"}
    words = lambda text: {word for word in re.findall(r"[a-z]{4,}", text.casefold()) if word not in stop}
    updated_words = words(content)
    related = bool(updated_words & words(" ".join(node.get("content", node["label"]) for node in [decision, *reasons])))
    storage = re.search(r"\b(?:sqlite|database|storage)\b", decision.get("content", decision["label"]), re.I)
    concurrency = re.search(r"\b(?:concurrent|simultaneous(?:ly)?|multiple\s+(?:people|writers|users))\b", content, re.I)
    related = related or bool(storage and concurrency)
    if not related:
        return [], [], [decision["id"]]
    previous = [node["id"] for node in reasons]
    dependents = dependency_index(graph)
    affected = {decision["id"]}
    for identifier in previous:
        affected.update(dependents.get(identifier, []))
    return previous, sorted(affected), []


def capture(state, payload):
    """Atomically remember low impact notes grounded only to this user turn.

    The model may create definitions, but cannot set beliefs, observations,
    action scopes or severe conflicts through this path. No activity log is
    written here; the service persists the returned activity with its reply.
    """
    message = _current(state)
    if not isinstance(payload, dict) or set(payload) != {"rationale", "operations"}:
        raise ValueError("Capture accepts only rationale and operations")
    rationale = payload["rationale"]
    if not isinstance(rationale, str) or not rationale.strip() or len(rationale) > 4000:
        raise ValueError("Capture rationale must be nonempty text up to 4000 characters")
    try:
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, OverflowError, RecursionError, UnicodeError) as exc:
        raise ValueError("Capture must contain bounded, finite JSON values") from exc
    if len(encoded) > MAX_PROPOSAL_BYTES:
        raise ValueError("Capture exceeds 128 KiB")
    operations = payload["operations"]
    if not isinstance(operations, list) or not 1 <= len(operations) <= MAX_OPERATIONS:
        raise ValueError(f"Capture requires 1..{MAX_OPERATIONS} operations")
    previous_graph = state.get("relationships")
    graph, added_nodes, records = previous_graph, [], []
    for submitted in operations:
        if not isinstance(submitted, dict) or submitted.get("operation") not in ("node", "relation"):
            raise ValueError("Conversational capture accepts only node and relation creation")
        operation = grounded_operation(state, submitted, required=True)
        record = operation[operation["operation"]]
        if any(citation["message_id"] != message["id"] for citation in record["citations"]):
            raise ValueError("Conversational capture may cite only the current user message")
        if operation["operation"] == "node":
            record["stance"] = _stance(record, message["content"])
        elif record.get("severe") is not False or not isinstance(record.get("actions"), list) or record["actions"]:
            raise ValueError("Conversational relations must have severe false and actions []")
        graph, event = apply(graph, operation)
        records.append(record)
        if operation["operation"] == "node":
            added_nodes.append(graph["nodes"][event["node_id"]])
    summary = rationale.strip()[:400]
    current_ids = [node["id"] for node in added_nodes]
    if (re.match(r"^actually\b", _plain(message["content"]), re.I)
            and not any(node["stance"] == "decided" for node in added_nodes)):
        previous_ids, affected, possible = _correction_context(previous_graph, message["content"])
        if possible:
            summary = ("Possible context for an earlier decision: " + summary)[:400]
        activity = _activity("assumption_changed", "Assumption to revisit", current_ids, _citations(records),
                             summary=summary, previous_node_ids=previous_ids, affected_node_ids=affected,
                             possible_node_ids=possible)
    else:
        label = ("Decision remembered" if any(node["stance"] == "decided" for node in added_nodes)
                 else "Option remembered" if any(node["stance"] == "considering" for node in added_nodes)
                 else "Note remembered" if added_nodes else "Connection remembered")
        activity = _activity("remembered", label, current_ids, _citations(records), summary=summary)
    # All validation (including the final activity construction) precedes mutation.
    state["relationships"] = graph
    return activity


def _citation(message):
    return {"message_id": message["id"], "start": 0, "end": len(message["content"]), "quote": message["content"]}


def _node(label, kind, content, stance, citation):
    return {"operation": "node", "node": {"id": "note-" + uuid.uuid4().hex,
            "label": label[:256], "content": content[:4000], "kind": kind, "importance": 1,
            "stance": stance, "citations": [citation]}}


def _relation(source, target, kind, citation):
    return {"operation": "relation", "relation": {"id": "link-" + uuid.uuid4().hex,
            "source": source, "target": target, "kind": kind, "weight": 1,
            "severe": False, "actions": [], "citations": [citation]}}


def _recall(state, *, all_notes=False):
    graph = state.get("relationships")
    if all_notes:
        nodes = _active_nodes(graph)[:6]
        memory = list(state.get("memory", {}).items())[:6]
        if not nodes and not memory:
            return "I haven’t recorded any notes yet.", []
        lines = [f"{node['stance'].capitalize() if node.get('stance') else 'Recorded ' + node['kind']}: {node['label']}"
                 for node in nodes]
        lines.extend(f"Saved note {key}: {value[:200]}{'…' if len(value) > 200 else ''}" for key, value in memory)
        activity = _activity("recalled", "Notes recalled", [node["id"] for node in nodes], _citations(nodes),
                             summary="; ".join(lines)[:400], memory_keys=[key for key, _ in memory])
        return "Here’s what I have recorded:\n" + "\n".join("- " + line for line in lines), [activity]
    decision = _latest_decision(graph)
    if decision is None:
        return "I don’t have a settled decision recorded yet.", []
    dependencies = _dependencies(graph, decision["id"])
    reasons = [node.get("content", node["label"]) for node in dependencies]
    reply = "The most recent recorded decision was: " + decision.get("content", decision["label"])
    reply += "\nThe recorded reasoning was: " + "; ".join(reasons) if reasons else "\nNo supporting reason was recorded."
    activity = _activity("recalled", "Decision recalled", [decision["id"], *[node["id"] for node in dependencies]],
                         _citations([decision, *dependencies]), summary=reply[:400], rationale=reasons)
    return reply, [activity]


def _safe_branch(name):
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,79}", name)
                and ".." not in name and "//" not in name and not name.endswith(("/", ".")))


def _review_reply(state, action, selection=None):
    pending = [proposal for proposal in state.get("proposals", []) if proposal.get("status") == "pending"]
    if not pending:
        return {"reply": "There isn’t a pending suggestion to " + ("keep." if action == "accept" else "reject."),
                "activities": [], "navigation": None}
    if selection is not None and not 1 <= selection <= len(pending):
        return {"reply": f"There are {len(pending)} pending suggestions. Choose a number from 1 to {len(pending)}.",
                "activities": [], "navigation": None}
    if len(pending) > 1 and selection is None:
        summaries = [proposal.get("rationale", "Structure suggestion")[:120] for proposal in pending]
        return {"reply": "There are several pending suggestions. Which one do you mean?\n" +
                         "\n".join(f"{index}. {summary}" for index, summary in enumerate(summaries, 1)) +
                         "\nReply ‘Keep suggestion 2’ or ‘Reject suggestion 2’, using the number you want.",
                "activities": [], "navigation": None}
    chosen = pending[0 if selection is None else selection - 1]
    candidate = deepcopy(state)
    try:
        reviewed = review(candidate, chosen["id"], action)
    except ConflictError:
        return {"reply": "The notes changed since that suggestion. I need a fresh suggestion before keeping it.",
                "activities": [], "navigation": None}
    state["proposals"] = candidate["proposals"]
    if "relationships" in candidate:
        state["relationships"] = candidate["relationships"]
    nodes = [operation["node"]["id"] for operation in reviewed["operations"] if operation["operation"] == "node"]
    records = [operation.get(operation["operation"], operation) for operation in reviewed["operations"]]
    kept = action == "accept"
    activity = _activity("proposal_accepted" if kept else "proposal_rejected", "Suggestion kept" if kept else "Suggestion rejected",
                         nodes, _citations(records), summary=reviewed["rationale"][:400], proposal_id=reviewed["id"])
    return {"reply": "I kept that suggestion." if kept else "I rejected that suggestion.",
            "activities": [activity], "navigation": None}


def _view_reply(state, message, view, *, reasoning=False):
    """Describe saved context and request a read-only browser view after commit."""
    if view == "memory":
        reply, recalled = _recall(state, all_notes=not reasoning)
        node_ids = recalled[0]["node_ids"] if recalled else []
        citations = recalled[0]["citations"] if recalled else [_citation(message)]
    else:
        reply = {
            "decision": "The Decision tool compares acting, waiting, and abstaining using your explicit assumptions.",
            "structure": "Structure shows saved relationships and suggestions awaiting your review.",
            "audit": "The Audit tool reviews the evidence you supply against a source-count and reliability rule.",
            "usage": "Usage shows recorded tokens, the lifetime budget, and configured cost estimates.",
            "tools": "Tools contains Decision, Structure, Audit, and Usage, plus export and history verification.",
        }[view]
        node_ids, citations = [], [_citation(message)]
    activity = _activity("view_requested", "Reasoning requested" if reasoning else view.capitalize() + " requested",
                         node_ids, citations, view=view, summary=reply[:400])
    return {"reply": reply, "activities": [activity], "navigation": None}


def prepare(state):
    """Handle a few explicit intents; unknown messages leave state untouched."""
    result = {"reply": None, "activities": [], "navigation": None}
    try:
        message = _current(state)
    except ValueError:
        return result
    content = message["content"]
    plain = _plain(content)
    flags = _flags(content)
    if flags["quoted"] or flags["hypothetical"]:
        return result
    if flags["conflicting_decision"]:
        result["reply"] = "I’m unsure which choice is current. Which decision and reason should I remember?"
        return result
    intent = re.sub(r"[.!?]\s*$", "", plain)
    if re.fullmatch(r"(?:show(?: me)? (?:the )?(?:assumptions|reasoning|reasons) (?:behind|for) (?:that|this|our|the) decision|what assumptions did we make)", intent, re.I):
        return _view_reply(state, message, "memory", reasoning=True)
    view = re.fullmatch(r"(?:show|open)(?: me)?(?: the| my| our)? (memory|remembered context|saved notes|decision (?:tool|model|calculator)|structure|relationships|audit(?: tool)?|evidence review|usage|tools)", intent, re.I)
    if view or intent.casefold() == "show me what you remember":
        name = view.group(1).casefold() if view else "memory"
        destination = ("memory" if name in {"memory", "remembered context", "saved notes"} else
                       "decision" if name.startswith("decision ") else
                       "structure" if name in {"structure", "relationships"} else
                       "audit" if name in {"audit", "audit tool", "evidence review"} else name)
        return _view_reply(state, message, destination)
    if re.fullmatch(r"(?:why did we decide that|why did we choose that|what was our reasoning)", intent, re.I):
        result["reply"], result["activities"] = _recall(state)
        return result
    if re.fullmatch(r"(?:what do you remember|what have you remembered|show me what you remember)", intent, re.I):
        result["reply"], result["activities"] = _recall(state, all_notes=True)
        return result
    if re.fullmatch(r"(?:keep that|accept that proposal|keep that suggestion)", intent, re.I):
        return _review_reply(state, "accept")
    if re.fullmatch(r"(?:reject that proposal|reject that suggestion|discard that suggestion)", intent, re.I):
        return _review_reply(state, "reject")
    selected = re.fullmatch(r"(keep|accept|reject) (?:suggestion|proposal) ([0-9]{1,6})", intent, re.I)
    if selected:
        return _review_reply(state, "reject" if selected.group(1).casefold() == "reject" else "accept", int(selected.group(2)))
    if re.fullmatch(r"let's (?:try|explore) (?:another direction|an alternative|a different direction)", intent, re.I):
        return {"reply": "Let’s explore that in a new branch.", "activities": [],
                "navigation": {"kind": "branch", "name": None}}
    branch = re.fullmatch(r"create a branch called\s+(.+)", intent, re.I)
    switch = re.fullmatch(r"(?:switch to|go back to)\s+(.+)", intent, re.I)
    if branch or switch:
        name = (branch or switch).group(1).strip("\"'“”")
        if not _safe_branch(name):
            return {"reply": "Use a branch name with letters, numbers, dots, underscores, slashes, or hyphens.",
                    "activities": [], "navigation": None}
        return {"reply": ("I’ll create a branch called " if branch else "I’ll switch to ") + name + ".",
                "activities": [], "navigation": {"kind": "branch" if branch else "switch", "name": name}}
    citation = _citation(message)
    decision_text = re.sub(r"^actually[, :]?\s+", "", plain, flags=re.I)
    decision = re.fullmatch(r"(?:" + _CHOICE_PREFIX + r")\s+(.+)", decision_text, re.I | re.S)
    if decision and flags["decided"]:
        parts = re.split(r"\bbecause\b", decision.group(1), maxsplit=1, flags=re.I)
        choice = parts[0].strip().rstrip(".! ")
        reason = parts[1].strip().rstrip(".! ") if len(parts) > 1 else None
        if not choice or reason == "":
            return result
        commitment = re.split(r"\bbecause\b", decision_text, maxsplit=1, flags=re.I)[0].strip().rstrip(".! ")
        replacement = re.fullmatch(r"(.+?)\s+instead of\s+(.+)", choice, re.I | re.S)
        old_choice = None
        if replacement:
            choice, old_choice = (part.strip().rstrip(".! ") for part in replacement.groups())
        choice_node = _node(choice, "decision", commitment, "decided", citation)
        operations = [choice_node]
        if reason is not None:
            reason_flags = _flags(reason)
            reason_kind = ("condition" if reason_flags["hypothetical"]
                           else "requirement" if re.search(r"\b(?:must|need|needs|required|requirement)\b", reason, re.I) else "assumption")
            reason_stance = "mentioned" if reason_flags["hypothetical"] else "considering" if reason_flags["tentative"] else "believed"
            reason_node = _node(reason, reason_kind, reason, reason_stance, citation)
            operations.extend([reason_node, _relation(choice_node["node"]["id"], reason_node["node"]["id"], "depends_on", citation)])
        if old_choice:
            for old in _active_nodes(state.get("relationships")):
                if (old["kind"] == "decision" and old.get("stance") == "decided"
                        and old["label"].strip().rstrip(".! ").casefold() == old_choice.casefold()):
                    operations.append(_relation(choice_node["node"]["id"], old["id"], "supersedes", citation))
                    break
        rationale = f"{choice} because {reason}." if reason is not None else f"Decision: {choice}."
        activity = capture(state, {"rationale": rationale[:4000], "operations": operations})
        reply = f"I remembered the decision to use {choice}."
        if reason is not None:
            reply += f" The recorded reason is: {reason}."
        return {"reply": reply,
                "activities": [activity], "navigation": None}
    tentative = re.fullmatch(r"(?:maybe\s+|i'm considering\s+|i am considering\s+)(.+)", plain, re.I | re.S)
    if tentative:
        option = tentative.group(1).strip().rstrip(".!? ")
        if option:
            activity = capture(state, {"rationale": "Considering " + option + ".", "operations": [
                _node(option, "claim", content, "considering", citation)]})
            return {"reply": f"I’ll keep {option} as an option you’re considering.", "activities": [activity], "navigation": None}
    remembered = re.fullmatch(r"remember that\s+(.+)", plain, re.I | re.S)
    if remembered:
        claim = remembered.group(1).strip()
        stance = _stance({"kind": "claim"}, content)
        activity = capture(state, {"rationale": claim, "operations": [_node(claim, "claim", claim, stance, citation)]})
        return {"reply": "I remembered that, with your original words attached.", "activities": [activity], "navigation": None}
    correction = re.fullmatch(r"actually[, :]?\s+(.+)", plain, re.I | re.S)
    if correction:
        updated = correction.group(1).strip()
        stance = _stance({"kind": "assumption"}, content)
        new = _node(updated, "assumption", updated, stance, citation)
        operations = [new]
        # Only an explicit replacement whose old wording names an existing
        # assumption/requirement earns a supersedes edge. "Actually" alone
        # does not establish a contradiction or replacement.
        replacement = re.search(r"\binstead of\s+(.+?)\.?$", updated, re.I)
        if replacement:
            old_text = replacement.group(1).strip().rstrip(".! ").casefold()
            for old in _active_nodes(state.get("relationships")):
                if old["kind"] in {"assumption", "requirement"} and old_text in {
                        old["label"].strip().rstrip(".! ").casefold(),
                        old.get("content", "").strip().rstrip(".! ").casefold()}:
                    operations.append(_relation(new["node"]["id"], old["id"], "supersedes", citation))
                    break
        activity = capture(state, {"rationale": updated, "operations": operations})
        reply = "I recorded the updated assumption and kept the earlier reasoning."
        if activity["affected_node_ids"]:
            reply += " The related decision is worth revisiting in light of that change."
        elif activity["possible_node_ids"]:
            earlier = state["relationships"]["nodes"][activity["possible_node_ids"][0]]["label"]
            reply += f" Does that change the earlier decision about {earlier}?"
        return {"reply": reply, "activities": [activity], "navigation": None}
    return result
