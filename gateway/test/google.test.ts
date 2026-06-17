import { describe, it, expect } from "vitest";
import {
  toGeminiRequest,
  toAnthropicResponse,
  mapFinishReason,
  type AnthropicRequest,
  type GeminiResponse,
} from "../src/providers/google";
import { providerForModel, getAdapter } from "../src/providers/registry";
import { estimateCostUsd } from "../src/pricing";

/**
 * Phase 8 (Gemini) unit tests — pure translation + cost + dispatch, NO network.
 * The real Gemini API is never called; only the pure functions are exercised.
 */

// --- Request translation: Anthropic -> Gemini --------------------------------

describe("toGeminiRequest", () => {
  it("turns the top-level system field into system_instruction", () => {
    const req: AnthropicRequest = {
      model: "gemini-2.5-flash-lite",
      system: "You are terse.",
      messages: [{ role: "user", content: "hi" }],
    };
    const { request } = toGeminiRequest(req);
    expect(request.system_instruction).toEqual({ parts: [{ text: "You are terse." }] });
  });

  it("maps assistant role to model, keeps user, and content to parts", () => {
    const req: AnthropicRequest = {
      model: "gemini-2.5-flash-lite",
      messages: [
        { role: "user", content: "first" },
        { role: "assistant", content: [{ type: "text", text: "reply" }] },
        { role: "user", content: [{ type: "text", text: "a" }, { type: "text", text: "b" }] },
      ],
    };
    const { request } = toGeminiRequest(req);
    expect(request.contents).toEqual([
      { role: "user", parts: [{ text: "first" }] },
      { role: "model", parts: [{ text: "reply" }] },
      { role: "user", parts: [{ text: "a\nb" }] },
    ]);
  });

  it("maps max_tokens to maxOutputTokens and stop_sequences to stopSequences", () => {
    const { request } = toGeminiRequest({
      model: "gemini-2.5-flash-lite",
      messages: [{ role: "user", content: "hi" }],
      max_tokens: 256,
      stop_sequences: ["STOP", "END"],
    });
    expect(request.generationConfig?.maxOutputTokens).toBe(256);
    expect(request.generationConfig?.stopSequences).toEqual(["STOP", "END"]);
  });

  it("includes temperature only when it is set", () => {
    const withTemp = toGeminiRequest({
      model: "gemini-2.5-flash-lite",
      messages: [{ role: "user", content: "hi" }],
      temperature: 0.4,
    });
    expect(withTemp.request.generationConfig?.temperature).toBe(0.4);

    const withoutTemp = toGeminiRequest({
      model: "gemini-2.5-flash-lite",
      messages: [{ role: "user", content: "hi" }],
    });
    // No generationConfig fields set at all -> the object is omitted entirely.
    expect(withoutTemp.request.generationConfig).toBeUndefined();
  });

  it("does not crash on non-text content: passes text, marks the rest (v1 limit)", () => {
    const { request, notes } = toGeminiRequest({
      model: "gemini-2.5-flash-lite",
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
    expect(request.contents[0].parts[0].text).toContain("describe this");
    expect(request.contents[0].parts[0].text).toContain("omitted image content");
    expect(notes.join(" ")).toContain("dropped non-text block(s): image");
  });

  it("notes the streaming downgrade when stream:true is requested", () => {
    const { notes } = toGeminiRequest({
      model: "gemini-2.5-flash-lite",
      messages: [{ role: "user", content: "hi" }],
      stream: true,
    });
    expect(notes.join(" ")).toContain("downgraded to non-streaming");
  });
});

// --- Response translation: Gemini -> Anthropic -------------------------------

describe("toAnthropicResponse", () => {
  it("joins candidate parts into a single Anthropic text block with usage", () => {
    const resp: GeminiResponse = {
      candidates: [
        {
          content: { role: "model", parts: [{ text: "hello " }, { text: "world" }] },
          finishReason: "STOP",
        },
      ],
      usageMetadata: { promptTokenCount: 12, candidatesTokenCount: 7 },
    };
    const out = toAnthropicResponse(resp, "gemini-2.5-flash-lite", "msg_fixed");
    expect(out.type).toBe("message");
    expect(out.role).toBe("assistant");
    expect(out.id).toBe("msg_fixed");
    expect(out.model).toBe("gemini-2.5-flash-lite");
    expect(out.content).toEqual([{ type: "text", text: "hello world" }]);
    expect(out.stop_reason).toBe("end_turn");
    expect(out.usage).toEqual({ input_tokens: 12, output_tokens: 7 });
  });

  it("returns an empty text block (no throw) when candidates are missing/empty", () => {
    const blocked: GeminiResponse = { candidates: [], usageMetadata: { promptTokenCount: 5 } };
    const out = toAnthropicResponse(blocked, "gemini-2.5-flash-lite", "msg_x");
    expect(out.content).toEqual([{ type: "text", text: "" }]);
    // Missing candidatesTokenCount defaults to 0.
    expect(out.usage).toEqual({ input_tokens: 5, output_tokens: 0 });
    expect(out.stop_reason).toBe("end_turn");

    const empty = toAnthropicResponse({}, "gemini-2.5-flash-lite", "msg_y");
    expect(empty.content).toEqual([{ type: "text", text: "" }]);
    expect(empty.usage).toEqual({ input_tokens: 0, output_tokens: 0 });
  });

  it("maps a SAFETY/MAX_TOKENS finishReason even with no parts", () => {
    const safety: GeminiResponse = { candidates: [{ finishReason: "SAFETY" }] };
    expect(toAnthropicResponse(safety, "gemini-2.5-flash-lite", "id").stop_reason).toBe("end_turn");
    const truncated: GeminiResponse = { candidates: [{ finishReason: "MAX_TOKENS" }] };
    expect(toAnthropicResponse(truncated, "gemini-2.5-flash-lite", "id").stop_reason).toBe("max_tokens");
  });
});

describe("mapFinishReason", () => {
  it("maps each Gemini finishReason to the right Anthropic stop_reason", () => {
    expect(mapFinishReason("STOP")).toBe("end_turn");
    expect(mapFinishReason("MAX_TOKENS")).toBe("max_tokens");
    expect(mapFinishReason("SAFETY")).toBe("end_turn");
    expect(mapFinishReason("RECITATION")).toBe("end_turn");
    expect(mapFinishReason("OTHER")).toBe("end_turn");
  });

  it("defaults unknown/missing reasons to end_turn", () => {
    expect(mapFinishReason("SOMETHING_NEW")).toBe("end_turn");
    expect(mapFinishReason(null)).toBe("end_turn");
    expect(mapFinishReason(undefined)).toBe("end_turn");
  });
});

// --- Cost: tokens x Gemini price table ---------------------------------------

describe("Gemini cost", () => {
  it("prices gemini-2.5-flash-lite usage from the central table", () => {
    // gemini-2.5-flash-lite: $0.10 / 1M input, $0.40 / 1M output.
    // 1000 in -> 0.0001, 500 out -> 0.0002 => 0.0003
    expect(estimateCostUsd("gemini-2.5-flash-lite", 1000, 500)).toBeCloseTo(0.0003, 8);
  });

  it("prices gemini-2.5-pro usage from the central table", () => {
    // gemini-2.5-pro: $1.25 / 1M input, $10.00 / 1M output.
    expect(estimateCostUsd("gemini-2.5-pro", 1_000_000, 1_000_000)).toBeCloseTo(11.25, 6);
  });
});

// --- Registry / dispatch -----------------------------------------------------

describe("providerForModel (with Gemini)", () => {
  it("routes gemini-* models to google", () => {
    expect(providerForModel("gemini-2.5-flash-lite")).toBe("google");
    expect(providerForModel("gemini-2.5-pro")).toBe("google");
    expect(providerForModel("gemini-1.5-flash")).toBe("google");
  });

  it("still routes claude/gpt/unknown as before", () => {
    expect(providerForModel("claude-opus-4-8")).toBe("anthropic");
    expect(providerForModel("gpt-4o-mini")).toBe("openai");
    expect(providerForModel("o3-mini")).toBe("openai");
    expect(providerForModel("mystery-model-9000")).toBe("anthropic");
    expect(providerForModel(null)).toBe("anthropic");
  });
});

describe("getAdapter (with Gemini)", () => {
  it("returns the Gemini adapter for google", () => {
    expect(getAdapter("google")?.provider).toBe("google");
  });

  it("leaves openai and anthropic dispatch unchanged", () => {
    expect(getAdapter("openai")?.provider).toBe("openai");
    expect(getAdapter("anthropic")).toBeNull();
  });
});
