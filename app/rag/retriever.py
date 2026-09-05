"""Load offline-built, per-team FAISS indexes and search them for similar prompts.

The indexes and their sidecar metadata are produced by ``scripts/build_rag_index``.
Each team has its own subdirectory under the store (``<store>/<team>/``) holding a
``rag_index.faiss`` and a ``rag_meta.json``, so one team's history can never feed
another team's routing hint.

``Retriever`` fronts the whole store: it loads each team's index lazily on first
use and caches it in memory. A team with no directory, an empty index, a corrupt
file, or a failed search all yield an empty list: nothing here ever raises into
the caller, and a brand-new team with no history is just an ordinary empty result.

Similarity is cosine, implemented as an inner-product index (IndexFlatIP) over
L2-normalized vectors: the build normalizes the stored vectors and the query is
normalized here, so the inner product is the cosine similarity in [-1, 1].
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("slice.gateway")

# The two on-disk artifacts per team, shared with the build script.
INDEX_FILENAME = "rag_index.faiss"
META_FILENAME = "rag_meta.json"

# Team ids become directory names, so map anything outside a safe set to "_" (and
# never let a team resolve to an empty/".."/"." name that could escape the store).
# The build script and the retriever must agree on this, so it lives here.
_UNSAFE_TEAM = re.compile(r"[^A-Za-z0-9_.-]")


def team_dir_name(team: str) -> str:
    """Filesystem-safe subdirectory name for a team's index."""
    name = _UNSAFE_TEAM.sub("_", team or "default")
    return name if name not in ("", ".", "..") else "default"


def _import_faiss():
    """Import faiss, but pull in the embedding model (torch) first.

    On macOS, importing faiss *before* torch initializes segfaults on an OpenMP
    double-load. Importing the embeddings module first guarantees torch is loaded
    before faiss, whichever call site touches faiss first. Both imports are cached,
    so the model is loaded at most once.
    """
    import app.rag.embeddings  # noqa: F401  # force torch-before-faiss ordering.
    import faiss

    return faiss


@dataclass(frozen=True)
class Neighbor:
    """One retrieved past request: its metadata plus the similarity score.

    ``score`` is cosine similarity in [-1, 1] (higher is closer). The rest is
    whatever the index build stored for the row, any field may be None if it
    wasn't present on the original request.
    """

    score: float
    model: str | None = None
    cost_usd: float | None = None
    verdict: str | None = None
    routed_from: str | None = None
    prompt: str | None = None


class _TeamIndex:
    """One team's loaded FAISS index plus its row metadata, or an empty stand-in.

    Loading failures are swallowed and leave the index empty, so a bad or missing
    file degrades to "no hint" instead of an error.
    """

    def __init__(self, index_dir: str | Path):
        self._dir = Path(index_dir)
        self._index = None
        self._meta: list[dict] = []
        self._load()

    def _load(self) -> None:
        index_path = self._dir / INDEX_FILENAME
        meta_path = self._dir / META_FILENAME
        if not index_path.is_file() or not meta_path.is_file():
            logger.info(
                json.dumps({"event": "rag_index_absent", "dir": str(self._dir)})
            )
            return
        try:
            # Imported lazily (torch-first, see _import_faiss) so a deployment with
            # no index never pays the model/faiss import cost at all.
            faiss = _import_faiss()

            index = faiss.read_index(str(index_path))
            meta = json.loads(meta_path.read_text())
            if not isinstance(meta, list):
                raise ValueError("rag meta is not a list")
        except Exception as exc:  # noqa: BLE001  # a broken index is just "no hint".
            logger.warning(
                json.dumps({"event": "rag_index_load_failed", "error": str(exc)})
            )
            return
        self._index = index
        self._meta = meta

    @property
    def empty(self) -> bool:
        """True when there is nothing to search, no index, or a zero-vector one."""
        return self._index is None or self._index.ntotal == 0

    def search(self, prompt: str, k: int) -> list[Neighbor]:
        """Up to ``k`` nearest past prompts, closest first. Never raises."""
        if self.empty or not (prompt or "").strip() or k <= 0:
            return []
        try:
            faiss = _import_faiss()

            from app.rag.embeddings import embed_one

            vector = embed_one(prompt).reshape(1, -1).astype("float32")
            faiss.normalize_L2(vector)
            limit = min(k, self._index.ntotal)
            scores, indices = self._index.search(vector, limit)
            neighbors: list[Neighbor] = []
            for score, position in zip(scores[0], indices[0]):
                if position < 0 or position >= len(self._meta):
                    continue
                neighbors.append(self._neighbor(float(score), self._meta[position]))
            return neighbors
        except Exception as exc:  # noqa: BLE001  # search must never break routing.
            logger.warning(
                json.dumps({"event": "rag_retrieve_failed", "error": str(exc)})
            )
            return []

    @staticmethod
    def _neighbor(score: float, row: dict) -> Neighbor:
        row = row if isinstance(row, dict) else {}
        return Neighbor(
            score=score,
            model=row.get("model"),
            cost_usd=row.get("cost_usd"),
            verdict=row.get("verdict"),
            routed_from=row.get("routed_from"),
            prompt=row.get("prompt"),
        )


