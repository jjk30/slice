"""Phase-6 RAG tests: embeddings, the per-team retriever, and the router hint.

Split by cost. The embeddings and seeded-index tests load the real local model —
nothing here reaches a network at inference once the weights are cached. The router
tests lean on fakes: a ``FakeRetriever`` of canned neighbors and the same
``SpyClassify`` judge the phase-5 suite uses, so the RAG wiring can be checked
without touching the model, Redis, or Postgres.

The load-bearing tests are team isolation (one team's history never feeds another's
hint) and the empty fallback (a new team, or a missing/empty index, degrades to "no
hint" while the router still produces a valid pick from the config order).
"""

import fakeredis.aioredis
import httpx
import numpy as np
import pytest
import respx

from app import config, judge
from app.db import RequestRecord
from app.main import app
from app.rag import embeddings
from app.rag.prompt import MAX_PROMPT_CHARS, extract_prompt_text
from app.rag.retriever import (
    INDEX_FILENAME,
    META_FILENAME,
    Neighbor,
    Retriever,
    team_dir_name,
)
from app.router import route
from app.rules import RulesCache

MESSAGES_URL = f"{config.ANTHROPIC_BASE_URL}/v1/messages"

PROVIDER_RESPONSE = {
    "id": "msg_01",
    "type": "message",
    "role": "assistant",
    "content": [{"type": "text", "text": "hello"}],
    "usage": {"input_tokens": 1000, "output_tokens": 500},
}

OPUS = "claude-opus-5"
HAIKU = "claude-haiku-4-5-20251001"
EASY_MODEL = config.ROUTE_EASY_MODEL

REQUEST = {
    "model": OPUS,
    "max_tokens": 64,
    "messages": [{"role": "user", "content": "write a function"}],
}


# --- Fakes -----------------------------------------------------------------


class SpyClassify:
    """A fake judge: records the hint it was handed and returns a fixed verdict."""

    def __init__(self, verdict="easy"):
        self.verdict = verdict
        self.hints: list[str | None] = []

    async def __call__(self, text, model, headers, client, *, hint=None):
        self.hints.append(hint)
        return judge.JudgeResult(self.verdict)


class FakeRetriever:
    """A per-team retriever stand-in: canned neighbors, recording the team asked."""

    def __init__(self, neighbors=None):
        self._neighbors = list(neighbors or [])
        self.teams: list[str] = []

    @property
    def calls(self) -> int:
        return len(self.teams)

    def retrieve(self, team, prompt, k=5):
        self.teams.append(team)
        return list(self._neighbors[:k])


class _NoRules:
    async def match(self, team, from_model):
        return None

    async def all(self):
        return []


async def _route(payload, *, team="acme", classify=None, retriever=None):
    return await route(
        payload,
        {},  # headers
        team,
        None,  # redis
        None,  # httpx client — the fake judge ignores it
        _NoRules(),
        classify=classify or SpyClassify(),
        retriever=retriever,
    )


def _meta(model, prompt, cost=0.01, routed_from=None):
    return {"model": model, "cost_usd": cost, "routed_from": routed_from, "prompt": prompt}


def _seed_team(base, team, prompts, meta):
    """Write a real per-team FAISS index + sidecar under ``base/<team>/``."""
    from scripts.build_rag_index import _write_index

    _write_index(embeddings.embed_texts(prompts), meta, base / team_dir_name(team))


# --- Embeddings ------------------------------------------------------------


def test_embed_texts_returns_one_row_per_input():
    vectors = embeddings.embed_texts(["hello world", "a second prompt", "third"])
    assert vectors.ndim == 2
    assert vectors.shape[0] == 3
    assert vectors.shape[1] == 384  # all-MiniLM-L6-v2 width
    assert vectors.dtype == np.float32


def test_embed_one_is_a_single_vector():
    vector = embeddings.embed_one("just one")
    assert vector.shape == (384,)


def test_embed_empty_list_returns_empty_but_shaped():
    vectors = embeddings.embed_texts([])
    assert vectors.shape == (0, 384)


# --- Retriever: seeded per-team index --------------------------------------


