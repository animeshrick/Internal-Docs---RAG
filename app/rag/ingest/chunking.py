"""
chunking.py - turn parsed documents (rag.ingest.parsing.parse_docx output) into chunks.

Method: structure-aware. Blocks are grouped by top-level section and packed up
to CONFIG["max_tokens"]. Tables are their own chunks. A block is only split
(sentence-level, with overlap) when it is bigger than the cap by itself.

    chunk_document(info)  -> (chunks, reason_if_skipped)
    chunk_all(docs)       -> (chunks, skipped_docs)
    check_chunks(docs, chunks) -> list of problems
    write_jsonl(chunks, path)
"""
import hashlib
import json
import re
from itertools import groupby

from app.rag.ingest.parsing import chunk_metadata

CONFIG = {
    "max_tokens": 450,                 # cap per chunk (approximate tokens)
    "min_tokens": 30,                  # smaller text chunks get merged into a neighbour
    "overlap_ratio": 0.10,             # only used when ONE block is split
    "allowed_status": {"Approved"},    # documents with another status are not indexed
}


def est_tokens(s):
    """Rough token count (1 token ~ 4 characters). Swap for the embedding model's tokenizer later."""
    return max(1, round(len(s) / 4))


# ------------------------------------------------------------------ helpers
def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:40] or "section"


def section_key(b):
    hp = b["heading_path"]
    return hp[1] if len(hp) > 1 else (hp[0] if hp else "")


def split_sentences(text):
    return [p for p in re.split(r"(?<=[.!?;])\s+", text) if p]


def common_prefix(paths):
    out = []
    for parts in zip(*paths):
        if all(p == parts[0] for p in parts):
            out.append(parts[0])
        else:
            break
    return out


def pack_pieces(pieces, cap, overlap_tokens):
    """Pack text pieces (sentences or table lines) into groups <= cap. Overlap only for sentences."""
    # groups, cur, cur_t = [], [], 0
    groups = []  # Completed chunks
    cur = []     # Currently building chunk
    cur_t = 0    # Estimated tokens in current chunk
    for p in pieces:
        t = est_tokens(p)
        if cur and cur_t + t > cap:
            groups.append(cur)
            tail = cur[-1]
            cur, cur_t = ([tail], est_tokens(tail)) if overlap_tokens and est_tokens(tail) <= overlap_tokens else ([], 0)
        cur.append(p)
        cur_t += t
    if cur:
        groups.append(cur)
    return groups


# ------------------------------------------------------------ one document
def chunk_document(info, config=CONFIG):
    status = info.get("status")
    if status is None:
        if info.get("doc_type") != "reference":
            return [], "no status and not a reference document"
    elif status not in config["allowed_status"]:
        return [], f"status '{status}' is not indexed"

    cap, min_t = config["max_tokens"], config["min_tokens"]
    overlap_t = int(cap * config["overlap_ratio"])
    base_meta = chunk_metadata(info)
    doc_id = info["doc_id"]

    chunks = [] # stores the final output dictionaries
    used_ids = set() # tracks section IDs so duplicate section names don't produce identical IDs

    for sec_title, grp in groupby(info["blocks"], key=section_key): 
        '''
        section_key() identifies the section to which each block belongs
        info["blocks"] = [
            {"type": "heading", "text": "Annual Leave"},
            {"type": "text", "text": "18 days leave"},
            {"type": "heading", "text": "Sick Leave"},
            {"type": "text", "text": "12 days leave"}
        ]


        groupby() groups consecutive blocks with the same section key
        [
            ("Annual Leave", [heading1, text1]),
            ("Sick Leave", [heading2, text2])
        ]
        '''
        blocks = list(grp)
        if all(b["type"] == "heading" for b in blocks):
            continue                                        # title-only group: nothing to index

        # section id create
        section_id = f"{doc_id}#{slug(sec_title)}"
        n = 2
        while section_id in used_ids:                           # two sections with the same title
            section_id, n = f"{doc_id}#{slug(sec_title)}-{n}", n + 1
        used_ids.add(section_id)

        section_chunks = []                                     # dicts: kind, paths, lines, types

        def emit(kind, lines, paths, types):
            section_chunks.append({"kind": kind, "lines": lines, "paths": paths, "types": types})

        cur, cur_t, intro = [], 0, None
        '''
            cur	    Blocks currently being accumulated into a chunk
            cur_t	Estimated token count of those blocks
            intro	A short introductory block that may be repeated when a list continues
        '''

        def flush():
            nonlocal cur, cur_t # When I assign to cur and cur_t, modify the variables in the enclosing function, not new local variables inside flush()
            body = [b for b in cur if b["type"] != "heading"]
            if body:
                prefix = common_prefix([b["heading_path"] for b in body])
                lines = []
                for b in cur:
                    if b["type"] == "heading" and prefix and b["text"] == prefix[-1]:
                        continue                            # already in the prefix
                    lines.append(b["text"])
                emit("text", lines, [prefix], {b["type"] for b in body})
            cur, cur_t = [], 0

        for b in blocks:
            t = b["type"]
            if t == "table":
                flush()
                intro = None
                prefix = b["heading_path"]
                for grp_lines in pack_pieces(b["lines"], cap, 0):
                    emit("table", grp_lines, [prefix], {"table"})
                continue
            if t == "heading":
                intro = None
                if cur_t >= cap * 0.6:                      # prefer to break at a sub-heading
                    flush()
                cur.append(b)
                cur_t += est_tokens(b["text"])
                continue

            bt = est_tokens(b["text"])
            if bt > cap:                                    # one block bigger than the cap: split it
                flush()
                for piece in pack_pieces(split_sentences(b["text"]), cap, overlap_t):
                    emit("text", [" ".join(piece)], [b["heading_path"]], {t})
                continue
            if cur and cur_t + bt > cap:
                flush()
                if t == "list_item" and intro is not None:  # keep the lead-in sentence with a continued list
                    cur, cur_t = [intro], est_tokens(intro["text"])
            cur.append(b)
            cur_t += bt
            if t in ("text", "callout"):
                intro = b if bt <= 60 else None
        flush()

        # merge tiny text chunks into an adjacent text chunk of the same section
        merged = []
        for c in section_chunks:
            if (merged and c["kind"] == "text" and merged[-1]["kind"] == "text"
                    and (est_tokens("\n".join(c["lines"])) < min_t or est_tokens("\n".join(merged[-1]["lines"])) < min_t)
                    and est_tokens("\n".join(merged[-1]["lines"] + c["lines"])) <= cap):
                merged[-1]["lines"] += c["lines"]
                merged[-1]["paths"] += c["paths"]
                merged[-1]["types"] |= c["types"]
            else:
                merged.append(c)

        for i, c in enumerate(merged):
            prefix = common_prefix(c["paths"]) or [info["title"]]
            text = " > ".join(prefix) + "\n" + "\n".join(c["lines"])
            meta = {**base_meta, "heading_path": prefix, "section": sec_title,
                    "section_id": section_id, "chunk_index": i, "block_types": sorted(c["types"]),
                    "kind": c["kind"]}
            chunks.append({
                "chunk_id": f"{section_id}#{i:02d}",
                "text": text,
                "tokens": est_tokens(text),
                "metadata": meta,
                "hash": hashlib.sha256((text + json.dumps(meta, sort_keys=True)).encode()).hexdigest()[:16],
            })
    return chunks, None


