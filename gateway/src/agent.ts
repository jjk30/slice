import { BudgetEngine, type BudgetStore } from "./budget";

/**
 * Phase 7 — the agent loop (circuit breaker).
 *
 * An OPT-IN agentic mode. By default slice is a transparent proxy; when a request
 * carries `x-slice-agent: on` the proxy runs this bounded loop instead of a single
 * passthrough:
 *
 *   1. Walk a model LADDER cheapest -> strongest (built from the tier config and
 *      the Phase 6 cost ranking).
 *   2. Call the model, then CHECK the answer (see {@link Checker}).
 *   3. Pass -> return that answer and stop.
 *   4. Fail -> step up one rung and try again.
 *   5. Stop when the check passes, the ladder runs out, OR the next attempt would
 *      cross the per-task budget ceiling. Always return the best answer so far.
 *
 * This file is the PURE core: it owns no I/O. The model call, the checker (which
 * wraps the verifier), the budget engine, and the per-attempt logger are all
 * injected via {@link AgentDeps}, so the unit tests drive it with fakes and never
 * touch the network. The real wiring lives in agentHandler.ts.
 *
 * FAIL-OPEN, ALWAYS: if anything in the loop throws — or no attempt ever completes
 * (empty ladder / first attempt already over the ceiling) — we fall back to a
 * SINGLE normal passthrough call so the request never breaks.
 */

// --- What a model invocation produces ---------------------------------------

/** One model call's full result, including its priced USD cost. */
export interface ModelAttemptResult {
  status: number; // provider HTTP status
  answerText: string; // text answer extracted from the response (empty if none)
  rawBody: string; // raw response body, replayed verbatim to the client
  costUsd: number; // priced cost of THIS attempt (main call tokens)
  inputTokens: number | null;
  outputTokens: number | null;
}

// --- The Checker abstraction (v1 = hard checks + a cheap soft verifier) ------

/** The slice of a {@link ModelAttemptResult} a {@link Checker} grades. */
export interface Candidate {
  status: number;
  answerText: string;
  rawBody: string;
}

/** Context a checker needs that is constant across the loop's attempts. */
export interface CheckContext {
  /** The user's original request text, handed to the verifier. */
  requestText: string;
  /** True only when we can cheaply tell the client expected a JSON answer. */
  expectsJson: boolean;
}

/** Strict, typed check verdict — never a loose string. */
export type CheckVerdict = "pass" | "escalate";

export interface CheckOutcome {
  verdict: CheckVerdict;
  reason: string; // human-readable; logged with the attempt
}

/** Swappable check strategy. v1 is {@link V1Checker}; callers can supply others. */
export interface Checker {
  check(candidate: Candidate, ctx: CheckContext): Promise<CheckOutcome>;
}

/** The cheap soft grader: "does this candidate fully answer the request?" */
export type Verifier = (requestText: string, answerText: string) => Promise<boolean>;

/**
 * RESERVED EXTENSION POINT — a caller-supplied test command (compile / unit
 * tests / linter) that grades a candidate by RUNNING it. Intentionally NOT
 * implemented in v1; the type exists so a future checker can run it as an extra
 * hard gate without any interface change. See {@link V1Checker.check}.
 */
export type TestCommandRunner = (candidate: Candidate, ctx: CheckContext) => Promise<CheckOutcome>;

export interface V1CheckerOptions {
  /** The cheap verifier model call (injected; real one calls Haiku). */
  verify: Verifier;
  /**
   * RESERVED, UNUSED in v1. Wire a real {@link TestCommandRunner} here later to
   * add a compile/test/lint gate — no interface change needed.
   */
  testCommand?: TestCommandRunner;
}

/**
 * v1 checker. Runs the cheapest, most decisive checks first:
 *   1. HARD (deterministic, free): provider returned 2xx.
 *   2. HARD (deterministic, free): the answer is non-empty.
 *   3. HARD (only when we can cheaply tell JSON was expected): it parses.
 *   4. SOFT (a cheap model call): the verifier grades yes/no.
 * Any hard failure or a verifier "no" => escalate. All pass => stop.
 */
export class V1Checker implements Checker {
  constructor(private readonly opts: V1CheckerOptions) {}

