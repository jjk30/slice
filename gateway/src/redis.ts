import { createClient, type RedisClientType } from "redis";
import { logger } from "./logger";

/**
 * Single shared Redis client for the whole process (cache + spend counters).
 *
 * SAFETY PROPERTY (mirrors Postgres): if Redis is down, the gateway MUST keep
 * serving. Every operation here is FAIL-OPEN — when the client isn't ready or a
 * command throws, we log a (throttled) warning and return a safe fallback
 * instead of propagating the error. A Redis outage can never take down the proxy.
 */
const client: RedisClientType = createClient({
  url: process.env.REDIS_URL ?? "redis://localhost:6379",
  socket: {
    // Cap reconnect backoff so a long outage doesn't busy-loop, and never give
    // up retrying (so the cache/caps come back automatically once Redis returns).
    reconnectStrategy: (retries) => Math.min(retries * 200, 3000),
  },
});

// node-redis emits 'error' on every failed (re)connect; without a handler the
// process would crash. Throttle the warning so an outage doesn't flood the log.
let lastWarn = 0;
function warnThrottled(message: string, err?: unknown): void {
  const now = Date.now();
  if (now - lastWarn < 5000) return;
  lastWarn = now;
  logger.warn({ err: err instanceof Error ? err.message : err }, message);
}

client.on("error", (err) => warnThrottled("redis unavailable (fail-open)", err));

/** Connect once at startup. Best-effort: never blocks or crashes the gateway. */
export async function connectRedis(): Promise<void> {
  try {
    await client.connect();
    logger.info("connected to redis");
  } catch (err) {
    warnThrottled("redis initial connect failed (will retry in background)", err);
  }
}

/** Close on shutdown. Best-effort; never throws. */
export async function closeRedis(): Promise<void> {
  await client.quit().catch(() => undefined);
}

/**
 * Run a Redis op, returning `fallback` if the client isn't ready or the command
 * throws. This is the one place the fail-open guarantee is enforced.
 */
async function safe<T>(op: (c: RedisClientType) => Promise<T>, fallback: T): Promise<T> {
  if (!client.isReady) {
    warnThrottled("redis not ready; failing open");
    return fallback;
  }
  try {
    return await op(client);
  } catch (err) {
    warnThrottled("redis command failed; failing open", err);
    return fallback;
  }
}

// --- Typed, fail-open primitives used by the cache and budget modules --------

export function redisGet(key: string): Promise<string | null> {
  return safe((c) => c.get(key), null);
}

export function redisSetEx(key: string, value: string, ttlSeconds: number): Promise<boolean> {
  return safe(async (c) => {
    await c.set(key, value, { EX: ttlSeconds });
    return true;
  }, false);
}

/** Atomic float increment; returns the new total, or null if Redis is down. */
export function redisIncrByFloat(key: string, amount: number): Promise<number | null> {
  return safe(async (c) => {
    // node-redis returns the new value as a string.
    const next = await c.incrByFloat(key, amount);
    const n = Number(next);
    return Number.isFinite(n) ? n : null;
  }, null);
}

export function redisGetFloat(key: string): Promise<number | null> {
  return safe(async (c) => {
    const raw = await c.get(key);
    if (raw === null) return 0;
    const n = Number(raw);
    return Number.isFinite(n) ? n : 0;
  }, null);
}

/** Push onto the head of a list and trim to `max` items (recent-N window). */
export function redisLPushTrim(key: string, value: string, max: number): Promise<boolean> {
  return safe(async (c) => {
    await c.lPush(key, value);
    await c.lTrim(key, 0, max - 1);
    return true;
  }, false);
}

export function redisLRange(key: string, start: number, stop: number): Promise<string[]> {
  return safe((c) => c.lRange(key, start, stop), []);
}

/**
 * Enumerate keys matching a glob pattern via a non-blocking SCAN cursor (never
 * KEYS, which would stall Redis). Read-only; used by the stats API to discover
 * which teams have a running spend counter. Fail-open -> [] if Redis is down.
 */
export function redisScanKeys(pattern: string): Promise<string[]> {
  return safe(async (c) => {
    const found: string[] = [];
    let cursor = 0;
    do {
      const reply = await c.scan(cursor, { MATCH: pattern, COUNT: 100 });
      cursor = Number(reply.cursor);
      found.push(...reply.keys);
    } while (cursor !== 0);
    return found;
  }, []);
}
