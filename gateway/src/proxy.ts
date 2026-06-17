import type { Request, Response } from "express";
import { Readable } from "node:stream";
import { logRequest, type RequestLog } from "./logger";
import { persistRequest } from "./db";
import { routeRequest } from "./router";
import {
  cacheAllowed,
  computeCacheKey,
  getCachedResponse,
  isStreamingRequest,
  setCachedResponse,
} from "./cache";
import { budget, budgetEnabled, recordCapEvent } from "./budget";
import { estimateCostUsd } from "./pricing";
import { logger } from "./logger";
import { agentEnabled, handleAgentRequest } from "./agentHandler";
import { getAdapter, providerForModel } from "./providers/registry";

/**
 * Headers we must NOT forward verbatim to the upstream. `host` would point at
 * the gateway, and the length/encoding headers are recomputed by `fetch` for
 * the new request. Everything else (including the client's `x-api-key` /
 * `authorization` / `anthropic-version`) is passed through untouched.
 */
const HOP_BY_HOP = new Set(["host", "content-length", "connection", "transfer-encoding"]);

/** Pull the `model` field out of the (already-parsed) request body buffer. */
function readModel(body: Buffer): string | null {
  if (body.length === 0) return null;
  try {
    const parsed = JSON.parse(body.toString("utf8"));
    return typeof parsed?.model === "string" ? parsed.model : null;
  } catch {
    return null; // non-JSON body (e.g. GET) — nothing to read
  }
}

/** Token usage we try to recover from the upstream response. */
interface Usage {
  input_tokens: number | null;
  output_tokens: number | null;
}

/**
 * Extracts token usage from a *streaming* (SSE) Anthropic response without
 * buffering the whole thing. We scan line-by-line for `data:` payloads and pick
 * usage out of the `message_start` (input + initial output) and `message_delta`
 * (cumulative output) events as they fly past.
 */
async function extractUsageFromSSE(stream: ReadableStream<Uint8Array>, usage: Usage): Promise<void> {
  const reader = stream.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  const consumeLine = (line: string) => {
    const trimmed = line.trim();
    if (!trimmed.startsWith("data:")) return;
    const payload = trimmed.slice(5).trim();
    if (!payload || payload === "[DONE]") return;
    try {
      const evt = JSON.parse(payload);
      // message_start carries the message object with the input token count.
      const u = evt?.usage ?? evt?.message?.usage;
      if (u) {
        if (typeof u.input_tokens === "number") usage.input_tokens = u.input_tokens;
        if (typeof u.output_tokens === "number") usage.output_tokens = u.output_tokens;
      }
    } catch {
      /* partial / non-JSON line — ignore */
    }
  };

  // Read to completion. This branch of the tee runs independently of the client
  // pipe, so it never blocks delivery to the client.
  // eslint-disable-next-line no-constant-condition
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let nl: number;
    while ((nl = buffer.indexOf("\n")) !== -1) {
      consumeLine(buffer.slice(0, nl));
      buffer = buffer.slice(nl + 1);
    }
  }
  if (buffer) consumeLine(buffer);
}

/**
 * Extracts token usage from a *non-streaming* JSON response body. We already
 * buffer this body (it's small) so we can cache it, so parse usage from the same
 * bytes rather than re-reading a stream.
 */
function extractUsageFromBuffer(bytes: Buffer, usage: Usage): void {
  try {
    const u = JSON.parse(bytes.toString("utf8"))?.usage;
    if (u) {
      if (typeof u.input_tokens === "number") usage.input_tokens = u.input_tokens;
      if (typeof u.output_tokens === "number") usage.output_tokens = u.output_tokens;
    }
  } catch {
    /* error body or non-JSON — no usage to report */
  }
}

/** Which "team" a request belongs to (Phase 4 budget caps). Real auth comes later. */
function teamFrom(req: Request): string {
  const header = req.headers["x-slice-team"];
  return typeof header === "string" && header.trim() ? header.trim() : "default";
}

/** Default judge model, used only to price the judge call's token cost. */
const JUDGE_MODEL = () => process.env.JUDGE_MODEL ?? "claude-haiku-4-5-20251001";

/**
 * The proxy handler — Phase 4 order of operations (documented, in this order):
 *
 *   1. BUDGET CHECK   — block (429) if the team is already over its cap.
 *   2. CACHE LOOKUP   — on a hit, return the stored response (no provider call).
 *   3. ROUTE          — Phase 3 classifier rewrites the model (miss path only).
 *   4. FORWARD        — call the Anthropic upstream.
 *   5. STORE IN CACHE — cache the (successful, non-streaming) response.
 *   6. RECORD SPEND   — price the tokens and add to the team's running spend.
 *   7. PERSIST ROW    — one log line + one DB row (in `finally`, every path).
 *
 * Both Redis-backed features FAIL OPEN: a budget store outage allows the request
 * (step 1) and a cache outage degrades to a miss (steps 2/5) — see cache.ts /
 * redis.ts. A logging-store outage never blocks traffic either (db.ts).
 */
