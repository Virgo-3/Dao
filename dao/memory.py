"""Derived, local relevance index over immutable state snapshots.

Uses ordinary SQLite tables and positive-IDF BM25 scoring (k1=1.2, b=0.75).
No SQLite extensions, embeddings, network access, or model calls are required.
"""

import hashlib
import json
import math
import unicodedata
from collections import Counter


MAX_QUERY_CHARS = 12000
MAX_QUERY_TERMS = 64
EXCERPT_CHARS = 1200
INDEX_VERSION = 2
_STOP = frozenset("a an and are as at be been but by can could did do does for from had has have how i if in into is it its me my of on or our should so that the their them there these they this to was we were what when where which who why will with would you your".split())


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _word(value):
    value = unicodedata.normalize("NFKC", unicodedata.normalize("NFKC", value).casefold())
    # Unattached leading marks are not words; preserve marks attached to letters.
    for index, character in enumerate(value):
        if character.isalnum():
            return value[index:]
    return ""


def _tokens(text):
    """Yield normalized words with their start offsets in the original text.

    Decomposition before word splitting preserves combining marks and handles
    compatibility characters that expand into letters or separators. Normalize
    each complete word before case folding so mark reordering stays correct.
    """
    characters, start = [], None
    for position, character in enumerate(text):
        for part in unicodedata.normalize("NFKD", character):
            if part.isalnum() or unicodedata.category(part).startswith("M"):
                # Ignore unattached marks when locating the excerpt. A few
                # marks (such as Greek ypogegrammeni) case-fold into letters.
                if start is None and any(char.isalnum() for char in part.casefold()):
                    start = position
                characters.append(part)
            elif characters:
                word = _word("".join(characters))
                if word:
                    yield word, start
                characters.clear()
                start = None
    if characters:
        word = _word("".join(characters))
        if word:
            yield word, start


def terms(text):
    return [word for word, _ in _tokens(text) if word not in _STOP]


def _pointer(key):
    return str(key).replace("~", "~0").replace("/", "~1")


def documents(state):
    """Deduplicate inherited records by value, retaining a verifiable source path."""
    for key, value in state.items():
        if key == "notes" and isinstance(value, dict):
            items = [(f"/{_pointer(key)}/{_pointer(name)}", {name: note}) for name, note in value.items()]
        elif isinstance(value, list):
            items = [(f"/{_pointer(key)}/{index}", item) for index, item in enumerate(value)]
        else:
            items = [(f"/{_pointer(key)}", value)]
        for path, item in items:
            identity = hashlib.sha256(_json([key, item]).encode("utf-8")).hexdigest()
            role = item.get("role") if key == "messages" and isinstance(item, dict) else None
            if role in ("user", "assistant") and isinstance(item.get("content"), str):
                content = item["content"]
                kind = "message:" + role
            else:
                content, kind = _json({key: item}), key
            yield {"id": identity, "kind": kind, "content": content, "path": path}


def create_schema(db):
    # These are rebuildable indexes, not changes to the immutable commit schema.
    db.execute("""CREATE TABLE IF NOT EXISTS memory_documents (
        id TEXT PRIMARY KEY, kind TEXT NOT NULL, content TEXT NOT NULL,
        source_commit TEXT NOT NULL REFERENCES commits(id), source_path TEXT NOT NULL,
        source_sequence INTEGER NOT NULL, length INTEGER NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS memory_terms (
        term TEXT NOT NULL, document_id TEXT NOT NULL REFERENCES memory_documents(id),
        frequency INTEGER NOT NULL, PRIMARY KEY(term, document_id))""")
    db.execute("""CREATE TABLE IF NOT EXISTS memory_indexed (
        commit_id TEXT PRIMARY KEY REFERENCES commits(id))""")
    db.execute("""CREATE TABLE IF NOT EXISTS memory_index_version (
        id INTEGER PRIMARY KEY CHECK (id = 1), version INTEGER NOT NULL)""")


def synchronize(db, read_commit):
    """Backfill every unindexed checkpoint, then only index newly saved snapshots."""
    # Store.search holds one write transaction for the reset and backfill. If
    # validation or indexing fails, the old cache and version are restored.
    version = db.execute("SELECT version FROM memory_index_version WHERE id = 1").fetchone()
    if version is not None and version["version"] not in (1, INDEX_VERSION):
        raise ValueError("Unsupported memory index version. Update Dao before searching this database.")
    rebuild = version is None or version["version"] != INDEX_VERSION
    if rebuild:
        db.execute("DELETE FROM memory_terms")
        db.execute("DELETE FROM memory_documents")
        db.execute("DELETE FROM memory_indexed")
    pending = db.execute("""SELECT c.id, c.rowid AS sequence FROM commits c
        LEFT JOIN memory_indexed i ON i.commit_id = c.id
        WHERE i.commit_id IS NULL ORDER BY c.rowid""")
    for row in pending:
        commit = read_commit(db, row["id"])
        for doc in documents(commit["state"]):
            if db.execute("SELECT 1 FROM memory_documents WHERE id = ?", (doc["id"],)).fetchone():
                continue
            counts = Counter(terms(doc["content"]))
            db.execute("INSERT INTO memory_documents VALUES (?, ?, ?, ?, ?, ?, ?)",
                       (doc["id"], doc["kind"], doc["content"], commit["id"], doc["path"],
                        row["sequence"], sum(counts.values())))
            db.executemany("INSERT INTO memory_terms VALUES (?, ?, ?)",
                           ((term, doc["id"], frequency) for term, frequency in counts.items()))
        db.execute("INSERT INTO memory_indexed VALUES (?)", (commit["id"],))
    if rebuild:
        db.execute("""INSERT INTO memory_index_version VALUES (1, ?)
            ON CONFLICT(id) DO UPDATE SET version = excluded.version""", (INDEX_VERSION,))


