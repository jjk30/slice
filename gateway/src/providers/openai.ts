import { logger } from "../logger";
import type { CompleteInput, CompleteResult, ProviderAdapter } from "./types";

/**
 * Phase 8 — the OpenAI adapter (non-streaming).
 *
 * Translates an Anthropic Messages request into an OpenAI Chat Completions
 * request, calls OpenAI with the SERVER's OPENAI_API_KEY (never the client's
 * key), then translates the response back into Anthropic shape. The translation
 * functions are pure and exported so the unit tests exercise them with no
 * network. `complete()` is the only part that does I/O and it NEVER throws — any
 * failure becomes an Anthropic-shaped error result.
 */

// --- Anthropic-side request shapes (input) -----------------------------------
interface AnthropicTextBlock {
  type: "text";
  text: string;
}
interface AnthropicUnknownBlock {
  type: string;
  [key: string]: unknown;
}
type AnthropicContentBlock = AnthropicTextBlock | AnthropicUnknownBlock;
type AnthropicContent = string | AnthropicContentBlock[];

interface AnthropicMessage {
  role?: string;
  content?: AnthropicContent;
}

export interface AnthropicRequest {
  model?: string;
  system?: AnthropicContent;
  messages?: AnthropicMessage[];
  max_tokens?: number;
  stop_sequences?: string[];
  temperature?: number;
  stream?: boolean;
  [key: string]: unknown;
}

// --- OpenAI-side shapes ------------------------------------------------------
type OpenAIRole = "system" | "user" | "assistant";
interface OpenAIMessage {
  role: OpenAIRole;
  content: string;
}
export interface OpenAIRequest {
  model: string;
  messages: OpenAIMessage[];
  max_tokens?: number;
  max_completion_tokens?: number;
  stop?: string[];
  temperature?: number;
}
interface OpenAIChoice {
  message?: { role?: string; content?: string | null };
  finish_reason?: string | null;
}
export interface OpenAIResponse {
  id?: string;
  model?: string;
  choices?: OpenAIChoice[];
  usage?: { prompt_tokens?: number; completion_tokens?: number };
  error?: { message?: string; type?: string };
}

// --- Anthropic-side response shape (output) ----------------------------------
export interface AnthropicResponse {
  id: string;
  type: "message";
  role: "assistant";
  model: string;
  content: { type: "text"; text: string }[];
  stop_reason: string;
  stop_sequence: string | null;
  usage: { input_tokens: number; output_tokens: number };
}

const DEFAULT_BASE_URL = "https://api.openai.com/v1";

/**
 * Flatten Anthropic content into plain text. v1 is TEXT-ONLY: any non-text block
 * (image, tool_use, tool_result, ...) is dropped but clearly MARKED inline so the
 * request never crashes and the omission is visible. Returns the dropped types
 * for logging.
 */
function flattenContent(content: AnthropicContent | undefined): { text: string; dropped: string[] } {
  if (content == null) return { text: "", dropped: [] };
  if (typeof content === "string") return { text: content, dropped: [] };

  const parts: string[] = [];
  const dropped: string[] = [];
  for (const block of content) {
    if (block && block.type === "text" && typeof (block as AnthropicTextBlock).text === "string") {
      parts.push((block as AnthropicTextBlock).text);
    } else if (block && typeof block.type === "string") {
      dropped.push(block.type);
      parts.push(`[slice v1: omitted ${block.type} content — text-only]`);
    }
  }
  return { text: parts.join("\n"), dropped };
}

/**
 * Newer OpenAI reasoning models (o1/o3/o4, the o-* family) reject `max_tokens`
 * and require `max_completion_tokens`. Everything else keeps `max_tokens`.
 */
function usesMaxCompletionTokens(model: string): boolean {
  return /^o\d/i.test(model) || /^o-/i.test(model);
}

/** Translate an Anthropic request into an OpenAI request. Pure. */
export function toOpenAIRequest(req: AnthropicRequest): { request: OpenAIRequest; notes: string[] } {
  const notes: string[] = [];
  const messages: OpenAIMessage[] = [];

  // Anthropic's top-level `system` becomes a leading system message.
  if (req.system != null) {
    const { text, dropped } = flattenContent(req.system);
    if (text) messages.push({ role: "system", content: text });
    if (dropped.length) notes.push(`system: dropped non-text block(s): ${dropped.join(", ")}`);
  }

  for (const m of req.messages ?? []) {
    // Anthropic messages are user/assistant; anything else is treated as user.
    const role: OpenAIRole = m.role === "assistant" ? "assistant" : "user";
    const { text, dropped } = flattenContent(m.content);
    messages.push({ role, content: text });
    if (dropped.length) notes.push(`${role}: dropped non-text block(s): ${dropped.join(", ")}`);
  }

  const model = typeof req.model === "string" ? req.model : "";
  const request: OpenAIRequest = { model, messages };

  if (typeof req.max_tokens === "number") {
    if (usesMaxCompletionTokens(model)) request.max_completion_tokens = req.max_tokens;
    else request.max_tokens = req.max_tokens;
  }
  if (Array.isArray(req.stop_sequences) && req.stop_sequences.length > 0) {
    request.stop = req.stop_sequences;
  }
  if (typeof req.temperature === "number") request.temperature = req.temperature;
  if (req.stream === true) notes.push("stream requested; downgraded to non-streaming (v1)");

  return { request, notes };
}

