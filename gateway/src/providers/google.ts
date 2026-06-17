import { randomUUID } from "node:crypto";
import { logger } from "../logger";
import type { CompleteInput, CompleteResult, ProviderAdapter } from "./types";

/**
 * Phase 8 — the Google Gemini adapter (non-streaming).
 *
 * Translates an Anthropic Messages request into a Gemini `generateContent`
 * request, calls the Gemini DEVELOPER API (generativelanguage.googleapis.com,
 * NOT Vertex AI) with the SERVER's GEMINI_API_KEY via the `x-goog-api-key`
 * header (never in the URL, never the client's key), then translates the response
 * back into Anthropic shape. The translation functions are pure and exported so
 * the unit tests exercise them with no network. `complete()` is the only part
 * that does I/O and it NEVER throws — any failure becomes an Anthropic-shaped
 * error result, exactly like the OpenAI adapter.
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

// --- Gemini-side shapes ------------------------------------------------------
type GeminiRole = "user" | "model";
interface GeminiPart {
  text: string;
}
interface GeminiContent {
  role: GeminiRole;
  parts: GeminiPart[];
}
interface GeminiGenerationConfig {
  maxOutputTokens?: number;
  temperature?: number;
  stopSequences?: string[];
}
export interface GeminiRequest {
  contents: GeminiContent[];
  system_instruction?: { parts: GeminiPart[] };
  generationConfig?: GeminiGenerationConfig;
}
interface GeminiResponseCandidate {
  content?: { role?: string; parts?: { text?: string }[] };
  finishReason?: string;
}
export interface GeminiResponse {
  candidates?: GeminiResponseCandidate[];
  usageMetadata?: { promptTokenCount?: number; candidatesTokenCount?: number };
  error?: { message?: string; status?: string; code?: number };
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

const DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta";

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

/** Translate an Anthropic request into a Gemini request. Pure. */
export function toGeminiRequest(req: AnthropicRequest): { request: GeminiRequest; notes: string[] } {
  const notes: string[] = [];

  const contents: GeminiContent[] = [];
  for (const m of req.messages ?? []) {
    // Anthropic user/assistant -> Gemini user/model; anything else -> user.
    const role: GeminiRole = m.role === "assistant" ? "model" : "user";
    const { text, dropped } = flattenContent(m.content);
    contents.push({ role, parts: [{ text }] });
    if (dropped.length) notes.push(`${role}: dropped non-text block(s): ${dropped.join(", ")}`);
  }

  const request: GeminiRequest = { contents };

  // Anthropic's top-level `system` becomes Gemini's system_instruction.
  if (req.system != null) {
    const { text, dropped } = flattenContent(req.system);
    if (text) request.system_instruction = { parts: [{ text }] };
    if (dropped.length) notes.push(`system: dropped non-text block(s): ${dropped.join(", ")}`);
  }

  // generationConfig — only include the fields that are actually set.
  const generationConfig: GeminiGenerationConfig = {};
  if (typeof req.max_tokens === "number") generationConfig.maxOutputTokens = req.max_tokens;
  if (typeof req.temperature === "number") generationConfig.temperature = req.temperature;
  if (Array.isArray(req.stop_sequences) && req.stop_sequences.length > 0) {
    generationConfig.stopSequences = req.stop_sequences;
  }
  if (Object.keys(generationConfig).length > 0) request.generationConfig = generationConfig;

  if (req.stream === true) notes.push("stream requested; downgraded to non-streaming (v1)");

  return { request, notes };
}

/** Gemini finishReason -> Anthropic stop_reason. */
const FINISH_REASON_MAP: Readonly<Record<string, string>> = {
  STOP: "end_turn",
  MAX_TOKENS: "max_tokens",
  SAFETY: "end_turn",
  RECITATION: "end_turn",
  OTHER: "end_turn",
};

export function mapFinishReason(reason: string | null | undefined): string {
  if (!reason) return "end_turn";
  return FINISH_REASON_MAP[reason] ?? "end_turn";
}

/**
 * Translate a Gemini response into an Anthropic response. Pure. Gemini returns no
 * message id, so the caller supplies a synthetic one. Missing/empty candidates or
 * parts (a safety block, or thinking that consumed the whole budget) yield an
 * empty text block rather than throwing.
 */
export function toAnthropicResponse(
  resp: GeminiResponse,
  model: string,
  id: string,
): AnthropicResponse {
  const candidate = resp.candidates?.[0];
  const parts = candidate?.content?.parts ?? [];
  const text = parts.map((p) => p.text ?? "").join("");
  return {
    id,
    type: "message",
    role: "assistant",
    model,
    content: [{ type: "text", text }],
    stop_reason: mapFinishReason(candidate?.finishReason),
    stop_sequence: null,
    usage: {
      input_tokens: resp.usageMetadata?.promptTokenCount ?? 0,
      output_tokens: resp.usageMetadata?.candidatesTokenCount ?? 0,
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
    provider: "google",
    streamDowngraded: false,
  };
}

export const googleAdapter: ProviderAdapter = {
  provider: "google",

  async complete(input: CompleteInput): Promise<CompleteResult> {
    // Parse the incoming Anthropic request.
    let anthropic: AnthropicRequest;
    try {
      anthropic = JSON.parse(input.body.toString("utf8")) as AnthropicRequest;
    } catch {
      return errorResult(400, "invalid_request_error", "request body is not valid JSON", "");
    }
    const model = typeof anthropic.model === "string" ? anthropic.model : "";

    // SERVER key only — the client's key is never used for Gemini.
    const apiKey = process.env.GEMINI_API_KEY;
    if (!apiKey) {
      return errorResult(500, "authentication_error", "GEMINI_API_KEY is not configured", model);
    }

    const { request, notes } = toGeminiRequest(anthropic);
    if (notes.length > 0) {
      logger.warn({ model, notes }, "gemini adapter applied v1 translation limits");
    }

    const base = (process.env.GEMINI_BASE_URL ?? DEFAULT_BASE_URL).replace(/\/$/, "");
    // The model goes in the URL PATH; the key goes in a header, never the URL.
    const url = `${base}/models/${encodeURIComponent(model)}:generateContent`;

    try {
      const res = await fetch(url, {
        method: "POST",
        headers: {
          "content-type": "application/json",
          "x-goog-api-key": apiKey,
        },
        body: JSON.stringify(request),
      });

      const raw = await res.text();
      let parsed: GeminiResponse;
      try {
        parsed = JSON.parse(raw) as GeminiResponse;
      } catch {
        parsed = {};
      }

      if (!res.ok) {
        const message = parsed.error?.message ?? `gemini http ${res.status}`;
        return errorResult(res.status, parsed.error?.status ?? "api_error", message, model);
      }

      const anthropicResp = toAnthropicResponse(parsed, model, `msg_${randomUUID().replace(/-/g, "")}`);
      return {
        status: res.status,
        body: Buffer.from(JSON.stringify(anthropicResp), "utf8"),
        contentType: "application/json",
        usage: {
          input_tokens: parsed.usageMetadata?.promptTokenCount ?? null,
          output_tokens: parsed.usageMetadata?.candidatesTokenCount ?? null,
        },
        model: anthropicResp.model,
        provider: "google",
        streamDowngraded: input.stream === true,
      };
    } catch (err) {
      // FAIL-OPEN: a transport failure becomes a clean Anthropic-shaped error,
      // never a thrown exception — the gateway and other providers are untouched.
      logger.warn({ err: (err as Error).message, model }, "gemini adapter request failed");
      return errorResult(502, "api_error", `gemini request failed: ${(err as Error).message}`, model);
    }
  },
};