  async check(candidate: Candidate, ctx: CheckContext): Promise<CheckOutcome> {
    // 1. Hard: the provider call must have succeeded.
    if (candidate.status < 200 || candidate.status >= 300) {
      return { verdict: "escalate", reason: `provider status ${candidate.status}` };
    }

    // 2. Hard: the answer must be non-empty.
    if (candidate.answerText.trim().length === 0) {
      return { verdict: "escalate", reason: "empty answer" };
    }

    // 3. Hard (conditional): if the client expected JSON, it must parse. We skip
    // this check entirely when we can't cheaply tell JSON was expected.
    if (ctx.expectsJson) {
      try {
        JSON.parse(candidate.answerText);
      } catch {
        return { verdict: "escalate", reason: "answer is not valid JSON" };
      }
    }

    // 4. Soft: the cheap verifier grades the answer. A verifier OUTAGE is
    // non-fatal — we treat it as a pass so a flaky grader never forces a needless,
    // costly escalation; the hard checks above already guard basic quality.
    let answersFully: boolean;
    try {
      answersFully = await this.opts.verify(ctx.requestText, candidate.answerText);
    } catch (err) {
      return {
        verdict: "pass",
        reason: `verifier unavailable (${(err as Error).message}); hard checks passed`,
      };
    }
    if (!answersFully) {
      return { verdict: "escalate", reason: "verifier: does not fully answer the request" };
    }

    // NOTE: opts.testCommand is the reserved extension point. When a future
    // version supplies one, it would run HERE as an additional hard gate
    // (compile / unit tests / linter). Intentionally NOT invoked in v1.

    return { verdict: "pass", reason: "all checks passed" };
  }
}

// --- The model ladder --------------------------------------------------------

/**
 * Build the cheapest -> strongest model ladder.
 *
 * Base order is the tier config: the cheap tier first, then the strong tier,
 * de-duplicated keeping the first occurrence. We then STABLY sort by the Phase 6
 * ranking cost (ascending); a model the ranking knows nothing about keeps its
 * config position (its cost sorts as +Infinity, and the stable sort preserves the
 * order of equal keys). So:
 *   - a missing or empty ranking => exactly the config order (every cost Infinity);
 *   - a single-model tier        => a one-rung ladder;
 *   - a populated ranking        => true cheapest-first across both tiers.
 *
 * Pure: the cost lookup is injected, so this never touches disk.
 */
export function buildLadder(
  cheapTier: ReadonlyArray<string>,
  strongTier: ReadonlyArray<string>,
  costOf: (model: string) => number | null,
): string[] {
  const seen = new Set<string>();
  const configOrder: string[] = [];
  for (const model of [...cheapTier, ...strongTier]) {
    if (model && !seen.has(model)) {
      seen.add(model);
      configOrder.push(model);
    }
  }

  return configOrder
    .map((model, idx) => ({ model, idx, cost: costOf(model) ?? Number.POSITIVE_INFINITY }))
    .sort((a, b) => {
      // Explicit compare (not a.cost - b.cost): Infinity - Infinity is NaN, which
      // would corrupt the sort. Equal costs fall through to config order (idx).
      if (a.cost !== b.cost) return a.cost < b.cost ? -1 : 1;
      return a.idx - b.idx;
    })
    .map((entry) => entry.model);
}

// --- The loop ----------------------------------------------------------------

/** One persisted attempt — mapped by the wiring onto a RequestLog row. */
export interface AttemptLog {
  attempt: number; // 1-based rung index; 0 marks a fail-open passthrough
  model: string;
  status: number;
  costUsd: number;
  inputTokens: number | null;
  outputTokens: number | null;
  check: CheckVerdict;
  escalated: boolean; // true when this attempt failed AND a stronger rung exists
  reason: string;
}

/** Injected I/O. Fakes for tests; real provider/verifier/DB wiring in handler. */
export interface AgentDeps {
  /** The (possibly model-rewritten) request body to send each attempt. */
  requestBody: Buffer;
  /** The client's originally requested model (for logging the passthrough row). */
  requestedModel: string | null;
  /** Constant context handed to the checker each attempt. */
  checkContext: CheckContext;
  /** Call ONE model and return its priced result. May throw -> loop fails open. */
  callModel: (model: string, requestBody: Buffer) => Promise<ModelAttemptResult>;
  /** Pre-flight cost ESTIMATE for `model` — the circuit breaker's input. */
  estimateCost: (model: string) => number;
  /** Grade a candidate answer. */
  checker: Checker;
  /** The Phase 4 budget engine, totalling spend across the loop's attempts. */
  budget: BudgetEngine;
  /** Persist one attempt (one Postgres row via the existing logging path). */
  logAttempt: (entry: AttemptLog) => void;
  /** A single normal passthrough call — the fail-open escape hatch. */
  passthrough: () => Promise<ModelAttemptResult>;
}

export type StoppedReason = "passed" | "ladder_exhausted" | "budget_ceiling" | "failed_open";

export interface AgentLoopResult {
  answer: ModelAttemptResult | null; // best answer so far (null only if passthrough also produced none)
  model: string | null; // model that produced `answer`
  attempts: number; // completed ladder attempts (excludes a fail-open passthrough)
  stoppedReason: StoppedReason;
  totalCostUsd: number;
}