def chunk_all(docs, config=CONFIG):
    chunks, skipped = [], []
    for d in docs:
        c, why = chunk_document(d, config)
        if why:
            skipped.append((d["source"], why))
        chunks += c
    return chunks, skipped


# --------------------------------------------------------------- validation
def check_chunks(docs, chunks, config=CONFIG):
    problems, cap, min_t = [], config["max_tokens"], config["min_tokens"]
    ids = [c["chunk_id"] for c in chunks]
    for cid, n in __import__("collections").Counter(ids).items():
        if n > 1:
            problems.append(f"ERROR duplicate chunk_id {cid}")
    for c in chunks:
        body = c["text"].split("\n", 1)[1] if "\n" in c["text"] else ""
        if not body.strip():
            problems.append(f"ERROR empty chunk {c['chunk_id']}")
        if c["tokens"] > cap * 1.1:
            problems.append(f"WARN {c['chunk_id']} is {c['tokens']} tokens (cap {cap})")
        if c["tokens"] < min_t and c["metadata"]["kind"] == "text":
            problems.append(f"WARN {c['chunk_id']} is only {c['tokens']} tokens")
        if " > " not in c["text"].split("\n")[0] and c["text"].split("\n")[0] != c["metadata"]["title"]:
            problems.append(f"WARN {c['chunk_id']} has no heading-path prefix")
        if None in c["metadata"].values():
            problems.append(f"ERROR {c['chunk_id']} has a null metadata value")
    # coverage: every table row and every sentence of every body block must appear in a chunk
    by_doc = {}
    for c in chunks:
        by_doc.setdefault(c["metadata"]["doc_id"], []).append(c["text"])
    indexed = {d["doc_id"]: d for d in docs if d["doc_id"] in by_doc}
    for doc_id, d in indexed.items():
        blob = "\n".join(by_doc[doc_id])
        for i, b in enumerate(d["blocks"]):
            if b["type"] == "heading":
                continue
            pieces = b["lines"] if b["type"] == "table" else split_sentences(b["text"])
            for p in pieces:
                if p not in blob:
                    problems.append(f"ERROR {doc_id} block {i} ({b['type']}) text missing from chunks: {p[:60]!r}")
    return problems


def write_jsonl(chunks, path):
    with open(path, "w", encoding="utf8") as f:
        for c in chunks:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    import collections
    import statistics

    from app.core.logs import setup_logging
    from app.core.paths import CHUNKS_FILE_PATH, RAW_DOCS_DIR
    from app.rag.ingest.parsing import parse_folder

    setup_logging()

    docs, _ = parse_folder(RAW_DOCS_DIR)
    chunks, skipped = chunk_all(docs)
    print(f"\n{len(chunks)} chunks from {len(docs) - len(skipped)} documents; skipped: {skipped or 'none'}")
    per = collections.Counter(c["metadata"]["doc_id"] for c in chunks)
    for doc_id, n in per.items():
        print(f"  {doc_id:<14} {n:>3} chunks")
    sizes = [c["tokens"] for c in chunks]
    print(f"tokens per chunk: min {min(sizes)}, median {int(statistics.median(sizes))}, max {max(sizes)}")
    problems = check_chunks(docs, chunks)
    print("problems:", problems or "none")
    CHUNKS_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    write_jsonl(chunks, CHUNKS_FILE_PATH)
    print("written:", CHUNKS_FILE_PATH)
