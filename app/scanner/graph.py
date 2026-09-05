"""The scanner's supervisor graph (phase 18a): a real LangGraph StateGraph, not a loop.

    supervisor ─┬─▶ s3_public   ─┐
                ├─▶ sg_open      ─┤
                ├─▶ unencrypted  ─┼─▶ collector ─▶ END
                └─▶ iam_risk     ─┘

The supervisor node fans out to the four check nodes, which run **in parallel** (one
LangGraph superstep: each node offloads its blocking boto3 work to a thread, so the four
run concurrently on the event loop). A collector node merges their output. This mirrors
the supervisor/worker agent pattern from the NVIDIA cert rather than a for-loop over the
checks.

The merge is a reducer on the state: ``findings``, ``ran`` and ``errors`` each carry an
``operator.add`` annotation, so LangGraph combines the concurrent writes from the four
branches instead of rejecting them. Fail-open is enforced at the node: each node wraps
its check in try/except, so one check raising records an error and drops its findings but
never touches the other three or the run.
"""

from __future__ import annotations

import asyncio
import json
import logging
import operator
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, StateGraph

from app.scanner.checks import CHECKS
from app.scanner.models import Finding

logger = logging.getLogger("slice.gateway")


class ScanState(TypedDict, total=False):
    session: Any
    findings: Annotated[list, operator.add]
    ran: Annotated[list, operator.add]
    errors: Annotated[list, operator.add]


def _supervisor_node(state: ScanState) -> dict:
    # The fan-out point. It holds no logic of its own: the graph's edges do the work,
    # but keeping it a real node makes the supervisor/worker shape explicit.
    return {}


def _collector_node(state: ScanState) -> dict:
    # The join point. The reducer has already merged every branch's findings by the time
    # this runs; it exists so the four workers converge on one node before END.
    return {}


def _make_check_node(name: str, fn):
    async def node(state: ScanState) -> dict:
        try:
            # boto3 is blocking; run it in a thread so the four checks truly run at once.
            found = await asyncio.to_thread(fn, state["session"])
            return {"findings": list(found or []), "ran": [name]}
        except Exception as exc:  # noqa: BLE001  # one check failing never kills the others.
            logger.warning(
                json.dumps({"event": "scanner_check_failed", "check": name, "error": str(exc)})
            )
            return {"ran": [name], "errors": [{"check": name, "error": str(exc)}]}

    return node


def _build_graph():
    graph = StateGraph(ScanState)
    graph.add_node("supervisor", _supervisor_node)
    graph.add_node("collector", _collector_node)
    graph.set_entry_point("supervisor")
    for name, fn in CHECKS:
        graph.add_node(name, _make_check_node(name, fn))
        graph.add_edge("supervisor", name)  # fan out
        graph.add_edge(name, "collector")  # fan in
    graph.add_edge("collector", END)
    return graph.compile()


# Compiled once; the per-run session and results ride in ScanState.
_GRAPH = _build_graph()


async def run_scan_graph(session) -> list[Finding]:
    """Run every check over ``session`` through the supervisor graph. Never raises.

    Returns the merged findings, sorted most-severe first (then by check, then resource)
    so the caller and the alert see a stable, readable order. A graph-level failure (which
    the per-node guards make unlikely) degrades to an empty list rather than propagating.
    """
    initial: ScanState = {"session": session, "findings": [], "ran": [], "errors": []}
    try:
        final = await _GRAPH.ainvoke(initial)
    except Exception as exc:  # noqa: BLE001  # the whole scan fails open to nothing found.
        logger.warning(json.dumps({"event": "scanner_graph_error", "error": str(exc)}))
        return []

    findings: list[Finding] = list(final.get("findings", []))
    rank = {"high": 0, "med": 1, "low": 2}
    findings.sort(key=lambda f: (rank.get(f.severity, 3), f.check, f.resource_id))
    return findings