def excerpt(content, query_terms):
    """Find a short, contiguous source excerpt near the densest query-term window."""
    if len(content) <= EXCERPT_CHARS:
        return content, 0, len(content)
    matches = [(position, word) for word, position in _tokens(content) if word in query_terms]
    best, start, right, counts = (-1, -1), 0, 0, Counter()
    for left, (position, term) in enumerate(matches):
        while right < len(matches) and matches[right][0] - position < EXCERPT_CHARS - 160:
            counts[matches[right][1]] += 1
            right += 1
        quality = (len(counts), right - left)
        if quality > best:
            best, start = quality, max(0, position - 80)
        counts[term] -= 1
        if counts[term] == 0:
            del counts[term]
    start = min(start, len(content) - EXCERPT_CHARS)
    end = start + EXCERPT_CHARS
    return content[start:end], start, end


def _source_branches(db, sources):
    parents = {row["id"]: json.loads(row["parents"]) for row in db.execute("SELECT id, parents FROM commits")}
    found = {source: [] for source in sources}
    for branch in db.execute("SELECT name, head FROM branches ORDER BY name"):
        pending, seen = [branch["head"]], set()
        while pending:
            commit = pending.pop()
            if commit in seen:
                continue
            seen.add(commit)
            if commit in found:
                found[commit].append(branch["name"])
            if commit not in parents:
                raise ValueError("A memory source has a missing parent checkpoint.")
            pending.extend(parents[commit])
    return found


def search(db, query, current, read_commit, *, limit=8, max_chars=16000):
    if not isinstance(query, str) or not query.strip() or len(query) > MAX_QUERY_CHARS:
        raise ValueError(f"Search needs 1–{MAX_QUERY_CHARS} characters.")
    if type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError("Search limit must be an integer from 1 to 50.")
    if type(max_chars) is not int or not 4000 <= max_chars <= 100000:
        raise ValueError("Search context budget must be between 4,000 and 100,000 characters.")
    query.encode("utf-8")
    synchronize(db, read_commit)
    all_terms = list(dict.fromkeys(terms(query)))
    query_terms = all_terms[:MAX_QUERY_TERMS]
    stats = db.execute("SELECT COUNT(*) AS n, COALESCE(AVG(length), 1) AS average FROM memory_documents").fetchone()
    result = {"scope": "entire_tree", "current_checkpoint": current["id"],
              "indexed_checkpoints": db.execute("SELECT COUNT(*) FROM memory_indexed").fetchone()[0],
              "indexed_documents": stats["n"], "query_terms": query_terms,
              "query_terms_truncated": len(all_terms) > len(query_terms),
              "results_truncated": False, "results": []}
    weights = []
    for term in query_terms:
        frequency = db.execute("SELECT COUNT(*) FROM memory_terms WHERE term = ?", (term,)).fetchone()[0]
        if frequency:
            weights.extend((term, math.log1p((stats["n"] - frequency + 0.5) / (frequency + 0.5))))
    if not weights:
        return result
    values = ",".join("(?, ?)" for _ in range(len(weights) // 2))
    # Only placeholder counts are interpolated. Search text is always bound data.
    rows = db.execute(f"""WITH query_terms(term, idf) AS (VALUES {values})
        SELECT d.*, SUM(q.idf * (t.frequency * 2.2) /
            (t.frequency + 1.2 * (0.25 + 0.75 * d.length / ?))) AS relevance
        FROM query_terms q JOIN memory_terms t ON t.term = q.term
        JOIN memory_documents d ON d.id = t.document_id
        GROUP BY d.id ORDER BY relevance DESC, d.source_sequence DESC, d.id
        LIMIT ?""", (*weights, max(1, stats["average"]), limit + 1)).fetchall()
    result["results_truncated"] = len(rows) > limit
    rows = rows[:limit]
    branches = _source_branches(db, {row["source_commit"] for row in rows})
    current_ids = {doc["id"] for doc in documents(current["state"])}
    verified = {}
    used = 0
    for row in rows:
        source = row["source_commit"]
        if source not in verified:
            commit = read_commit(db, source)
            verified[source] = {doc["id"]: doc for doc in documents(commit["state"])}
        doc = verified[source].get(row["id"])
        if not doc or doc["content"] != row["content"]:
            raise ValueError("The memory index does not match its source checkpoint.")
        text, start, end = excerpt(doc["content"], set(query_terms))
        hit = {"document_id": row["id"], "kind": doc["kind"], "score": round(row["relevance"], 6),
               "source_commit": source, "source_path": doc["path"],
               "source_branches": branches[source][:8], "source_branch_count": len(branches[source]),
               "in_current_state": row["id"] in current_ids, "excerpt": text,
               "excerpt_start": start, "excerpt_end": end, "content_chars": len(doc["content"])}
        cost = len(_json(hit))
        if used + cost > max_chars:
            result["results_truncated"] = True
            break
        result["results"].append(hit)
        used += cost
    return result
