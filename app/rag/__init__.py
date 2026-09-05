"""Phase 6: semantic retrieval over past request prompts.

Three small pieces, all optional and all fail-open:

- ``embeddings``: a local sentence-transformers model, loaded once, CPU only.
- ``prompt``: pull the user prompt out of an Anthropic request body for logging.
- ``retriever``: load an offline-built FAISS index and search it for a hint.

Nothing here is on the request hot path except a single retriever search, and that
runs only on the auto-routing path and never raises. RAG is a hint to the judge,
never a rule.
"""
