"""Model-stats pipeline: read requests -> per-model summary (read only)."""

from __future__ import annotations

from kedro.pipeline import Pipeline, node, pipeline

from .nodes import rank_models_by_cost, summarize_by_model


def create_pipeline(**kwargs) -> Pipeline:
    return pipeline(
        [
            node(
                func=summarize_by_model,
                inputs="requests_raw",
                outputs="model_summary",  # kept in memory; nothing is written to the DB
                name="summarize_by_model",
            ),
            node(
                func=rank_models_by_cost,
                inputs="requests_raw",
                outputs="model_cost_ranking",  # persisted as a CSV file artifact
                name="rank_models_by_cost",
            ),
        ]
    )