def test_seeded_index_returns_the_obvious_nearest_neighbor(tmp_path):
    prompts = [
        "how do I bake sourdough bread at home",
        "what is the capital of France",
        "write a python function to reverse a linked list",
    ]
    meta = [
        _meta(HAIKU, prompts[0]),
        _meta("claude-sonnet-5", prompts[1], cost=0.05),
        _meta(OPUS, prompts[2], cost=0.2),
    ]
    _seed_team(tmp_path, "acme", prompts, meta)

    neighbors = Retriever(tmp_path).retrieve("acme", "reverse a linked list in python", k=1)
    assert len(neighbors) == 1
    # The coding prompt is by far the closest match.
    assert neighbors[0].prompt == prompts[2]
    assert neighbors[0].model == OPUS
    assert 0.0 < neighbors[0].score <= 1.0000001


def test_seeded_index_caps_k_at_index_size(tmp_path):
    _seed_team(tmp_path, "acme", ["only one prompt here"], [_meta(HAIKU, "only one prompt here")])
    neighbors = Retriever(tmp_path).retrieve("acme", "anything", k=5)
    assert len(neighbors) == 1


# --- Retriever: team isolation (required) ----------------------------------


def test_team_isolation_only_returns_the_querying_team(tmp_path):
    a_prompts = ["deploy a kubernetes cluster on aws", "configure an nginx reverse proxy"]
    b_prompts = ["bake chocolate chip cookies", "grill salmon with lemon butter"]
    _seed_team(tmp_path, "team-a", a_prompts, [_meta(OPUS, p) for p in a_prompts])
    _seed_team(tmp_path, "team-b", b_prompts, [_meta(HAIKU, p) for p in b_prompts])

    retriever = Retriever(tmp_path)

    # Query team A in A's own domain: only A's rows ever come back.
    a_hits = retriever.retrieve("team-a", "deploy a service to kubernetes", k=5)
    assert a_hits
    assert all(n.prompt in a_prompts for n in a_hits)
    assert all(n.model == OPUS for n in a_hits)

    # Even querying team A with B's *exact* prompt returns only A's rows, never B's.
    crossover = retriever.retrieve("team-a", "bake chocolate chip cookies", k=5)
    assert all(n.prompt in a_prompts for n in crossover)
    assert all(n.prompt not in b_prompts for n in crossover)

    # And team B is symmetric: its query only sees B's rows.
    b_hits = retriever.retrieve("team-b", "grill some fish", k=5)
    assert b_hits
    assert all(n.prompt in b_prompts for n in b_hits)


def test_new_team_with_no_index_returns_empty(tmp_path):
    _seed_team(tmp_path, "team-a", ["a prompt for A"], [_meta(OPUS, "a prompt for A")])
    retriever = Retriever(tmp_path)
    # A brand-new team has no directory at all: empty, cleanly, no crash.
    assert retriever.retrieve("brand-new-team", "anything at all", k=5) == []


# --- Retriever: empty / corrupt / missing (never raises) -------------------


def test_missing_store_returns_nothing(tmp_path):
    # No team directories exist anywhere under the store.
    assert Retriever(tmp_path).retrieve("acme", "some prompt", k=5) == []


def test_empty_team_index_returns_nothing(tmp_path):
    _seed_team(tmp_path, "acme", [], [])
    assert Retriever(tmp_path).retrieve("acme", "some prompt", k=5) == []


def test_corrupt_team_index_file_never_raises(tmp_path):
    team_dir = tmp_path / team_dir_name("acme")
    team_dir.mkdir(parents=True)
    (team_dir / INDEX_FILENAME).write_bytes(b"this is not a faiss index at all")
    (team_dir / META_FILENAME).write_text("[]")
    assert Retriever(tmp_path).retrieve("acme", "some prompt", k=5) == []  # must not raise


def test_corrupt_team_meta_file_never_raises(tmp_path):
    _seed_team(tmp_path, "acme", ["a real prompt"], [_meta("m", "a real prompt")])
    (tmp_path / team_dir_name("acme") / META_FILENAME).write_text("{ not valid json")
    assert Retriever(tmp_path).retrieve("acme", "a real prompt", k=1) == []  # must not raise


