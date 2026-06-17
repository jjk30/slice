import type { Request, Response } from "express";
import type { IncomingHttpHeaders } from "node:http";
import { logRequest, logger, type RequestLog } from "./logger";
import { persistRequest } from "./db";
import { ladderTiers } from "./router";
import { costOf } from "./ranking";
import { estimateCostUsd } from "./pricing";
import {
  buildLadder,
  makeTaskBudget,
  runAgentLoop,
  V1Checker,
  type AgentDeps,
  type AttemptLog,
  type CheckContext,
  type ModelAttemptResult,
} from "./agent";

/**
 * Phase 7 — real wiring for the agent loop.
 *
 * The pure loop in agent.ts owns no I/O; THIS file builds the concrete
 * dependencies (real provider calls, the Haiku verifier, the per-task budget
 * engine, the Postgres logging path) and hands them to {@link runAgentLoop}. It is
 * the only Phase 7 file that touches the network, env, or Express — kept separate
 * so the loop stays unit-testable with fakes.
 *
 * KEY HANDLING (same contract as router.ts): every upstream call REUSES THE
 * CLIENT'S API KEY from the incoming request headers. We never read a key from
 * env or source, never store it, and never log it.
 */

const HOP_BY_HOP = new Set(["host", "content-length", "connection", "transfer-encoding"]);

const DEFAULT_VERIFIER_MODEL = "claude-haiku-4-5-20251001";
// Rough output-token assumption used only for the pre-flight cost ESTIMATE that
// gates each attempt. The real cost is priced from actual usage after the call.
const EST_OUTPUT_TOKENS = 1024;

const cfg = () => ({
  upstream: (process.env.ANTHROPIC_UPSTREAM ?? "https://api.anthropic.com").replace(/\/$/, ""),
  taskBudgetUsd: (() => {
    const n = Number(process.env.AGENT_TASK_BUDGET_USD ?? "0.50");
    return Number.isFinite(n) && n > 0 ? n : 0.5;
  })(),
  verifierModel: process.env.AGENT_VERIFIER_MODEL ?? DEFAULT_VERIFIER_MODEL,
});

/** Opt-in switch: the loop only runs when the client sends `x-slice-agent: on`. */
export function agentEnabled(headers: IncomingHttpHeaders): boolean {
  const header = headers["x-slice-agent"];
  return typeof header === "string" && header.trim().toLowerCase() === "on";
}

// --- Anthropic request/response shapes (minimal, typed — no `any`) -----------
interface TextBlock {
  type: string;
  text?: string;
}
interface MessagesResponse {
  content?: TextBlock[];
  usage?: { input_tokens?: number; output_tokens?: number };
}
interface MessagesRequestBody {
  model?: string;
  messages?: { role?: string; content?: string | TextBlock[] }[];
  response_format?: { type?: string };
  [key: string]: unknown;
}

/** Headers for an upstream `/v1/messages` call — reuses the client's key. */
function agentHeaders(clientHeaders: IncomingHttpHeaders): Record<string, string> {
  const out: Record<string, string> = { "content-type": "application/json" };
  for (const key of ["x-api-key", "authorization", "anthropic-version", "anthropic-beta"]) {
    const value = clientHeaders[key];
    if (typeof value === "string") out[key] = value;
  }
  if (!out["anthropic-version"]) out["anthropic-version"] = "2023-06-01";
  return out;
}

/** Forward headers for a raw passthrough — strip hop-by-hop, keep the rest. */
function forwardHeaders(clientHeaders: IncomingHttpHeaders): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [key, value] of Object.entries(clientHeaders)) {
    if (value === undefined || HOP_BY_HOP.has(key.toLowerCase())) continue;
    out[key] = Array.isArray(value) ? value.join(", ") : value;
  }
  return out;
}

function extractAnswerText(rawBody: string): string {
  try {
    const data = JSON.parse(rawBody) as MessagesResponse;
    return (data.content ?? [])
      .filter((b) => b.type === "text")
      .map((b) => b.text ?? "")
      .join("\n")
      .trim();
  } catch {
    return ""; // error body / non-JSON — no answer text
  }
}

function extractUsage(rawBody: string): { input: number | null; output: number | null } {
  try {
    const u = (JSON.parse(rawBody) as MessagesResponse).usage;
    return {
      input: typeof u?.input_tokens === "number" ? u.input_tokens : null,
      output: typeof u?.output_tokens === "number" ? u.output_tokens : null,
    };
  } catch {
    return { input: null, output: null };
  }
}