/** OpenAI finish_reason -> Anthropic stop_reason. */
const FINISH_REASON_MAP: Readonly<Record<string, string>> = {
  stop: "end_turn",
  length: "max_tokens",
  tool_calls: "tool_use",
  content_filter: "end_turn",
};

export function mapFinishReason(reason: string | null | undefined): string {
  if (!reason) return "end_turn";
  return FINISH_REASON_MAP[reason] ?? "end_turn";
}

/** Translate an OpenAI response into an Anthropic response. Pure. */
export function toAnthropicResponse(resp: OpenAIResponse, fallbackModel: string): AnthropicResponse {
  const choice = resp.choices?.[0];
  const content = choice?.message?.content;
  return {
    id: resp.id ?? "",
    type: "message",
    role: "assistant",
    model: resp.model ?? fallbackModel,
    content: [{ type: "text", text: typeof content === "string" ? content : "" }],
    stop_reason: mapFinishReason(choice?.finish_reason),
    stop_sequence: null,
    usage: {
      input_tokens: resp.usage?.prompt_tokens ?? 0,
      output_tokens: resp.usage?.completion_tokens ?? 0,
    },
  };
}

/** Build an Anthropic-shaped error envelope as a `CompleteResult`. */
function errorResult(
  status: number,
  type: string,
  message: string,
  model: string,
): CompleteResult {
  const body = Buffer.from(JSON.stringify({ type: "error", error: { type, message } }), "utf8");
  return {
    status,
    body,
    contentType: "application/json",
    usage: { input_tokens: null, output_tokens: null },
    model,
    provider: "openai",
    streamDowngraded: false,
  };
}

export const openaiAdapter: ProviderAdapter = {
  provider: "openai",

  async complete(input: CompleteInput): Promise<CompleteResult> {
    // Parse the incoming Anthropic request.
    let anthropic: AnthropicRequest;
    try {
      anthropic = JSON.parse(input.body.toString("utf8")) as AnthropicRequest;
    } catch {
      return errorResult(400, "invalid_request_error", "request body is not valid JSON", "");
    }
    const model = typeof anthropic.model === "string" ? anthropic.model : "";

    // SERVER key only — the client's key is never used for OpenAI.
    const apiKey = process.env.OPENAI_API_KEY;
    if (!apiKey) {
      return errorResult(500, "authentication_error", "OPENAI_API_KEY is not configured", model);
    }

    const { request, notes } = toOpenAIRequest(anthropic);
    if (notes.length > 0) {
      logger.warn({ model, notes }, "openai adapter applied v1 translation limits");
    }

    const base = (process.env.OPENAI_BASE_URL ?? DEFAULT_BASE_URL).replace(/\/$/, "");

    try {
      const res = await fetch(`${base}/chat/completions`, {
        method: "POST",
        headers: {
          "content-type": "application/json",
          authorization: `Bearer ${apiKey}`,
        },
        body: JSON.stringify(request),
      });

      const raw = await res.text();
      let parsed: OpenAIResponse;
      try {
        parsed = JSON.parse(raw) as OpenAIResponse;
      } catch {
        parsed = {};
      }

      if (!res.ok) {
        const message = parsed.error?.message ?? `openai http ${res.status}`;
        return errorResult(res.status, parsed.error?.type ?? "api_error", message, model);
      }

      const anthropicResp = toAnthropicResponse(parsed, model);
      return {
        status: res.status,
        body: Buffer.from(JSON.stringify(anthropicResp), "utf8"),
        contentType: "application/json",
        usage: {
          input_tokens: parsed.usage?.prompt_tokens ?? null,
          output_tokens: parsed.usage?.completion_tokens ?? null,
        },
        model: anthropicResp.model,
        provider: "openai",
        streamDowngraded: input.stream === true,
      };
    } catch (err) {
      // FAIL-OPEN: a transport failure becomes a clean Anthropic-shaped error,
      // never a thrown exception — the gateway and other providers are untouched.
      logger.warn({ err: (err as Error).message, model }, "openai adapter request failed");
      return errorResult(502, "api_error", `openai request failed: ${(err as Error).message}`, model);
    }
  },
};
