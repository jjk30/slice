"""Build the per-team RAG indexes from past requests. Run offline, off the hot path.

    python -m scripts.build_rag_index

Reads every successful, billed request that has a stored prompt, groups them by
team, and writes one FAISS index (plus a JSON sidecar of per-row metadata) per team
under ``RAG_INDEX_DIR/<team>/``, so one team's history never feeds another's hint.
Cosine similarity is done as an inner-product index over L2-normalized vectors. A
team with no eligible rows simply gets no directory; the retriever treats a missing
team as an empty result. It prints the rows indexed per team.

This is a build tool, not part of the server: it imports the heavy embedding model
and talks to Postgres directly. Nothing here runs during request handling.
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections import defaultdict
from pathlib import Path

import asyncpg
import numpy as np

# Import order matters: the embedding model (torch) MUST load before faiss, or the
# process segfaults on an OpenMP double-load on macOS. embed_texts pulls torch in;
# only then do we import faiss. See app.rag.retriever._import_faiss for the detail.
from app.rag.embeddings import embed_texts  # noqa: E402 — must precede faiss.
import faiss  # noqa: E402

from app import config
from app.rag.retriever import INDEX_FILENAME, META_FILENAME, team_dir_name

# Only rows worth learning from: a real, successful, billed call with a prompt.
# team may be NULL on rows that predate the column; those fold into "default".
SELECT_ROWS = """
SELECT team, model, cost_usd, routed_from, prompt_text
FROM requests
WHERE status = 200 AND cost_usd > 0 AND prompt_text IS NOT NULL
ORDER BY id
"""

# How much of the prompt to keep in the sidecar for the judge-facing snippet.
SNIPPET_CHARS = 200


async def _fetch_rows(dsn: str) -> list[dict]:
    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch(SELECT_ROWS)
    finally:
        await conn.close()
    return [dict(row) for row in rows]


def _meta_for(row: dict) -> dict:
    prompt = row.get("prompt_text") or ""
    cost = row.get("cost_usd")
    return {
        "model": row.get("model"),
        # asyncpg returns NUMERIC as Decimal; JSON needs a plain float.
        "cost_usd": float(cost) if cost is not None else None,
        "routed_from": row.get("routed_from"),
        "prompt": prompt[:SNIPPET_CHARS],
    }


def _write_index(vectors: np.ndarray, meta: list[dict], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    # Dimension from the vectors when present, else probe the model on an empty
    # embed so the empty index is still a valid, correctly-shaped IndexFlatIP.
    dim = vectors.shape[1] if vectors.size else embed_texts([]).shape[1]
    index = faiss.IndexFlatIP(dim)
    if vectors.size:
        vectors = vectors.astype("float32", copy=False)
        faiss.normalize_L2(vectors)  # cosine similarity via inner product.
        index.add(vectors)
    faiss.write_index(index, str(out_dir / INDEX_FILENAME))
    (out_dir / META_FILENAME).write_text(json.dumps(meta))


async def build() -> int:
    if not config.DATABASE_URL:
        print("DATABASE_URL is not set; nothing to index.", file=sys.stderr)
        return 1

    base = Path(config.RAG_INDEX_DIR)
    rows = await _fetch_rows(config.DATABASE_URL)

    if not rows:
        print("No eligible rows found; nothing to index.")
        return 0

    # Group by team so each team gets its own isolated index.
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        groups[row.get("team") or "default"].append(row)

    for team, team_rows in sorted(groups.items()):
        vectors = embed_texts([row["prompt_text"] for row in team_rows])
        meta = [_meta_for(row) for row in team_rows]
        out_dir = base / team_dir_name(team)
        _write_index(vectors, meta, out_dir)
        print(f"Indexed {len(team_rows)} rows for team '{team}' to {out_dir}/.")

    print(f"Done: {len(groups)} team(s), {len(rows)} rows total.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(build()))