class Retriever:
    """Per-team RAG retriever over a store of ``<store>/<team>/`` indexes.

    Construct it with the store's base directory. Each team's index is loaded and
    cached on first ``retrieve``; a team with no directory (a brand-new team, say)
    caches an empty index and returns nothing. ``retrieve`` never raises.
    """

    def __init__(self, base_dir: str | Path):
        self._base = Path(base_dir)
        self._cache: dict[str, _TeamIndex] = {}
        self._lock = threading.Lock()

    def retrieve(self, team: str, prompt: str, k: int = 5) -> list[Neighbor]:
        """Up to ``k`` nearest past prompts for ``team``, closest first. Never raises."""
        try:
            return self._team_index(team).search(prompt, k)
        except Exception as exc:  # noqa: BLE001  # belt-and-suspenders; never raise.
            logger.warning(
                json.dumps({"event": "rag_retrieve_failed", "error": str(exc)})
            )
            return []

    def _team_index(self, team: str) -> _TeamIndex:
        # Runs inside asyncio.to_thread, so guard the shared cache with a lock. The
        # first lookup for a team loads its index (cheap for a missing dir); later
        # lookups reuse it.
        key = team_dir_name(team)
        with self._lock:
            index = self._cache.get(key)
            if index is None:
                index = _TeamIndex(self._base / key)
                self._cache[key] = index
            return index


def _has_any_index(base: Path) -> bool:
    """True when at least one team subdirectory under ``base`` holds a built index.

    With no index there is nothing to search, so the heavy embedding model is never
    needed and the startup warm-up is skipped entirely (an unconfigured RAG store costs
    nothing at boot). Any filesystem trouble reading the store is treated as "no index".
    """
    try:
        return any((child / INDEX_FILENAME).is_file() for child in base.iterdir() if child.is_dir())
    except OSError:
        return False


def warm() -> None:
    """Force the heavy RAG dependency to load NOW: the embedding model, then faiss.

    Blocking and potentially slow (or, with a broken/missing dependency, raising).
    ``load_default`` runs this once at startup inside a bounded daemon thread so a
    crash or a hang here can never take down or wedge the server: it just disables RAG.
    """
    _import_faiss()  # imports app.rag.embeddings (constructs the model), then faiss


def load_default() -> "Retriever | None":
    """Build the per-team retriever for the configured store dir, fail-open.

    Returns a ready ``Retriever`` normally, or ``None`` when RAG cannot be made ready,
    a missing or broken embedding dependency, a model that will not load, or a load that
    runs past ``RAG_WARM_TIMEOUT_SECONDS``, logging exactly one structured line. ``None``
    means the gateway starts and serves traffic with retrieval simply returning nothing.

    The expensive embedding model is loaded HERE, once, in a daemon thread bounded by the
    timeout: a missing dependency (an instant ImportError), a broken one, or a stuck
    import are all caught or timed out, so startup never hangs or crashes on RAG. When the
    store has no index there is nothing to serve, so the warm-up is skipped and a cheap,
    lazy retriever is returned unchanged.
    """
    from app import config

    try:
        retriever = Retriever(config.RAG_INDEX_DIR)
    except Exception as exc:  # noqa: BLE001  # construction is trivial, but never crash boot.
        logger.warning(
            json.dumps({"event": "rag_load_failed", "stage": "construct", "error": str(exc)})
        )
        return None

    if not _has_any_index(retriever._base):
        # Nothing to search: skip the model load, keep the (cheap) retriever. Retrieval
        # will report "rag_index_absent" per team and never touch the heavy dependency.
        return retriever

    timeout = config.RAG_WARM_TIMEOUT_SECONDS
    outcome: dict = {}

    def _run() -> None:
        started = time.monotonic()
        try:
            warm()
            outcome["ok"] = round(time.monotonic() - started, 3)
        except Exception as exc:  # noqa: BLE001  # ImportError, model-load failure, anything.
            outcome["error"] = str(exc)

    thread = threading.Thread(target=_run, name="rag-warm", daemon=True)
    thread.start()
    thread.join(timeout)

    if thread.is_alive():
        # The load is stuck; abandon the daemon thread and start without RAG.
        logger.warning(
            json.dumps({"event": "rag_load_failed", "stage": "warm_timeout", "timeout_s": timeout})
        )
        return None
    if "error" in outcome:
        logger.warning(
            json.dumps({"event": "rag_load_failed", "stage": "warm", "error": outcome["error"]})
        )
        return None

    logger.info(json.dumps({"event": "rag_ready", "warm_seconds": outcome.get("ok")}))
    return retriever
