"""Local sentence-transformers embeddings for the RAG index.

The model (``config.RAG_EMBED_MODEL``, default ``all-MiniLM-L6-v2``, 384-dim) is
loaded once at import and reused for every call. It runs on CPU, no GPU assumed,
no network at inference time once the weights are cached. Vectors are returned
unnormalized; the FAISS layer (index build and retriever) normalizes them for
cosine similarity via an IndexFlatIP.
"""

from __future__ import annotations

import numpy as np
from sentence_transformers import SentenceTransformer

from app import config

MODEL_NAME = config.RAG_EMBED_MODEL

# Loaded once, at import, and shared by every call. This is the expensive step, so
# importing this module is deliberately heavy and everything downstream is cheap.
_model = SentenceTransformer(MODEL_NAME, device="cpu")

# Probe once for the embedding width rather than the deprecated accessor, so an
# empty input can still return a correctly-shaped (0, dim) array.
_DIM = int(_model.encode(["_"], convert_to_numpy=True).shape[1])


def embed_texts(texts: list[str]) -> np.ndarray:
    """Embed a list of strings into a ``(len(texts), dim)`` float32 array.

    An empty list returns an empty ``(0, dim)`` array rather than raising, so
    callers can treat "nothing to embed" as an ordinary, shape-consistent case.
    """
    if not texts:
        return np.empty((0, _DIM), dtype=np.float32)
    vectors = _model.encode(list(texts), convert_to_numpy=True)
    return vectors.astype(np.float32, copy=False)


def embed_one(text: str) -> np.ndarray:
    """Embed a single string into a 1-D ``(dim,)`` float32 array."""
    return embed_texts([text])[0]
