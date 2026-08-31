# colab/train_judge.ipynb

Trains a LoRA judge (Qwen2.5-0.5B-Instruct) on slice's routed traffic to retire
the rented Haiku routing judge. Runs top to bottom on a free Colab T4.

Upload at runtime, never committed: `judge_train.jsonl` and `judge_eval.jsonl`
from `scripts/prepare_judge_data.py`, plus `judge_eval_fresh.jsonl` from
`scripts/generate_fresh_eval.py` for the section 9 fresh eval (reported number).

Outputs `judge_lora.zip` (adapter) and `judge_merged.zip` (merged model for
TensorRT-LLM). See `RESULTS.md` for the latest run's metrics.