# --- Router integration: hint, empty fallback, disabled --------------------


async def test_rag_hint_reaches_judge_and_sets_header(monkeypatch):
    monkeypatch.setattr(config, "AUTO_ROUTE_ENABLED", True)
    monkeypatch.setattr(config, "RAG_ENABLED", True)
    spy = SpyClassify("easy")
    retriever = FakeRetriever([
        Neighbor(score=0.9, model=HAIKU),
        Neighbor(score=0.8, model=HAIKU),
        Neighbor(score=0.7, model="claude-sonnet-5"),
    ])

    decision = await _route(REQUEST, team="acme", classify=spy, retriever=retriever)

    assert retriever.teams == ["acme"]  # the request's team was passed through
    assert decision.rag == "hit:3"
    assert spy.hints[0] is not None
    assert "Haiku x2" in spy.hints[0]
    assert "Sonnet x1" in spy.hints[0]
    # RAG is only a hint: the verdict still drives the pick.
    assert decision.served_model == EASY_MODEL
    assert decision.reason == "auto"


async def test_empty_index_falls_back_and_router_still_picks(tmp_path, monkeypatch):
    """Required fallback: a new team with no index yields no hint, routing still works."""
    monkeypatch.setattr(config, "AUTO_ROUTE_ENABLED", True)
    monkeypatch.setattr(config, "RAG_ENABLED", True)
    # A real per-team retriever whose store holds another team, not this one.
    _seed_team(tmp_path, "someone-else", ["unrelated"], [_meta(OPUS, "unrelated")])
    retriever = Retriever(tmp_path)
    spy = SpyClassify("easy")

    decision = await _route(REQUEST, team="fresh-team", classify=spy, retriever=retriever)

    assert decision.rag == "empty"
    assert spy.hints[0] is None  # no hint passed to the judge
    # Config order still produces a valid pick: easy verdict routes down.
    assert decision.served_model == EASY_MODEL
    assert decision.reason == "auto"


async def test_rag_disabled_is_identical_to_phase_5(monkeypatch):
    monkeypatch.setattr(config, "AUTO_ROUTE_ENABLED", True)
    monkeypatch.setattr(config, "RAG_ENABLED", False)
    spy = SpyClassify("easy")
    retriever = FakeRetriever([Neighbor(score=0.99, model=HAIKU)])

    decision = await _route(REQUEST, classify=spy, retriever=retriever)

    assert retriever.calls == 0  # never consulted
    assert decision.rag is None  # no x-slice-rag header
    assert spy.hints[0] is None  # judge got no hint — phase-5 behavior
    assert decision.served_model == EASY_MODEL
    assert decision.reason == "auto"


async def test_retrieval_never_runs_on_the_rule_path(monkeypatch):
    """Retrieval stays off the fast path: a rule match short-circuits before it."""
    monkeypatch.setattr(config, "AUTO_ROUTE_ENABLED", True)
    monkeypatch.setattr(config, "RAG_ENABLED", True)
    from app.rules import SwitchRule

    class OneRule:
        async def match(self, team, from_model):
            return SwitchRule(1, "acme", OPUS, "claude-sonnet-5")

        async def all(self):
            return []

    retriever = FakeRetriever([Neighbor(score=0.9, model=HAIKU)])
    decision = await route(
        REQUEST, {}, "acme", None, None, OneRule(),
        classify=SpyClassify("easy"), retriever=retriever,
    )

    assert retriever.calls == 0  # rule path never touches RAG
    assert decision.reason == "rule"
    assert decision.rag is None


async def test_corrupt_retriever_in_router_never_raises(monkeypatch):
    """A retriever that raises inside retrieve() degrades to no hint, not an error."""
    monkeypatch.setattr(config, "AUTO_ROUTE_ENABLED", True)
    monkeypatch.setattr(config, "RAG_ENABLED", True)

    class ExplodingRetriever:
        def retrieve(self, team, prompt, k=5):
            raise RuntimeError("index is on fire")

    spy = SpyClassify("hard")
    decision = await _route(REQUEST, classify=spy, retriever=ExplodingRetriever())

    assert spy.hints[0] is None
    assert decision.rag is None
    assert decision.verdict == "hard"
    assert decision.served_model == OPUS  # hard keeps the client's model


