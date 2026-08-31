# Judge LoRA training results

Numbers below are from the executed Colab run on a free T4 (the source notebook
exported from Colab). They are transcribed from that run's cell outputs.

Important: the repo notebook was ported from that run (install fix folded in,
outputs stripped, fresh eval section added). It has not yet been re-run top to
bottom after porting. Re-run it to regenerate these numbers before trusting them
as reproducible.

## Model and LoRA settings

- Base model: `Qwen/Qwen2.5-0.5B-Instruct`, loaded fp16 (T4 is Turing, no bf16)
- LoRA: r 16, alpha 32, dropout 0.05, target modules q_proj, k_proj, v_proj, o_proj
- Trainable params: 2,162,688 of 496,195,456 (0.4359%)
- Gradient checkpointing on, seed 42 everywhere
- 3 epochs, per-device batch 8 x grad accumulation 2 (effective 16)
- LR 2e-4, cosine schedule, warmup_steps 4, optimizer adamw_torch
- Training: 123 steps, final train loss 0.198, runtime about 75 s (26.2 samples/s)

## Data sizes

- Train: 654 rows
- Shared-template eval (`judge_eval.jsonl`): 74 rows
- Fresh eval (`judge_eval_fresh.jsonl`): 100 rows (54 easy, 46 hard)

## Shared-template eval (judge_eval.jsonl)

Scored with the LoRA adapter on top of the base model.

- Overall accuracy: 86.5% (64/74), majority baseline 58.1%
- Per label: easy 81.4% (35/43), hard 93.5% (29/31)
- Confusion (true -> pred): easy->easy 35, easy->hard 8, hard->easy 2, hard->hard 29
- Where template_category agrees with label: 92.1% (58/63)
- Where template_category disagrees with label: 54.5% (6/11)

## Fresh eval, unseen templates (judge_eval_fresh.jsonl)

Scored with the merged model. This is the reported generalization number.

- Overall accuracy: 92.0% (92/100), majority baseline 54.0%
- Per label: easy 94.4% (51/54), hard 89.1% (41/46)
- Confusion (true -> pred): easy->easy 51, easy->hard 3, hard->easy 5, hard->hard 41
- Where template_category agrees with label: 94.8% (91/96)
- Where template_category disagrees with label: 25.0% (1/4)

## Latency

Single-decision latency on the T4 over the 74 shared-eval rows. This is the
before number for the later TensorRT-LLM conversion.

- Mean: 133.3 ms
- p95: 173.5 ms

## Run notes: three live fixes applied during the Colab run

1. Uninstall Colab's preinstalled torchao 0.10 after the pip install; it trips
   peft's import check on a fresh runtime.
2. Every `apply_chat_template(..., tokenize=True)` call now passes
   `return_dict=False`, because transformers 5 returns a dict by default.
3. `warmup_ratio=0.03` replaced with `warmup_steps=4`.