/** The user's request text — same idea as the router's prompt extraction. */
function extractRequestText(parsed: MessagesRequestBody): string {
  const messages = Array.isArray(parsed.messages) ? parsed.messages : [];
  const parts: string[] = [];
  for (const m of messages) {
    if (m?.role && m.role !== "user") continue;
    const content = m?.content;
    if (typeof content === "string") parts.push(content);
    else if (Array.isArray(content)) {
      for (const block of content) {
        if (block?.type === "text" && typeof block.text === "string") parts.push(block.text);
      }
    }
  }
  return parts.join("\n").trim();
}

/** Cheap, conservative "did the client expect JSON?" detector. */
function detectExpectsJson(parsed: MessagesRequestBody, headers: IncomingHttpHeaders): boolean {
  const hint = headers["x-slice-expect"];
  if (typeof hint === "string" && hint.trim().toLowerCase() === "json") return true;
  const fmt = parsed.response_format?.type;
  return typeof fmt === "string" && fmt.toLowerCase().includes("json");
}

/**
 * Handle one `x-slice-agent: on` request: build the ladder + real deps, run the
 * bounded loop, and replay the best answer to the client. Mirrors proxy.ts's
 * fail-safety — any unexpected throw yields a clean 502, never a hang.
 */
export async function handleAgentRequest(req: Request, res: Response): Promise<void> {
  const start = Date.now();
  const { upstream, taskBudgetUsd, verifierModel } = cfg();

  const originalBody: Buffer = Buffer.isBuffer(req.body) ? req.body : Buffer.alloc(0);

  // Parse once. A non-JSON / non-messages body can't run the loop — fall straight
  // through to a normal passthrough so the request never breaks.
  let parsed: MessagesRequestBody | null = null;
  try {
    if (originalBody.length > 0) parsed = JSON.parse(originalBody.toString("utf8")) as MessagesRequestBody;
  } catch {
    parsed = null;
  }
  const requestedModel = typeof parsed?.model === "string" ? parsed.model : null;

  // --- Real dependency: ONE passthrough call (also the fail-open escape hatch).
  const passthrough = async (): Promise<ModelAttemptResult> => {
    const hasBody = req.method !== "GET" && req.method !== "HEAD" && originalBody.length > 0;
    const upstreamRes = await fetch(upstream + req.originalUrl, {
      method: req.method,
      headers: forwardHeaders(req.headers),
      body: hasBody ? originalBody : undefined,
      ...(hasBody ? { duplex: "half" } : {}),
    });
    const rawBody = await upstreamRes.text();
    const usage = extractUsage(rawBody);
    return {
      status: upstreamRes.status,
      rawBody,
      answerText: extractAnswerText(rawBody),
      costUsd: estimateCostUsd(requestedModel, usage.input, usage.output),
      inputTokens: usage.input,
      outputTokens: usage.output,
    };
  };

  try {
    // No usable body to drive the loop: just passthrough once and return it.
    if (!parsed) {
      const result = await passthrough();
      writeAnswer(res, result, requestedModel, 0, "failed_open");
      logAttemptRow(req, start, requestedModel, {
        attempt: 0,
        model: requestedModel ?? "(passthrough)",
        status: result.status,
        costUsd: result.costUsd,
        inputTokens: result.inputTokens,
        outputTokens: result.outputTokens,
        check: "pass",
        escalated: false,
        reason: "non-JSON body; passthrough",
      });
      return;
    }

    // --- Build the ladder from the tier config + Phase 6 ranking. ------------
    const { cheapTier, strongTier } = ladderTiers();
    const ladder = buildLadder(cheapTier, strongTier, costOf);

    const checkContext: CheckContext = {
      requestText: extractRequestText(parsed),
      expectsJson: detectExpectsJson(parsed, req.headers),
    };

    // --- Real dependency: call ONE model (rewrites `model`, forces non-stream).
    const callModel = async (model: string, requestBody: Buffer): Promise<ModelAttemptResult> => {
      const body = JSON.parse(requestBody.toString("utf8")) as MessagesRequestBody;
      body.model = model;
      delete body.stream; // the loop must read the FULL answer to check it
      const upstreamRes = await fetch(`${upstream}/v1/messages`, {
        method: "POST",
        headers: agentHeaders(req.headers),
        body: JSON.stringify(body),
      });
      const rawBody = await upstreamRes.text();
      const usage = extractUsage(rawBody);
      return {
        status: upstreamRes.status,
        rawBody,
        answerText: extractAnswerText(rawBody),
        costUsd: estimateCostUsd(model, usage.input, usage.output),
        inputTokens: usage.input,
        outputTokens: usage.output,
      };
    };

    // --- Real dependency: the cheap Haiku verifier (soft check). -------------
    const verify = async (requestText: string, answerText: string): Promise<boolean> => {
      const upstreamRes = await fetch(`${upstream}/v1/messages`, {
        method: "POST",
        headers: agentHeaders(req.headers),
        body: JSON.stringify({
          model: verifierModel,
          max_tokens: 4,
          system:
            "You are a strict grader. Reply with exactly one word: YES if the " +
            "candidate answer fully and correctly answers the request, else NO.",
          messages: [
            {
              role: "user",
              content:
                `Request:\n${requestText.slice(0, 4000)}\n\n` +
                `Candidate answer:\n${answerText.slice(0, 4000)}\n\n` +
                "Does the candidate fully answer the request? Reply YES or NO.",
            },
          ],
        }),
      });
      if (!upstreamRes.ok) throw new Error(`verifier http ${upstreamRes.status}`);
      const data = (await upstreamRes.json()) as MessagesResponse;
      const raw = (data.content ?? [])
        .filter((b) => b.type === "text")
        .map((b) => b.text ?? "")
        .join(" ")
        .trim()
        .toUpperCase();
      return raw.startsWith("YES");
    };

    // --- Real dependency: pre-flight cost estimate (the circuit breaker gate).
    const promptBytes = originalBody.length;
    const estimateCost = (model: string): number =>
      estimateCostUsd(model, Math.ceil(promptBytes / 4), EST_OUTPUT_TOKENS);

    const deps: AgentDeps = {
      requestBody: originalBody,
      requestedModel,
      checkContext,
      callModel,
      estimateCost,
      checker: new V1Checker({ verify }),
      budget: makeTaskBudget(taskBudgetUsd),
      logAttempt: (entry) => logAttemptRow(req, start, requestedModel, entry),
      passthrough,
    };

    const result = await runAgentLoop(ladder, deps);
    if (!result.answer) {
      // Both the loop AND the passthrough produced nothing — surface a clean 502.
      res.status(502).json({ error: { type: "agent_error", message: "agent produced no answer" } });
      return;
    }
    writeAnswer(res, result.answer, result.model, result.attempts, result.stoppedReason);
  } catch (err) {
    logger.warn({ err: (err as Error).message }, "agent loop handler error");
    if (!res.headersSent) {
      res.status(502).json({ error: { type: "gateway_error", message: (err as Error).message } });
    } else {
      res.end();
    }
  }
}

