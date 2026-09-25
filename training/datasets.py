"""Prepare a fine-tuning dataset from git history (stdlib + git only).

Runs on any machine with `git`; no ML deps required. Turns the actual commits
of the repos cloned by the code agents into instruction/fix samples QLoRA
fine-tuning can use, so the smol model family learns your stack's real
patterns.

Usage (after cloning the repos the agents build):

    python3 training/datasets.py --repos repos/rust-app repos/go-app \
        --out /data/train.jsonl --valid /data/valid.jsonl --max-samples 2000

Feed the outputs to `training/finetune.py`.
"""

from __future__ import annotations

import argparse
import json
import random
import subprocess
from pathlib import Path
from typing import Any


def commit_diffs(repo: Path, max_commits: int = 200) -> list[dict[str, Any]]:
    """Yield per-commit ``{subject, message, diffs: [{path, hunks: [...]}]}``."""
    cmd = ["git", "log", "-p", "--format=%x01%s%x02%b%x03", f"-{max_commits}", "HEAD"]
    if not Path(repo / ".git").exists():
        raise ValueError(f"{repo} is not a git repository")
    proc = subprocess.run(cmd, cwd=repo, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"git log failed in {repo}: {proc.stderr[:300]}")

    commits: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in proc.stdout.splitlines():
        if line.startswith("\x01"):
            if current:
                commits.append(current)
            current = {"subject": line[1:], "message": "", "diffs": []}
        elif line.startswith("\x02"):
            current["message"] = line[1:]
        elif line.startswith("\x03"):
            continue
        elif line.startswith("+++ b/"):
            path = line.split("b/", 1)[1].strip()
            current["diffs"].append({"path": path, "hunks": []})
        elif line.startswith("@@") and current:
            if current["diffs"]:
                current["diffs"][-1]["hunks"].append({"old": [], "new": [], "context_ok": False})
        elif (
            line.startswith(("+", "-", " "))
            and current
            and current["diffs"]
            and current["diffs"][-1]["hunks"]
        ):
            hunk = current["diffs"][-1]["hunks"][-1]
            hunk["old"].append(line[1:]) if line.startswith("-") else (
                hunk["new"].append(line[1:]) if line.startswith("+") else None
            )
    if current:
        commits.append(current)
    return commits


def commits_to_samples(commits: list[dict[str, Any]], max_samples: int) -> list[dict[str, Any]]:
    """Turn commit hunks into ``{instruction, changed_lines, context}`` samples.

    Only hunks mixing additions+deletions are kept — those resemble a "fix
    this" edit the model should learn to produce.
    """
    samples: list[dict[str, Any]] = []
    for commit in commits:
        for diff in commit["diffs"]:
            for hunk in diff["hunks"]:
                if not hunk["old"] or not hunk["new"]:
                    continue
                samples.append(
                    {
                        "path": diff["path"],
                        "subject": commit["subject"],
                        "message": commit["message"],
                        "removed": hunk["old"],
                        "added": hunk["new"],
                    }
                )
                if len(samples) >= max_samples:
                    return samples
    return samples


def format_sample(sample: dict[str, Any]) -> tuple[str, str]:
    """Return ``(instruction, target)`` for a JSONL training row."""
    instruction = (
        f"In {sample['path']}, change the following code.\n"
        "Current code:\n```\n" + "\n".join(sample["removed"]) + "\n```"
    )
    target = "\n".join(sample["added"])
    return instruction, target


def write_split(samples: list[dict[str, Any]], path: Path) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for sample in samples:
            instruction, target = format_sample(sample)
            row = {
                "path": sample["path"],
                "subject": sample["subject"],
                "prompt": instruction,
                "completion": target,
            }
            fh.write(json.dumps(row) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build code-fix training data from git history")
    parser.add_argument("--repos", nargs="+", required=True, help="paths to git repos to scan")
    parser.add_argument("--out", required=True, type=Path, help="train jsonl path")
    parser.add_argument("--valid", type=Path, help="valid jsonl path (default: <out>.valid.jsonl)")
    parser.add_argument("--max-samples", type=int, default=2000, help="total samples (train+valid)")
    parser.add_argument("--valid-fraction", type=float, default=0.1)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    samples: list[dict[str, Any]] = []
    for repo in args.repos:
        print(f"scanning {repo}…")
        commits = commit_diffs(Path(repo))
        samples.extend(commits_to_samples(commits, args.max_samples))
        print(
            f"  {len(commits)} commits, {sum(1 for d in commits for f in d['diffs'] for h in f['hunks'] if h['old'] and h['new'])} editable hunks"
        )

    rng = random.Random(args.seed)
    rng.shuffle(samples)
    if args.valid is None:
        args.valid = Path(str(args.out) + ".valid.jsonl")
    split = int(len(samples) * (1 - args.valid_fraction))
    write_split(samples[:split], args.out)
    write_split(samples[split:], args.valid)
    print(f"train={split} valid={len(samples) - split} -> {args.out}, {args.valid}")


if __name__ == "__main__":
    main()
