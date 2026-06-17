"""Phase 6, step two — basic per-model stats over the gateway's request logs.

READ ONLY. This node receives the `requests` table as a pandas DataFrame and
computes a small per-model summary. It never writes to the database.
"""

from __future__ import annotations

import pandas as pd

# Real columns in the gateway's `requests` table (see gateway/db/init/01_schema.sql
# plus the Phase 3/4 migration in gateway/src/db.ts). We group by `routed_model`,
# the model slice actually used, which is also what the gateway's stats API groups
# spend by. `cost_usd` is numeric (nullable), `latency_ms` is an int.
MODEL_COL = "routed_model"


def summarize_by_model(requests: pd.DataFrame) -> pd.DataFrame:
    """Per-model summary: request count, total cost, avg cost, avg latency.

    Args:
        requests: the full `requests` table as a DataFrame.

    Returns:
        One row per model with the summary columns, sorted by total cost.
    """
    total_rows = len(requests)
    print(f"\n[model_stats] total rows read from `requests`: {total_rows}")

    if total_rows == 0:
        print(
            "[model_stats] the requests table is EMPTY. No real data to summarize. "
            "Send some traffic through the gateway and re-run."
        )
        return pd.DataFrame(
            columns=["model", "requests", "total_cost_usd", "avg_cost_usd", "avg_latency_ms"]
        )

    if total_rows < 10:
        print(f"[model_stats] note: only {total_rows} row(s) present — this is low-volume real data.")

    df = requests.copy()
    # cost_usd is NUMERIC and may be NULL on some rows; coerce to float so sums and
    # means ignore missing values instead of erroring.
    df["cost_usd"] = pd.to_numeric(df["cost_usd"], errors="coerce")
    df["latency_ms"] = pd.to_numeric(df["latency_ms"], errors="coerce")
    df["model"] = df[MODEL_COL].fillna("(unknown)")

    summary = (
        df.groupby("model")
        .agg(
            requests=("id", "count"),
            total_cost_usd=("cost_usd", "sum"),
            avg_cost_usd=("cost_usd", "mean"),
            avg_latency_ms=("latency_ms", "mean"),
        )
        .reset_index()
        .sort_values("total_cost_usd", ascending=False, ignore_index=True)
    )

    # Show a few real rows so the data shape is visible.
    sample_cols = ["id", "routed_model", "requested_model", "status", "latency_ms", "cost_usd", "cache_hit", "created_at"]
    sample_cols = [c for c in sample_cols if c in df.columns]
    print("\n[model_stats] sample rows (first 5):")
    print(df[sample_cols].head().to_string(index=False))

    print("\n[model_stats] per-model summary:")
    print(summary.to_string(index=False))
    print()

    return summary


# Ranking columns, in output order. `rank` 1 = cheapest by avg cost per call.
RANKING_COLUMNS = ["rank", "model", "calls", "total_cost_usd", "avg_cost_usd", "avg_latency_ms"]


def rank_models_by_cost(requests: pd.DataFrame) -> pd.DataFrame:
    """Cost ranking per model from billable, successful rows only.

    Honest first pass: ranks by AVERAGE cost per call (cheapest first). Latency is
    kept as an informational column but is NOT the ranking key — at low sample
    sizes it is noisy and dominated by network round-trips. Same filter as the
    batch summary: status == 200 AND cost_usd > 0, which excludes the stale
    pre-cost NULL rows and the non-billable zero-cost rows (cache hits, blocks,
    errors).

    Args:
        requests: the full `requests` table as a DataFrame.

    Returns:
        One row per model with a `rank` column, sorted cheapest-first.
    """
    df = requests.copy()
    df["cost_usd"] = pd.to_numeric(df["cost_usd"], errors="coerce")
    df["latency_ms"] = pd.to_numeric(df["latency_ms"], errors="coerce")
    df["status"] = pd.to_numeric(df["status"], errors="coerce")

    billable = df[(df["status"] == 200) & (df["cost_usd"] > 0)].copy()
    print(f"\n[ranking] billable rows (status=200 AND cost_usd>0): {len(billable)} of {len(df)} total")

    if billable.empty:
        print("[ranking] no billable rows to rank. Send real successful traffic and re-run.")
        return pd.DataFrame(columns=RANKING_COLUMNS)

    billable["model"] = billable[MODEL_COL].fillna("(unknown)")
    ranking = (
        billable.groupby("model")
        .agg(
            calls=("id", "count"),
            total_cost_usd=("cost_usd", "sum"),
            avg_cost_usd=("cost_usd", "mean"),
            avg_latency_ms=("latency_ms", "mean"),  # informational, not the ranking key
        )
        .reset_index()
        .sort_values("avg_cost_usd", ascending=True, ignore_index=True)
    )

    # Round for a clean, readable artifact (still plenty of precision for cost).
    ranking["total_cost_usd"] = ranking["total_cost_usd"].round(6)
    ranking["avg_cost_usd"] = ranking["avg_cost_usd"].round(6)
    ranking["avg_latency_ms"] = ranking["avg_latency_ms"].round(1)

    ranking.insert(0, "rank", range(1, len(ranking) + 1))
    ranking = ranking[RANKING_COLUMNS]

    print("[ranking] cost ranking (cheapest first; avg_latency_ms is informational):")
    print(ranking.to_string(index=False))
    print()

    return ranking