/** Replay an answer to the client, tagged with what the loop did. */
function writeAnswer(
  res: Response,
  answer: ModelAttemptResult,
  model: string | null,
  attempts: number,
  stoppedReason: string,
): void {
  if (res.headersSent) return;
  res.status(answer.status);
  res.setHeader("content-type", "application/json");
  res.setHeader("x-slice-agent", "on");
  res.setHeader("x-slice-agent-model", model ?? "");
  res.setHeader("x-slice-agent-attempts", String(attempts));
  res.setHeader("x-slice-agent-stop", stoppedReason);
  res.send(answer.rawBody);
}

/** Map one {@link AttemptLog} onto the existing Postgres logging path. */
function logAttemptRow(
  req: Request,
  start: number,
  requestedModel: string | null,
  entry: AttemptLog,
): void {
  const record: RequestLog = {
    method: req.method,
    path: req.path,
    model: entry.model,
    status: entry.status,
    latency_ms: Date.now() - start,
    input_tokens: entry.inputTokens,
    output_tokens: entry.outputTokens,
    requested_model: requestedModel,
    routed_model: entry.model,
    verdict: null,
    judge_input_tokens: null,
    judge_output_tokens: null,
    cache_hit: false,
    cost_usd: entry.costUsd,
    agent_attempt: entry.attempt,
    agent_check: entry.check,
    agent_escalated: entry.escalated,
  };
  logRequest(record); // console line
  persistRequest(record); // fire-and-forget DB row
}
