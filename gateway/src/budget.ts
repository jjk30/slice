import { logger } from "./logger";
import { redisGetFloat, redisIncrByFloat, redisLPushTrim } from "./redis";
import { persistBudgetEvent } from "./db";

/**
 * Phase 4 — budget caps, as a SOURCE-AGNOSTIC engine.
 *
 * The engine knows nothing about AI, tokens, or models. It only understands a
 * stream of typed `SpendEvent`s ({ account, amountUsd, ... }) accumulating
 * against a per-account limit. Today the only producer is AI request cost
 * (proxy.ts prices tokens via pricing.ts and feeds the result in); a future
 * producer (e.g. an AWS cost poller) can emit `SpendEvent`s into the SAME engine
 * with ZERO changes here. That is the property a reviewer should check below:
 * nothing in this file references models, tokens, or HTTP.
 */

/** A unit of spend from ANY source. `source` is free-form ("ai_request", ...). */
export interface SpendEvent {
  account: string;
  amountUsd: number;
  timestamp: number;
  source: string;
}

/** Emitted when an account crosses the warn threshold or is blocked at the cap. */
export interface CapEvent {
  account: string;
  kind: "warn" | "block";
  spendUsd: number;
  limitUsd: number;
  timestamp: number;
  source: string;
}

/** Snapshot of an account's standing, used for the pre-flight block decision. */
export interface BudgetStatus {
  account: string;
  spendUsd: number;
  limitUsd: number;
  remainingUsd: number;
  overLimit: boolean;
}

/**
 * Pluggable persistence for running spend. Decoupled from Redis so the engine is
 * testable and storage-agnostic. Both methods FAIL-OPEN: on a store outage
 * `getSpend` reports 0 (so nothing is blocked) and `addSpend` returns null.
 */
export interface BudgetStore {
  getSpend(account: string): Promise<number>;
  addSpend(account: string, amountUsd: number): Promise<number | null>;
}

export interface BudgetEngineOptions {
  /** Resolve the USD cap for an account (per-team limits live here). */
  limitFor: (account: string) => number;
  /** Fraction of the cap that triggers a warn event (e.g. 0.8). */
  warnRatio: number;
  /** Sink for cap events (log/persist/alert). Kept outside the engine. */
  onEvent?: (event: CapEvent) => void;
}

export class BudgetEngine {
  constructor(
    private readonly store: BudgetStore,
    private readonly opts: BudgetEngineOptions,
  ) {}

  /** Current standing for an account — read before forwarding to decide blocks. */
  async status(account: string): Promise<BudgetStatus> {
    const limitUsd = this.opts.limitFor(account);
    const spendUsd = await this.store.getSpend(account); // fail-open -> 0
    return {
      account,
      spendUsd,
      limitUsd,
      remainingUsd: Math.max(0, limitUsd - spendUsd),
      // Only enforce when a positive limit is configured.
      overLimit: limitUsd > 0 && spendUsd >= limitUsd,
    };
  }

  /**
   * Record spend and emit a warn event when the account first crosses the warn
   * threshold. (The hard BLOCK is enforced pre-flight by the caller using
   * `status()`, which is where the block CapEvent is emitted.)
   */
  async record(event: SpendEvent): Promise<void> {
    const newTotal = await this.store.addSpend(event.account, event.amountUsd);
    if (newTotal === null) return; // store down: fail-open, nothing to emit

    const limitUsd = this.opts.limitFor(event.account);
    if (limitUsd <= 0) return;

    const warnAt = limitUsd * this.opts.warnRatio;
    const previous = newTotal - event.amountUsd;
    // Fire exactly once, on the upward crossing of the warn threshold.
    if (previous < warnAt && newTotal >= warnAt) {
      this.opts.onEvent?.({
        account: event.account,
        kind: "warn",
        spendUsd: newTotal,
        limitUsd,
        timestamp: event.timestamp,
        source: event.source,
      });
    }
  }
}

// --- Redis-backed store ------------------------------------------------------
const SPEND_KEY = (account: string) => `slice:spend:${account}`;

/** Live spend counter in Redis. Fail-open via the redis.ts primitives. */
class RedisBudgetStore implements BudgetStore {
  async getSpend(account: string): Promise<number> {
    const value = await redisGetFloat(SPEND_KEY(account));
    return value ?? 0; // null (Redis down) -> 0 so nothing is blocked
  }
  async addSpend(account: string, amountUsd: number): Promise<number | null> {
    return redisIncrByFloat(SPEND_KEY(account), amountUsd);
  }
}

// --- Config (env-driven) -----------------------------------------------------
export const budgetEnabled = (): boolean => process.env.BUDGET_ENABLED !== "false";

/** Per-team limit: BUDGET_LIMIT_<TEAM> overrides the global BUDGET_LIMIT_USD. */
function limitFor(account: string): number {
  const perTeam = process.env[`BUDGET_LIMIT_${account.toUpperCase()}`];
  const raw = perTeam ?? process.env.BUDGET_LIMIT_USD ?? "10";
  const n = Number(raw);
  return Number.isFinite(n) ? n : 10;
}

const warnRatio = (): number => {
  const n = Number(process.env.BUDGET_WARN_RATIO ?? "0.8");
  return Number.isFinite(n) && n > 0 && n < 1 ? n : 0.8;
};

const EVENTS_KEY = (account: string) => `slice:budget:events:${account}`;
const RECENT_EVENTS_MAX = 50;

/**
 * The cap-event sink: log it, persist it to Postgres, and keep a recent-N copy
 * in Redis. Used by the engine (warn events) and by proxy.ts (block events).
 * All side effects are best-effort so they never break the request path.
 */
export function recordCapEvent(event: CapEvent): void {
  logger.warn(
    {
      account: event.account,
      kind: event.kind,
      spend_usd: Number(event.spendUsd.toFixed(6)),
      limit_usd: event.limitUsd,
      source: event.source,
    },
    `budget ${event.kind} for team ${event.account}`,
  );
  persistBudgetEvent(event); // fire-and-forget (see db.ts)
  void redisLPushTrim(EVENTS_KEY(event.account), JSON.stringify(event), RECENT_EVENTS_MAX);
}

/** The process-wide engine, wired to Redis + the cap-event sink. */
export const budget = new BudgetEngine(new RedisBudgetStore(), {
  limitFor,
  warnRatio: warnRatio(),
  onEvent: recordCapEvent,
});