# --- Integration through /v1/messages --------------------------------------


class FakeWriter:
    def __init__(self):
        self.records: list[RequestRecord] = []

    async def record(self, record: RequestRecord) -> None:
        self.records.append(record)


@pytest.fixture
def wired_app():
    """A native endpoint with a fake writer, fake redis, no rules, and a retriever."""
    writer = FakeWriter()
    prev = {k: getattr(app.state, k, None) for k in ("db", "redis", "rules", "retriever")}
    app.state.db = writer
    app.state.redis = fakeredis.aioredis.FakeRedis()
    app.state.rules = RulesCache(None)
    yield writer
    for key, value in prev.items():
        setattr(app.state, key, value)


@respx.mock
async def test_native_request_stamps_rag_header_and_logs_prompt_and_team(
    client, wired_app, monkeypatch
):
    monkeypatch.setattr(config, "AUTO_ROUTE_ENABLED", True)
    monkeypatch.setattr(config, "RAG_ENABLED", True)
    monkeypatch.setattr(config, "RAG_STORE_PROMPTS", True)
    monkeypatch.setattr(judge, "classify", SpyClassify("easy"))
    app.state.retriever = FakeRetriever(
        [Neighbor(score=0.9, model=HAIKU), Neighbor(score=0.8, model="claude-sonnet-5")]
    )
    respx.post(MESSAGES_URL).mock(return_value=httpx.Response(200, json=PROVIDER_RESPONSE))

    r = await client.post("/v1/messages", json=REQUEST, headers={"x-slice-team": "acme"})

    assert r.status_code == 200
    assert r.headers["x-slice-rag"] == "hit:2"
    # The retriever was asked for *this* team's history.
    assert app.state.retriever.teams == ["acme"]
    # The row logs the prompt and the team, for the offline per-team index build.
    assert wired_app.records[0].prompt_text == "write a function"
    assert wired_app.records[0].team == "acme"


@respx.mock
async def test_nothing_stored_when_rag_store_prompts_false(client, wired_app, monkeypatch):
    """RAG_STORE_PROMPTS off: prompt_text is never stored, but team still is."""
    monkeypatch.setattr(config, "AUTO_ROUTE_ENABLED", False)  # no judge; forward as asked
    monkeypatch.setattr(config, "RAG_STORE_PROMPTS", False)
    respx.post(MESSAGES_URL).mock(return_value=httpx.Response(200, json=PROVIDER_RESPONSE))

    r = await client.post("/v1/messages", json=REQUEST, headers={"x-slice-team": "acme"})

    assert r.status_code == 200
    saved = wired_app.records[0]
    assert saved.prompt_text is None  # nothing stored
    assert saved.team == "acme"  # team is threaded regardless


@respx.mock
async def test_team_defaults_when_header_absent(client, wired_app, monkeypatch):
    monkeypatch.setattr(config, "AUTO_ROUTE_ENABLED", False)
    respx.post(MESSAGES_URL).mock(return_value=httpx.Response(200, json=PROVIDER_RESPONSE))

    r = await client.post("/v1/messages", json=REQUEST)  # no x-slice-team

    assert r.status_code == 200
    assert wired_app.records[0].team == "default"


# --- Build script (per-team) -----------------------------------------------


async def test_build_script_builds_one_isolated_index_per_team(tmp_path, monkeypatch):
    import scripts.build_rag_index as build

    monkeypatch.setattr(config, "RAG_INDEX_DIR", str(tmp_path))
    monkeypatch.setattr(config, "DATABASE_URL", "postgresql://fake")

    async def fake_fetch(dsn):
        return [
            {"team": "team-a", "model": OPUS, "cost_usd": 0.2,
             "routed_from": "claude-sonnet-5", "prompt_text": "reverse a linked list in python"},
            {"team": "team-b", "model": HAIKU, "cost_usd": 0.01,
             "routed_from": None, "prompt_text": "bake sourdough bread at home"},
        ]

    monkeypatch.setattr(build, "_fetch_rows", fake_fetch)
    rc = await build.build()

    assert rc == 0
    # Each team got its own directory.
    assert (tmp_path / "team-a" / INDEX_FILENAME).is_file()
    assert (tmp_path / "team-b" / INDEX_FILENAME).is_file()

    retriever = Retriever(tmp_path)
    a = retriever.retrieve("team-a", "reverse a linked list", k=5)
    assert a and a[0].model == OPUS and a[0].routed_from == "claude-sonnet-5"
    # Team A's index never contains team B's row.
    assert all(n.model != HAIKU for n in a)
    b = retriever.retrieve("team-b", "sourdough", k=5)
    assert b and all("bake" in (n.prompt or "") for n in b)


