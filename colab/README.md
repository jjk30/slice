# colab/train_judge.ipynb

Trains a LoRA judge (Qwen2.5-0.5B-Instruct) on slice's routed traffic. This
judge now runs the auto path in production, with the rented Haiku judge as the
fallback. Runs top to bottom on a free Colab T4.

Upload at runtime, never committed: `judge_train.jsonl` and `judge_eval.jsonl`
from `scripts/prepare_judge_data.py`, plus `judge_eval_fresh.jsonl` from
`scripts/generate_fresh_eval.py` for the section 9 fresh eval (reported number).

Outputs `judge_lora.zip` (adapter) and `judge_merged.zip` (the merged model),
plus `judge_fresh_predictions.jsonl` from section 9 for the RAGAS comparison
step. See `RESULTS.md` for the latest run's metrics.

## From notebook to production

The merged model becomes the deployed judge without retraining. Take the merged
weights, run `convert_hf_to_gguf` to get a GGUF file, then `llama-quantize` to
Q4_K_M for a 379 MB file, upload it to a private versioned S3 bucket, and the
box fetches it at boot. There a llama.cpp server serves it as the `judge`
compose service next to the gateway. See the "Serving in production" section of
`RESULTS.md` for the box, memory, latency, and fallback details.
