import type { IncomingHttpHeaders } from "node:http";

/**
 * Phase 8 — multi-provider adapters.
 *
 * One shared shape so every provider (Anthropic today, OpenAI now, Gemini/Grok
 * later) plugs into the same dispatch. An adapter takes an ANTHROPIC-format
 * request and returns an ANTHROPIC-format response, so the client always speaks
 * one dialect regardless of which provider actually ran the model.
 *
 * NON-STREAMING FIRST: this phase translates only non-streaming JSON. A streaming
 * request to a non-Anthropic model is downgraded to a single non-streaming call
 * (see `streamDowngraded`); SSE translation is intentionally out of scope.
 */

export type ProviderName = "anthropic" | "openai";

/** Token usage recovered from a provider response (nullable when absent). */
export interface ProviderUsage {
  input_tokens: number | null;
  output_tokens: number | null;
}

/** What an adapter receives — the raw incoming Anthropic request plus context. */
export interface CompleteInput {
  /** The incoming Anthropic-format request body, verbatim. */
  body: Buffer;
  /** Client request headers. Anthropic reuses the client key; OpenAI does NOT. */
  headers: IncomingHttpHeaders;
  /** True when the client asked to stream — the adapter may downgrade it. */
  stream: boolean;
}

/** What an adapter returns — an Anthropic-shaped response ready for the client. */
export interface CompleteResult {
  status: number; // HTTP status to mirror back to the client
  body: Buffer; // Anthropic-shaped response (or Anthropic-shaped error) bytes
  contentType: string;
  usage: ProviderUsage; // feeds the existing cost/budget/ranking path
  model: string; // the model that actually ran
  provider: ProviderName;
  streamDowngraded: boolean; // true when a stream:true request was served non-streaming
}

/**
 * One provider integration. Implementations MUST be fail-safe: translate provider
 * errors into an Anthropic-shaped error `CompleteResult` rather than throwing, so
 * a provider outage can never crash the gateway or affect another provider's path.
 */
export interface ProviderAdapter {
  readonly provider: ProviderName;
  complete(input: CompleteInput): Promise<CompleteResult>;
}