async def test_build_script_folds_null_team_into_default(tmp_path, monkeypatch):
    import scripts.build_rag_index as build

    monkeypatch.setattr(config, "RAG_INDEX_DIR", str(tmp_path))
    monkeypatch.setattr(config, "DATABASE_URL", "postgresql://fake")

    async def fake_fetch(dsn):
        return [{"team": None, "model": OPUS, "cost_usd": 0.2,
                 "routed_from": None, "prompt_text": "a prompt with no team"}]

    monkeypatch.setattr(build, "_fetch_rows", fake_fetch)
    rc = await build.build()

    assert rc == 0
    assert (tmp_path / "default" / INDEX_FILENAME).is_file()
    assert Retriever(tmp_path).retrieve("default", "a prompt with no team", k=1)


async def test_build_script_no_rows_writes_nothing(tmp_path, monkeypatch):
    import scripts.build_rag_index as build

    monkeypatch.setattr(config, "RAG_INDEX_DIR", str(tmp_path))
    monkeypatch.setattr(config, "DATABASE_URL", "postgresql://fake")

    async def empty_fetch(dsn):
        return []

    monkeypatch.setattr(build, "_fetch_rows", empty_fetch)
    rc = await build.build()

    assert rc == 0
    assert list(tmp_path.iterdir()) == []  # no team directories created
    assert Retriever(tmp_path).retrieve("any-team", "x", k=5) == []


async def test_build_script_without_database_url_is_a_clean_noop(monkeypatch):
    import scripts.build_rag_index as build

    monkeypatch.setattr(config, "DATABASE_URL", None)
    rc = await build.build()
    assert rc == 1  # nothing to do, non-zero, but no crash


# --- Prompt extraction -----------------------------------------------------


def test_prompt_extraction_joins_multiple_user_turns():
    payload = {
        "model": OPUS,
        "messages": [
            {"role": "user", "content": "first question"},
            {"role": "assistant", "content": "an answer"},
            {"role": "user", "content": "second question"},
        ],
    }
    assert extract_prompt_text(payload) == "first question\nsecond question"


def test_prompt_extraction_handles_block_content():
    payload = {
        "messages": [
            {"role": "user", "content": [
                {"type": "text", "text": "block one"},
                {"type": "text", "text": " block two"},
            ]},
        ],
    }
    assert extract_prompt_text(payload) == "block one block two"


def test_prompt_extraction_caps_at_4000_chars():
    payload = {"messages": [{"role": "user", "content": "x" * 5000}]}
    result = extract_prompt_text(payload)
    assert len(result) == MAX_PROMPT_CHARS == 4000


def test_prompt_extraction_missing_content_is_null():
    # No user messages at all.
    assert extract_prompt_text({"messages": [{"role": "assistant", "content": "hi"}]}) is None
    # A user turn with no usable text.
    assert extract_prompt_text({"messages": [{"role": "user", "content": []}]}) is None
    # Not a dict / no messages.
    assert extract_prompt_text(None) is None
    assert extract_prompt_text({"model": OPUS}) is None
    assert extract_prompt_text("not a dict") is None


def test_prompt_extraction_only_first_4000_when_joined():
    payload = {"messages": [
        {"role": "user", "content": "a" * 3000},
        {"role": "user", "content": "b" * 3000},
    ]}
    result = extract_prompt_text(payload)
    assert len(result) == 4000
    assert result.startswith("a" * 3000 + "\n")