export async function proxyHandler(req: Request, res: Response): Promise<void> {
  // ---- 0. AGENT MODE (Phase 7): opt-in only. With `x-slice-agent: on` we run
  // the bounded ladder loop instead of a single passthrough. Absent the header
  // this is a no-op, so default proxy behavior is byte-for-byte unchanged.
  if (agentEnabled(req.headers)) {
    await handleAgentRequest(req, res);
    return;
  }

  const upstreamBase = process.env.ANTHROPIC_UPSTREAM ?? "https://api.anthropic.com";
  const start = Date.now();

  // `express.raw` populated req.body as a Buffer for every content type.
  const originalBody: Buffer = Buffer.isBuffer(req.body) ? req.body : Buffer.alloc(0);
  const team = teamFrom(req);
  const streaming = isStreamingRequest(originalBody);
  const requestedModel = readModel(originalBody);

  // Progressive state, finalized once in the `finally` block below.
  const usage: Usage = { input_tokens: null, output_tokens: null };
  let status = 502;
  let routedModel: string | null = requestedModel;
  let verdict: RequestLog["verdict"] = null;
  let judgeInputTokens: number | null = null;
  let judgeOutputTokens: number | null = null;
  let cacheHit = false;
  let costUsd = 0; // priced after the forward; stays 0 for cache hits / blocks

  try {
    // ---- 1. BUDGET CHECK: block if the team is already over its cap ---------
    // FAIL-OPEN: budget.status() reads Redis through fail-open primitives, so if
    // Redis is down spend reads as 0, overLimit is false, and we never block.
    if (budgetEnabled()) {
      const standing = await budget.status(team);
      if (standing.overLimit) {
        // Kill switch: record the cap event and refuse WITHOUT forwarding.
        recordCapEvent({
          account: team,
          kind: "block",
          spendUsd: standing.spendUsd,
          limitUsd: standing.limitUsd,
          timestamp: Date.now(),
          source: "ai_request",
        });
        status = 429;
        res.setHeader("x-slice-cache", "BYPASS");
        res.status(429).json({
          error: {
            type: "budget_exceeded",
            message: `budget exceeded for team ${team}`,
            team,
            spend_usd: Number(standing.spendUsd.toFixed(6)),
            limit_usd: standing.limitUsd,
          },
        });
        return; // finally still logs + persists this blocked attempt
      }
    }

    // ---- 2. CACHE LOOKUP: return on hit, no provider call -------------------
    // Streaming responses are NOT cached (we never buffer a live SSE stream).
    const allowCache = cacheAllowed(req.headers) && !streaming;
    const cacheKey = computeCacheKey(originalBody);
    if (allowCache) {
      const hit = await getCachedResponse(cacheKey); // null on miss OR Redis down
      if (hit) {
        cacheHit = true;
        status = hit.status;
        routedModel = hit.routedModel;
        if (hit.contentType) res.setHeader("content-type", hit.contentType);
        res.setHeader("x-slice-cache", "HIT");
        res.status(status).send(Buffer.from(hit.body, "utf8"));
        // No spend recorded on a hit — avoiding the provider call IS the saving.
        return;
      }
    }

    // ---- 3. ROUTE (Phase 3): classify + maybe rewrite the model -------------
    // routeRequest NEVER throws — feature off / override / judge failure all
    // return the original body unchanged.
    const routed = await routeRequest(originalBody, req.headers);
    const body = routed.body;
    routedModel = routed.decision.routedModel ?? requestedModel;
    verdict = routed.decision.verdict;
    judgeInputTokens = routed.decision.judgeInputTokens;
    judgeOutputTokens = routed.decision.judgeOutputTokens;

    // ---- 4. FORWARD: dispatch on the resolved model's provider --------------
    // Phase 8: a non-Anthropic provider is served by its ProviderAdapter, which
    // returns an Anthropic-shaped response. Anthropic stays on the EXACT original
    // path in the `else` below (streaming + header mirroring unchanged). Unknown
    // models resolve to "anthropic", so default behavior is untouched.
    const provider = providerForModel(routedModel);
    const adapter = provider === "anthropic" ? null : getAdapter(provider);
    if (adapter) {
      // Non-streaming only this phase: the adapter downgrades stream:true itself.
      const result = await adapter.complete({ body, headers: req.headers, stream: streaming });
      status = result.status;
      usage.input_tokens = result.usage.input_tokens;
      usage.output_tokens = result.usage.output_tokens;
      res.status(status);
      res.setHeader("content-type", result.contentType);
      res.setHeader("x-slice-cache", allowCache ? "MISS" : "OFF");
      if (result.streamDowngraded) res.setHeader("x-slice-stream", "downgraded");
      res.send(result.body);

      // ---- 5. STORE IN CACHE: same rules as the Anthropic path --------------
      if (allowCache && status >= 200 && status < 300) {
        await setCachedResponse(cacheKey, {
          status,
          contentType: result.contentType,
          body: result.body.toString("utf8"),
          routedModel,
        });
      }
    } else {
    const url = upstreamBase.replace(/\/$/, "") + req.originalUrl;
    const headers: Record<string, string> = {};
    for (const [key, value] of Object.entries(req.headers)) {
      if (value === undefined || HOP_BY_HOP.has(key.toLowerCase())) continue;
      headers[key] = Array.isArray(value) ? value.join(", ") : value;
    }
    const hasBody = req.method !== "GET" && req.method !== "HEAD" && body.length > 0;

    const upstream = await fetch(url, {
      method: req.method,
      headers,
      body: hasBody ? body : undefined,
      ...(hasBody ? { duplex: "half" } : {}),
    });
    status = upstream.status;

    // Mirror upstream status + headers back to the client.
    res.status(status);
    upstream.headers.forEach((value, key) => {
      if (!HOP_BY_HOP.has(key.toLowerCase())) res.setHeader(key, value);
    });

    if (!upstream.body) {
      res.setHeader("x-slice-cache", "BYPASS");
      res.end();
    } else if (streaming) {
      // Streaming path (unchanged, NOT cached): tee one branch to the client and
      // one to usage parsing so we never buffer the live SSE stream.
      res.setHeader("x-slice-cache", "BYPASS");
      const [clientStream, usageStream] = upstream.body.tee();
      const usageDone = extractUsageFromSSE(usageStream, usage);
      await new Promise<void>((resolve, reject) => {
        const nodeStream = Readable.fromWeb(clientStream as any);
        nodeStream.on("error", reject);
        res.on("close", resolve);
        res.on("finish", resolve);
        nodeStream.pipe(res);
      });
      await usageDone.catch(() => undefined);
    } else {
      // Non-streaming path: buffer the (small) JSON body so we can both return it
      // and store it in the cache. Extract usage from the same buffer.
      const bytes = Buffer.from(await upstream.arrayBuffer());
      extractUsageFromBuffer(bytes, usage);
      res.setHeader("x-slice-cache", allowCache ? "MISS" : "OFF");
      res.status(status).send(bytes);

      // ---- 5. STORE IN CACHE: only successful, cacheable responses ----------
      if (allowCache && status >= 200 && status < 300) {
        await setCachedResponse(cacheKey, {
          status,
          contentType: upstream.headers.get("content-type"),
          body: bytes.toString("utf8"),
          routedModel,
        });
      }
    }
    } // end provider dispatch (Anthropic inline branch)

    // ---- 6. RECORD SPEND: price tokens (main call + judge) ------------------
    // Price the request unconditionally so cost_usd is always logged/persisted
    // (this is what makes per-request spend observable). The cost = the main
    // call's tokens at the routed model's price PLUS the judge call's own tokens.
    costUsd =
      estimateCostUsd(routedModel, usage.input_tokens, usage.output_tokens) +
      estimateCostUsd(JUDGE_MODEL(), judgeInputTokens, judgeOutputTokens);

    // Feed the source-agnostic engine a dollar amount. FAIL-OPEN inside.
    if (budgetEnabled() && costUsd > 0) {
      await budget.record({
        account: team,
        amountUsd: costUsd,
        timestamp: Date.now(),
        source: "ai_request",
      });
    }
  } catch (err) {
    // Upstream unreachable / network error. Report a 502 and still log below.
    // Note: a throw here means step 6 (record-spend) was skipped for this request.
    logger.warn(
      { err: (err as Error).message, headersSent: res.headersSent, team },
      "proxy error while forwarding request",
    );
    if (!res.headersSent) {
      res.status(502).json({
        error: { type: "gateway_error", message: (err as Error).message },
      });
    } else {
      res.end();
    }
  } finally {
    // ---- 7. PERSIST ROW: one record, every path (block / hit / forward) -----
    const record: RequestLog = {
      method: req.method,
      path: req.path,
      model: routedModel, // the model actually used/forwarded
      status,
      latency_ms: Date.now() - start,
      input_tokens: usage.input_tokens,
      output_tokens: usage.output_tokens,
      requested_model: requestedModel,
      routed_model: routedModel,
      verdict,
      judge_input_tokens: judgeInputTokens,
      judge_output_tokens: judgeOutputTokens,
      cache_hit: cacheHit,
      cost_usd: costUsd,
      // Phase 8: which provider served this row (anthropic for every default path).
      provider: providerForModel(routedModel),
    };

    logRequest(record); // console, as in Phase 1

    // Fire-and-forget DB write: NOT awaited, errors swallowed (see db.ts).
    persistRequest(record);
  }
}
