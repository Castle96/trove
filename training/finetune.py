"""QLoRA fine-tuning for the code-agent model family (qwen2.5-coder etc.).

Runs on a GPU box with the training requirements installed
(``pip install -r training/requirements.txt``). Consumes the instruction /
completion JSONL written by ``training/datasets.py`` and emits a LoRA adapter
(plus an optionally merged full model) that the Ollama nodes can serve after a
GGUF export + ``ollama create``.

Example:

    python3 training/finetune.py \
        --model Qwen/Qwen2.5-Coder-1.5B \
        --train /data/train.jsonl --valid /data/valid.jsonl \
        --output /data/rust-smol-adapter \
        --epochs 2

Export to GGUF afterwards (on a box with llama.cpp):

    llama-quantize --allow-dirty \
        /data/rust-smol-adapter /data/rust-smol-q4_k_m.gguf q4_k_m

    ollama create rust-smol -f Modelfile-from-gguf
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

PROMPT_TEMPLATE = (
    "You are a {language} coding agent. Given flawed code, produce the smallest "
    "edits that make it correct, preserving behavior and public APIs.\n\n"
    "### Input\n{instruction}\n\n### Output\n{completion}"
)


def load_rows(path: Path) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            rows.append(
                {
                    "language": path.parent.name
                    if path.parent.name in ("rust", "go", "python")
                    else "generic",
                    "instruction": row["prompt"],
                    "completion": row["completion"],
                }
            )
    return rows


def build_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="QLoRA fine-tune the smol code-agent family")
    parser.add_argument(
        "--model",
        default="Qwen/Qwen2.5-Coder-1.5B",
        help="base model id (HuggingFace or local path)",
    )
    parser.add_argument("--train", required=True, type=Path, help="train jsonl (from datasets.py)")
    parser.add_argument("--valid", type=Path, help="valid jsonl")
    parser.add_argument(
        "--output", required=True, type=Path, help="where to write the LoRA adapter"
    )
    parser.add_argument("--epochs", type=int, default=2)
    parser.add_argument("--batch-size", type=int, default=2)
    parser.add_argument("--grad-accum", type=int, default=8)
    parser.add_argument("--learning-rate", type=float, default=2e-4)
    parser.add_argument("--max-seq-len", type=int, default=2048)
    parser.add_argument("--lora-r", type=int, default=32)
    parser.add_argument("--lora-alpha", type=int, default=64)
    parser.add_argument("--save-steps", type=int, default=250)
    parser.add_argument(
        "--wandb", action="store_true", default=False, help="log to Weights & Biases"
    )
    parser.add_argument(
        "--merge",
        action="store_true",
        default=False,
        help="also write merged full-precision weights",
    )
    parser.add_argument("--trust-remote-code", action="store_true", default=False)
    return parser.parse_args()


def main() -> None:
    args = build_args()

    import torch
    from datasets import Dataset as HFDataset
    from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training
    from transformers import (
        AutoModelForCausalLM,
        AutoTokenizer,
        BitsAndBytesConfig,
        Trainer,
        TrainingArguments,
    )

    train_rows = load_rows(args.train)
    if not train_rows:
        raise SystemExit("no training rows; run training/datasets.py first")
    print(f"loaded {len(train_rows)} training rows")

    tokenizer = AutoTokenizer.from_pretrained(
        args.model, trust_remote_code=args.trust_remote_code, padding_side="right"
    )
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    def tokenize(row: dict[str, str]) -> dict[str, list[int]]:
        text = PROMPT_TEMPLATE.format(**row)
        out = tokenizer(
            text,
            truncation=True,
            max_length=args.max_seq_len,
            padding="max_length",
        )
        out["labels"] = out["input_ids"].copy()
        return out

    hf_train = HFDataset.from_list(train_rows).map(
        tokenize, remove_columns=["instruction", "completion", "language"]
    )
    hf_valid = HFDataset.from_list(load_rows(args.valid)) if args.valid else None
    if hf_valid is not None:
        hf_valid = hf_valid.map(tokenize, remove_columns=["instruction", "completion", "language"])

    quant_config = BitsAndBytesConfig(
        load_in_4bit=True, bnb_4bit_quant_type="nf4", bnb_4bit_use_double_quant=True
    )
    model = AutoModelForCausalLM.from_pretrained(
        args.model,
        quantization_config=quant_config,
        device_map="auto",
        trust_remote_code=args.trust_remote_code,
        torch_dtype=torch.bfloat16,
    )
    model = prepare_model_for_kbit_training(model, use_gradient_checkpointing=True)
    model.config.use_cache = False

    model = get_peft_model(
        model,
        LoraConfig(
            r=args.lora_r,
            lora_alpha=args.lora_alpha,
            lora_dropout=0.05,
            bias="none",
            task_type="CAUSAL_LM",
            target_modules=[
                "q_proj",
                "k_proj",
                "v_proj",
                "o_proj",
                "gate_proj",
                "up_proj",
                "down_proj",
            ],
        ),
    )
    model.print_trainable_parameters()

    training_args = TrainingArguments(
        output_dir=str(args.output),
        num_train_epochs=args.epochs,
        per_device_train_batch_size=args.batch_size,
        per_device_eval_batch_size=args.batch_size,
        gradient_accumulation_steps=args.grad_accum,
        learning_rate=args.learning_rate,
        warmup_ratio=0.03,
        lr_scheduler_type="cosine",
        logging_steps=10,
        save_steps=args.save_steps,
        save_total_limit=2,
        evaluation_strategy="steps" if hf_valid is not None else "no",
        eval_steps=args.save_steps,
        bf16=True,
        report_to="wandb" if args.wandb else "none",
        gradient_checkpointing=True,
        optim="paged_adamw_8bit",
    )
    trainer = Trainer(
        model=model,
        args=training_args,
        train_dataset=hf_train,
        eval_dataset=hf_valid,
        tokenizer=tokenizer,
    )
    trainer.train()
    trainer.save_model(str(args.output))

    if args.merge:
        merged = model.merge_and_unload()
        merged.save_pretrained(str(args.output / "merged"))
        tokenizer.save_pretrained(str(args.output / "merged"))

    print(f"\nLoRA adapter saved to {args.output}")
    print("Next steps (on a GPU box):")
    print(f"  1. llama-quantize '{args.output}' '{args.output}/model-q4_k_m.gguf' q4_k_m")
    print("  2. ollama create <family-slug> -f Modelfile-from-gguf")


if __name__ == "__main__":
    main()
