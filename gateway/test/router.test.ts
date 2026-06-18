import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { routeRequest, loadTeamRules } from "../src/router";

/**
 * Phase (team switch-rules) unit tests — rule logic only, NO network. The judge
 * is exercised via a FAKE global fetch; the rules map is seeded via loadTeamRules'
 * injectable fetcher (no database).
 */

function bodyFor(model: string, prompt: string): Buffer {
  return Buffer.from(JSON.stringify({ model, messages: [{ role: "user", content: prompt }] }));
}

/** A fake non-streaming judge response that grades EASY. */
function fakeJudgeResponse(): Response {
  return new Response(
    JSON.stringify({ content: [{ type: "text", text: "EASY" }], usage: { input_tokens: 5, output_tokens: 1 } }),
    { status: 200, headers: { "content-type": "application/json" } },
  );
}

beforeEach(() => {
  // The rule check sits after the ROUTER_ENABLED gate, so routing must be on.
  vi.stubEnv("ROUTER_ENABLED", "true");
});

afterEach(async () => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
  await loadTeamRules(async () => []); // reset rules between tests
});

describe("team switch-rules", () => {
  it("applies a team's rule, returns the mapped model, and SKIPS the judge", async () => {
    await loadTeamRules(async () => [
      { team: "acme", from_model: "claude-opus-4-8", to_model: "gpt-4o" },
    ]);
    const fetchSpy = vi.fn();
    vi.stubGlobal("fetch", fetchSpy);

    const result = await routeRequest(bodyFor("claude-opus-4-8", "anything"), {}, "acme");

    // Body rewritten to the mapped model.
    expect(JSON.parse(result.body.toString("utf8")).model).toBe("gpt-4o");
    expect(result.decision.routedModel).toBe("gpt-4o");
    // Marked as a rule override, distinguishable from an auto-route.
    expect(result.decision.verdict).toBe("rule");
    // The judge was never called — the rule short-circuits before it.
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("only applies the rule to the matching team", async () => {
    await loadTeamRules(async () => [
      { team: "acme", from_model: "claude-opus-4-8", to_model: "gpt-4o" },
    ]);
    const fetchSpy = vi.fn(async () => fakeJudgeResponse());
    vi.stubGlobal("fetch", fetchSpy);

    // Different team -> no rule -> falls through to the judge.
    const result = await routeRequest(bodyFor("claude-opus-4-8", "other-team prompt"), {}, "globex");

    expect(result.decision.verdict).toBe("easy");
    expect(fetchSpy).toHaveBeenCalledTimes(1);
  });

  it("falls through to normal auto-routing when the team has no rule", async () => {
    await loadTeamRules(async () => []); // no rules at all
    const fetchSpy = vi.fn(async () => fakeJudgeResponse());
    vi.stubGlobal("fetch", fetchSpy);

    const result = await routeRequest(bodyFor("claude-opus-4-8", "classify me please"), {}, "acme");

    // Judge ran (normal routing), verdict is a judge verdict, not "rule".
    expect(fetchSpy).toHaveBeenCalledTimes(1);
    expect(result.decision.verdict).toBe("easy");
    // EASY -> cheap tier; with no ranking loaded, falls back to the first tier model.
    expect(result.decision.routedModel).toBe("claude-haiku-4-5-20251001");
  });

  it("does not let a rule override the route-off pin", async () => {
    await loadTeamRules(async () => [
      { team: "acme", from_model: "claude-opus-4-8", to_model: "gpt-4o" },
    ]);
    const fetchSpy = vi.fn();
    vi.stubGlobal("fetch", fetchSpy);

    const result = await routeRequest(
      bodyFor("claude-opus-4-8", "pinned"),
      { "x-slice-route": "off" },
      "acme",
    );

    // Route-off wins: original model untouched, no rule applied, no judge call.
    expect(result.decision.routedModel).toBe("claude-opus-4-8");
    expect(result.decision.verdict).toBeNull();
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("applies a team rule even when ROUTER_ENABLED is not 'true' (rules beat the gate)", async () => {
    vi.stubEnv("ROUTER_ENABLED", "false"); // auto-routing OFF
    await loadTeamRules(async () => [
      { team: "acme", from_model: "claude-opus-4-8", to_model: "gpt-4o" },
    ]);
    const fetchSpy = vi.fn();
    vi.stubGlobal("fetch", fetchSpy);

    const result = await routeRequest(bodyFor("claude-opus-4-8", "routing off but rule set"), {}, "acme");

    // The rule still applies; judge/ranking skipped; no judge call.
    expect(result.decision.routedModel).toBe("gpt-4o");
    expect(result.decision.verdict).toBe("rule");
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("route-off still beats a rule even with auto-routing off", async () => {
    vi.stubEnv("ROUTER_ENABLED", "false");
    await loadTeamRules(async () => [
      { team: "acme", from_model: "claude-opus-4-8", to_model: "gpt-4o" },
    ]);
    const fetchSpy = vi.fn();
    vi.stubGlobal("fetch", fetchSpy);

    const result = await routeRequest(
      bodyFor("claude-opus-4-8", "pinned"),
      { "x-slice-route": "off" },
      "acme",
    );

    // Route-off wins over the rule: original model untouched.
    expect(result.decision.routedModel).toBe("claude-opus-4-8");
    expect(result.decision.verdict).toBeNull();
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("passes through (no judge) when there is no rule and ROUTER_ENABLED is off", async () => {
    vi.stubEnv("ROUTER_ENABLED", "false");
    await loadTeamRules(async () => []); // no rules
    const fetchSpy = vi.fn(async () => fakeJudgeResponse());
    vi.stubGlobal("fetch", fetchSpy);

    const result = await routeRequest(bodyFor("claude-opus-4-8", "no rule, routing off"), {}, "acme");

    // Gate stops auto-routing: original model, no judge call.
    expect(result.decision.verdict).toBeNull();
    expect(result.decision.routedModel).toBe("claude-opus-4-8");
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("fails open to an empty map when the rules load throws", async () => {
    await loadTeamRules(async () => {
      throw new Error("db down");
    });
    const fetchSpy = vi.fn(async () => fakeJudgeResponse());
    vi.stubGlobal("fetch", fetchSpy);

    // No rules in effect -> normal routing, request not broken.
    const result = await routeRequest(bodyFor("claude-opus-4-8", "still works"), {}, "acme");

    expect(result.decision.verdict).toBe("easy");
    expect(fetchSpy).toHaveBeenCalledTimes(1);
  });
});
