/**
 * Per-model price table — the ONE place model costs live.
 *
 * Prices are USD per 1,000,000 tokens. Edit this constant (or override the whole
 * table via the MODEL_PRICES env var as JSON) when Anthropic pricing changes.
 * `estimateCostUsd` turns token usage into the dollar amount the budget engine
 * tracks — but the engine itself knows nothing about tokens or models, so other
 * cost sources can be priced however they like.
 */
export interface ModelPrice {
  inputPerMTok: number;
  outputPerMTok: number;
}

// Anthropic — unchanged from earlier phases.
const ANTHROPIC_PRICES: Record<string, ModelPrice> = {
  "claude-opus-4-8": { inputPerMTok: 15, outputPerMTok: 75 },
  "claude-sonnet-4-6": { inputPerMTok: 3, outputPerMTok: 15 },
  "claude-haiku-4-5-20251001": { inputPerMTok: 1, outputPerMTok: 5 },
};

// Phase 8 — OpenAI. Safe defaults (USD per 1M tokens); override the whole table
// via the MODEL_PRICES env var. Gemini/Grok tables drop in below the same way.
const OPENAI_PRICES: Record<string, ModelPrice> = {
  "gpt-4o": { inputPerMTok: 2.5, outputPerMTok: 10 },
  "gpt-4o-mini": { inputPerMTok: 0.15, outputPerMTok: 0.6 },
  "gpt-4.1": { inputPerMTok: 2, outputPerMTok: 8 },
  "gpt-4.1-mini": { inputPerMTok: 0.4, outputPerMTok: 1.6 },
  "gpt-4.1-nano": { inputPerMTok: 0.1, outputPerMTok: 0.4 },
  o1: { inputPerMTok: 15, outputPerMTok: 60 },
  "o3-mini": { inputPerMTok: 1.1, outputPerMTok: 4.4 },
  "o4-mini": { inputPerMTok: 1.1, outputPerMTok: 4.4 },
};

// Phase 8 — Google Gemini (developer API). Safe defaults (USD per 1M tokens);
// override the whole table via MODEL_PRICES. Same per-provider spread pattern.
const GOOGLE_PRICES: Record<string, ModelPrice> = {
  "gemini-2.5-pro": { inputPerMTok: 1.25, outputPerMTok: 10 },
  "gemini-2.5-flash": { inputPerMTok: 0.3, outputPerMTok: 2.5 },
  "gemini-2.5-flash-lite": { inputPerMTok: 0.1, outputPerMTok: 0.4 },
  "gemini-2.0-flash": { inputPerMTok: 0.1, outputPerMTok: 0.4 },
  "gemini-2.0-flash-lite": { inputPerMTok: 0.075, outputPerMTok: 0.3 },
  "gemini-1.5-flash": { inputPerMTok: 0.075, outputPerMTok: 0.3 },
  "gemini-1.5-pro": { inputPerMTok: 1.25, outputPerMTok: 5 },
};

// One merged table — the single lookup the budget + ranking already read. Adding
// a provider is just another spread here; no caller changes.
const DEFAULT_PRICES: Record<string, ModelPrice> = {
  ...ANTHROPIC_PRICES,
  ...OPENAI_PRICES,
  ...GOOGLE_PRICES,
};

// Fallback for any model not in the table — priced as a mid-tier model.
const FALLBACK_PRICE: ModelPrice = { inputPerMTok: 3, outputPerMTok: 15 };

/** Optional full-table override from env (JSON). Falls back to the constants. */
function loadPrices(): Record<string, ModelPrice> {
  const raw = process.env.MODEL_PRICES;
  if (!raw) return DEFAULT_PRICES;
  try {
    return { ...DEFAULT_PRICES, ...(JSON.parse(raw) as Record<string, ModelPrice>) };
  } catch {
    return DEFAULT_PRICES;
  }
}

const PRICES = loadPrices();

export function priceFor(model: string | null): ModelPrice {
  return (model && PRICES[model]) || FALLBACK_PRICE;
}

/** Dollar cost of one call given its token usage (nulls treated as 0). */
export function estimateCostUsd(
  model: string | null,
  inputTokens: number | null,
  outputTokens: number | null,
): number {
  const price = priceFor(model);
  const input = ((inputTokens ?? 0) / 1_000_000) * price.inputPerMTok;
  const output = ((outputTokens ?? 0) / 1_000_000) * price.outputPerMTok;
  return input + output;
}
