import { describe, it, expect } from "vitest";
import {
  toOpenAIRequest,
  toAnthropicResponse,
  mapFinishReason,
  type AnthropicRequest,
  type OpenAIResponse,
} from "../src/providers/openai";
import { providerForModel, getAdapter } from "../src/providers/registry";
import { estimateCostUsd } from "../src/pricing";

/**
 * Phase 8 unit tests — pure translation + cost + dispatch, NO network. The real
 * OpenAI API is never called; only the pure functions are exercised.
 */

// --- Request translation: Anthropic -> OpenAI --------------------------------

describe("toOpenAIRequest", () => {
  it("turns the top-level system field into a leading system message", () => {
    const req: AnthropicRequest = {
      model: "gpt-4o-mini",
      system: "You are terse.",
      messages: [{ role: "user", content: "hi" }],
    };
    const { request } = toOpenAIRequest(req);
    expect(request.messages[0]).toEqual({ role: "system", content: "You are terse." });
    expect(request.messages[1]).toEqual({ role: "user", content: "hi" });
  });

  it("maps user/assistant roles and string + text-block content", () => {
    const req: AnthropicRequest = {
      model: "gpt-4o-mini",
      messages: [
        { role: "user", content: "first" },
        { role: "assistant", content: [{ type: "text", text: "reply" }] },
        { role: "user", content: [{ type: "text", text: "a" }, { type: "text", text: "b" }] },
      ],
    };
    const { request } = toOpenAIRequest(req);
    expect(request.messages).toEqual([
      { role: "user", content: "first" },
      { role: "assistant", content: "reply" },
      { role: "user", content: "a\nb" },
    ]);
  });

  it("maps max_tokens and stop_sequences, and passes temperature through", () => {
    const req: AnthropicRequest = {
      model: "gpt-4o-mini",
      messages: [{ role: "user", content: "hi" }],
      max_tokens: 256,
      stop_sequences: ["STOP", "END"],
      temperature: 0.4,
    };
    const { request } = toOpenAIRequest(req);
    expect(request.max_tokens).toBe(256);
    expect(request.max_completion_tokens).toBeUndefined();
    expect(request.stop).toEqual(["STOP", "END"]);
    expect(request.temperature).toBe(0.4);
  });

  it("uses max_completion_tokens for the o-* reasoning models", () => {
    const { request } = toOpenAIRequest({
      model: "o3-mini",
      messages: [{ role: "user", content: "hi" }],
      max_tokens: 1000,
    });
    expect(request.max_completion_tokens).toBe(1000);
    expect(request.max_tokens).toBeUndefined();
  });

  it("does not crash on non-text content: passes text, marks the rest (v1 limit)", () => {
    const { request, notes } = toOpenAIRequest({
      model: "gpt-4o-mini",
      messages: [
        {
          role: "user",
          content: [
            { type: "text", text: "describe this" },
            { type: "image", source: { type: "base64", data: "..." } },
          ],
        },
      ],
    });
    expect(request.messages[0].content).toContain("describe this");
    expect(request.messages[0].content).toContain("omitted image content");
    expect(notes.join(" ")).toContain("dropped non-text block(s): image");
  });

  it("notes the streaming downgrade when stream:true is requested", () => {
    const { notes } = toOpenAIRequest({
      model: "gpt-4o-mini",
      messages: [{ role: "user", content: "hi" }],
      stream: true,
    });
    expect(notes.join(" ")).toContain("downgraded to non-streaming");
  });
});

// --- Response translation: OpenAI -> Anthropic -------------------------------

describe("toAnthropicResponse", () => {
  it("maps a choice to an Anthropic text block with usage", () => {
    const resp: OpenAIResponse = {
      id: "chatcmpl-123",
      model: "gpt-4o-mini-2024-07-18",
      choices: [{ message: { role: "assistant", content: "hello world" }, finish_reason: "stop" }],
      usage: { prompt_tokens: 12, completion_tokens: 7 },
    };
    const out = toAnthropicResponse(resp, "gpt-4o-mini");
    expect(out.type).toBe("message");
    expect(out.role).toBe("assistant");
    expect(out.id).toBe("chatcmpl-123");
    expect(out.model).toBe("gpt-4o-mini-2024-07-18");
    expect(out.content).toEqual([{ type: "text", text: "hello world" }]);
    expect(out.stop_reason).toBe("end_turn");
    expect(out.usage).toEqual({ input_tokens: 12, output_tokens: 7 });
  });

  it("falls back to the requested model when the response omits one", () => {
    const out = toAnthropicResponse({ choices: [{ message: { content: "x" } }] }, "gpt-4o");
    expect(out.model).toBe("gpt-4o");
  });
});

describe("mapFinishReason", () => {
  it("maps each OpenAI finish_reason to the right Anthropic stop_reason", () => {
    expect(mapFinishReason("stop")).toBe("end_turn");
    expect(mapFinishReason("length")).toBe("max_tokens");
    expect(mapFinishReason("tool_calls")).toBe("tool_use");
    expect(mapFinishReason("content_filter")).toBe("end_turn");
  });

  it("defaults unknown/missing reasons to end_turn", () => {
    expect(mapFinishReason("something_new")).toBe("end_turn");
    expect(mapFinishReason(null)).toBe("end_turn");
    expect(mapFinishReason(undefined)).toBe("end_turn");
  });
});

// --- Cost: tokens x price table ----------------------------------------------

describe("OpenAI cost", () => {
  it("prices gpt-4o-mini usage from the central table", () => {
    // gpt-4o-mini: $0.15 / 1M input, $0.60 / 1M output.
    // 1000 in -> 0.00015, 500 out -> 0.00030 => 0.00045
    expect(estimateCostUsd("gpt-4o-mini", 1000, 500)).toBeCloseTo(0.00045, 8);
  });

  it("prices gpt-4o usage from the central table", () => {
    // gpt-4o: $2.50 / 1M input, $10.00 / 1M output.
    expect(estimateCostUsd("gpt-4o", 1_000_000, 1_000_000)).toBeCloseTo(12.5, 6);
  });
});

// --- Registry / dispatch -----------------------------------------------------

describe("providerForModel", () => {
  it("routes gpt-* and o-* models to OpenAI", () => {
    expect(providerForModel("gpt-4o-mini")).toBe("openai");
    expect(providerForModel("gpt-4.1")).toBe("openai");
    expect(providerForModel("o3-mini")).toBe("openai");
    expect(providerForModel("o1")).toBe("openai");
  });

  it("routes claude-* models to the Anthropic path", () => {
    expect(providerForModel("claude-opus-4-8")).toBe("anthropic");
    expect(providerForModel("claude-haiku-4-5-20251001")).toBe("anthropic");
  });

  it("routes unknown models (and null) to the Anthropic default", () => {
    expect(providerForModel("mystery-model-9000")).toBe("anthropic");
    expect(providerForModel(null)).toBe("anthropic");
  });
});

describe("getAdapter", () => {
  it("returns the OpenAI adapter for openai, and null for anthropic (inline path)", () => {
    expect(getAdapter("openai")?.provider).toBe("openai");
    expect(getAdapter("anthropic")).toBeNull();
  });
});
