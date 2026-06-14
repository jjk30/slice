import { createHash } from "node:crypto";
import type { IncomingHttpHeaders } from "node:http";
import { redisGet, redisSetEx } from "./redis";

/**
 * Phase 4 — Redis response cache.
 *
 * On a HIT we return the stored response WITHOUT calling the provider (or the
 * router/judge) at all — that is the saving. Streaming requests are NOT cached
 * for now (we never buffer a live SSE stream), so the cache only ever applies to
 * non-streaming JSON responses.
 *
 * FAIL-OPEN: all reads/writes go through the redis.ts primitives, which return
 * safe fallbacks when Redis is down — so an outage simply degrades to "always a
 * miss, never stored" and never blocks the proxy.
 */

/** What we store per cache entry — enough to faithfully replay the response. */
export interface CachedResponse {
  status: number;
  contentType: string | null;
  body: string; // response JSON as UTF-8 text
  routedModel: string | null; // model that produced it (for the hit's row)
}

const CACHE_PREFIX = "slice:cache:";
const OVERRIDE_HEADER = "x-slice-cache";

export const cacheEnabled = (): boolean => process.env.CACHE_ENABLED !== "false";

const cacheTtl = (): number => {
  const n = Number(process.env.CACHE_TTL_SECONDS ?? "300");
  return Number.isFinite(n) && n > 0 ? n : 300;
};

/** Per-request opt-out: `x-slice-cache: off` skips both lookup and store. */
export function cacheAllowed(headers: IncomingHttpHeaders): boolean {
  if (!cacheEnabled()) return false;
  const override = headers[OVERRIDE_HEADER];
  return !(typeof override === "string" && override.toLowerCase() === "off");
}

/** Is this request asking for a streaming (SSE) response? Those aren't cached. */
export function isStreamingRequest(body: Buffer): boolean {
  if (body.length === 0) return false;
  try {
    return JSON.parse(body.toString("utf8"))?.stream === true;
  } catch {
    return false;
  }
}

/**
 * Cache key = hash of the request body that determines the answer.
 *
 * Note on ordering: the cache is looked up BEFORE routing, so we key on the
 * INCOMING request (which already contains the client's model + messages + gen
 * params), not the routed model. Routing is a deterministic function of this
 * same input, so the stored response stays correct. We strip the volatile
 * `stream` flag so a streaming/non-streaming pair can't collide (we only cache
 * the non-streaming side anyway).
 */
export function computeCacheKey(body: Buffer): string {
  let canonical = body.toString("utf8");
  try {
    const parsed = JSON.parse(canonical) as Record<string, unknown>;
    delete parsed.stream;
    canonical = JSON.stringify(parsed);
  } catch {
    /* non-JSON body: hash the raw bytes as-is */
  }
  return CACHE_PREFIX + createHash("sha256").update(canonical).digest("hex");
}

export async function getCachedResponse(key: string): Promise<CachedResponse | null> {
  const raw = await redisGet(key);
  if (!raw) return null;
  try {
    return JSON.parse(raw) as CachedResponse;
  } catch {
    return null; // corrupt entry -> treat as a miss
  }
}

export async function setCachedResponse(key: string, value: CachedResponse): Promise<void> {
  await redisSetEx(key, JSON.stringify(value), cacheTtl());
}
