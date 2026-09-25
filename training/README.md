# Fine-tuning scaffold

The smol code agents are meant to be *trained*, not just prompted — this
scaffold gives you a GPU-hosted QLoRA pipeline that produces LoRA adapters for
the model family the agents run, so the family progressively learns the actual
patterns, commit styles, and deps of the repos enrolled in Projects.

It lives **outside** the trove venv on purpose: training needs CUDA, its own
torch/transformers stack, and a machine trove itself doesn't run on. Keeping
it self-contained (own `requirements.txt`) means the trove container stays
slim.

## Flow

```
        enrolled repos (from your git server)
                     │  git log -p
                     ▼
          training/datasets.py ──▶ train.jsonl + valid.jsonl
                     │  instruction/completion records
                     ▼
          training/finetune.py ──▶ LoRA adapter (qwen2.5-coder-family)
                     │  llama-quantize
                     ▼
                  GGUF ──▶ ollama create <family-slug>
                     │
                     ▼
        collab on ray / fleet / jarvis (DOCKWATCH_CODE_AGENTS)
```

Why this shape:

- **Dataset = your real history.** `datasets.py` parses `git log -p` of the
  repos the agents build and turns removal/addition hunks into
  "here's flawed code, make it correct" samples. No synthetic data, no
  licensing worries — it's your own stack.
- **Family grows, not one big model.** Fine-tune the same base
  (e.g. `Qwen/Qwen2.5-Coder-1.5B`) once per language or once per team, then
  ship several small adapters. Ollama on each host serves whichever adapter
  matches that host's language.
- **Repos stay unset until enrolled.** `datasets.py` is pointed at the same
  clones the code-agent workers make, so training data tracks the enrolled
  projects automatically.

## Step 1 — build the dataset (any machine with git)

```bash
git clone <your-git-server>/rust-app repos/rust-app
git clone <your-git-server>/go-app    repos/go-app

python3 training/datasets.py \
  --repos repos/rust-app repos/go-app \
  --out /data/train.jsonl --valid /data/valid.jsonl --max-samples 3000
```

## Step 2 — QLoRA fine-tune (GPU box)

```bash
python3 -m venv /opt/train-venv && /opt/train-venv/bin/pip install -r training/requirements.txt

/opt/train-venv/bin/python training/finetune.py \
  --model Qwen/Qwen2.5-Coder-1.5B \
  --train /data/train.jsonl --valid /data/valid.jsonl \
  --output /data/smol-rust-lora \
  --epochs 2 --lora-r 32 --lora-alpha 64
```

Watch it on W&B with `--wandb`; `--merge` also writes merged full-precision
weights if you'd rather quantize than serve the adapter stack.

## Step 3 — serve the family on the nodes

```bash
# on the GPU box or node (llama.cpp installed):
llama-quantize --allow-dirty /data/smol-rust-lora/merged /data/smol-rust-q4_k_m.gguf q4_k_m

# on each node that should serve it:
ollama create smol-rust -f <Modelfile with FROM /data/smol-rust-q4_k_m.gguf>
```

Then set that model id in `DOCKWATCH_CODE_AGENTS` (or the node's
`CODE_AGENT_MODEL`) and the agentic fix loop uses the fine-tuned family.

## Re-training cadence

Re-run `datasets.py` whenever enrolled projects churn (its worker clones are a
free up-to-date mirror), fine-tune weekly or on demand, and `ollama cp` the new
tag into place — trove keeps probing the node's `/api/tags`, so the Pipelines
tab reflects the current family tag without a restart.