/** The single account name the per-task budget engine totals spend under. */
export const TASK_ACCOUNT = "task";

/**
 * Run the bounded ladder loop. Never rejects: any throw, or a loop that completes
 * zero attempts, falls back to a single passthrough.
 */
export async function runAgentLoop(
  ladder: ReadonlyArray<string>,
  deps: AgentDeps,
): Promise<AgentLoopResult> {
  try {
    const result = await loopCore(ladder, deps);
    // Zero completed attempts (empty ladder, or the first rung already over the
    // ceiling): fail open so the request still gets an answer.
    if (result.answer === null) return await failOpen(deps);
    return result;
  } catch {
    // Anything in the loop threw: fall back to a single normal passthrough.
    return await failOpen(deps);
  }
}

async function loopCore(
  ladder: ReadonlyArray<string>,
  deps: AgentDeps,
): Promise<AgentLoopResult> {
  let best: ModelAttemptResult | null = null;
  let bestModel: string | null = null;
  let attempts = 0;
  let totalCostUsd = 0;

  for (let i = 0; i < ladder.length; i++) {
    const model = ladder[i];

    // CIRCUIT BREAKER: would this attempt cross the per-task ceiling? We only know
    // an attempt's true cost AFTER the call, so we gate on a pre-flight estimate
    // and stop BEFORE crossing — returning the best answer so far.
    const standing = await deps.budget.status(TASK_ACCOUNT);
    const estimate = deps.estimateCost(model);
    if (standing.limitUsd > 0 && standing.spendUsd + estimate > standing.limitUsd) {
      return { answer: best, model: bestModel, attempts, stoppedReason: "budget_ceiling", totalCostUsd };
    }

    const result = await deps.callModel(model, deps.requestBody);
    attempts += 1;
    totalCostUsd += result.costUsd;
    best = result;
    bestModel = model;

    // Total this attempt's spend into the (reused) Phase 4 budget engine.
    await deps.budget.record({
      account: TASK_ACCOUNT,
      amountUsd: result.costUsd,
      timestamp: Date.now(),
      source: "agent_attempt",
    });

    const outcome = await deps.checker.check(
      { status: result.status, answerText: result.answerText, rawBody: result.rawBody },
      deps.checkContext,
    );
    const escalated = outcome.verdict === "escalate" && i < ladder.length - 1;

    deps.logAttempt({
      attempt: i + 1,
      model,
      status: result.status,
      costUsd: result.costUsd,
      inputTokens: result.inputTokens,
      outputTokens: result.outputTokens,
      check: outcome.verdict,
      escalated,
      reason: outcome.reason,
    });

    if (outcome.verdict === "pass") {
      return { answer: best, model: bestModel, attempts, stoppedReason: "passed", totalCostUsd };
    }
    // else: escalate to the next, stronger rung.
  }

  // Ran out of ladder without a pass — return the strongest answer we got.
  return { answer: best, model: bestModel, attempts, stoppedReason: "ladder_exhausted", totalCostUsd };
}

/** Fail-open escape hatch: one normal passthrough call, logged as attempt 0. */
async function failOpen(deps: AgentDeps): Promise<AgentLoopResult> {
  const result = await deps.passthrough();
  deps.logAttempt({
    attempt: 0,
    model: deps.requestedModel ?? "(passthrough)",
    status: result.status,
    costUsd: result.costUsd,
    inputTokens: result.inputTokens,
    outputTokens: result.outputTokens,
    check: "pass",
    escalated: false,
    reason: "fail-open passthrough",
  });
  return {
    answer: result,
    model: deps.requestedModel,
    attempts: 0,
    stoppedReason: "failed_open",
    totalCostUsd: result.costUsd,
  };
}

// --- Per-task budget: reuse the Phase 4 engine with an in-memory store --------

/**
 * In-memory {@link BudgetStore}. The Phase 4 engine is source-agnostic, so we
 * reuse it unchanged for the per-task ceiling by backing it with a process-local
 * counter scoped to a SINGLE loop (not Redis, which totals across all requests).
 */
export class InMemoryBudgetStore implements BudgetStore {
  private spend = 0;
  async getSpend(): Promise<number> {
    return this.spend;
  }
  async addSpend(_account: string, amountUsd: number): Promise<number> {
    this.spend += amountUsd;
    return this.spend;
  }
}

/** Build a per-task budget engine with the given USD ceiling. Reuses Phase 4. */
export function makeTaskBudget(limitUsd: number): BudgetEngine {
  return new BudgetEngine(new InMemoryBudgetStore(), {
    limitFor: () => limitUsd,
    warnRatio: 0.8,
  });
}
