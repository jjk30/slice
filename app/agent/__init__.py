"""The phase-7 agent loop: try cheap, check, escalate, stop at a budget ceiling.

A single agent, wired as a small LangGraph, that extends the router's auto path.
When auto-routing has sent a request down to a cheaper model, the loop tries that
model, checks the answer, and — if the check fails — walks up a cross-provider
ladder one rung at a time, stopping the moment the next attempt's upper-bound cost
would cross the per-request ceiling. Every stage fails open: a dead provider is
skipped, a checker that errors accepts the answer, and any unexpected failure falls
back to serving the cheap model's answer, exactly as phase 6 would.
"""

from app.agent.loop import AGENT_HEADER, LoopResult, agent_applies, run_agent_loop

__all__ = ["AGENT_HEADER", "LoopResult", "agent_applies", "run_agent_loop"]
