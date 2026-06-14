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

const DEFAULT_PRICES: Record<string, ModelPrice> = {
  "claude-opus-4-8": { inputPerMTok: 15, outputPerMTok: 75 },
  "claude-sonnet-4-6": { inputPerMTok: 3, outputPerMTok: 15 },
  "claude-haiku-4-5-20251001": { inputPerMTok: 1, outputPerMTok: 5 },
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
