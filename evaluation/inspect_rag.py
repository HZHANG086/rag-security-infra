"""Replay recorded RAG responses in Inspect AI and audit case-level scoring."""

import argparse
import json
from collections import Counter
from pathlib import Path

from inspect_ai import Task, eval as inspect_eval
from inspect_ai.dataset import Sample
from inspect_ai.model import ModelOutput
from inspect_ai.scorer import Score, Target, mean, scorer
from inspect_ai.solver import Generate, TaskState, solver

from rag_infra.alignment import build_cases, render_prompt, score, summarize
from rag_infra.core import canonical_digest

ARM_SOURCES = {
    "test": {"base": ("qwen05b-test", "boundary"),
             "prefix_filter": ("qwen05b-test", "boundary_filter"),
             "lora": ("qwen05b-lora-test", "boundary")},
    "challenge": {"base": ("qwen05b-challenge", "boundary"),
                  "lora": ("qwen05b-lora-challenge", "boundary")},
}


@solver
def recorded_reply():
    async def solve(state: TaskState, generate: Generate):
        state.output = ModelOutput.from_content(state.metadata["model"], state.metadata["reply"])
        return state

    return solve


def outcome(row):
    if row["attack_success"]:
        return "injected"
    if row["grounded_correct"]:
        return "correct_abstention" if row["kind"] == "missing" else "grounded"
    if row["abstained"]:
        return "abstention_format_error" if row["kind"] == "missing" else "false_refusal"
    if row["kind"] == "missing":
        return "answered_without_evidence"
    if row["answer_correct"]:
        return "incorrect_or_missing_citation"
    return "wrong_answer"


@scorer(metrics=[mean()])
def rag_rubric():
    async def grade(state: TaskState, target: Target):
        case = state.metadata["case"]
        if target.text != case["expected"]:
            raise ValueError("Inspect target and benchmark answer disagree")
        row = score(case, state.output.completion)
        return Score(
            value=int(row["grounded_correct"]),
            answer=row["reply"],
            explanation=outcome(row),
            metadata={"benchmark_row": row},
        )

    return grade


def load_arm(root, split, cases, filename, profile):
    report = json.loads((root / f"{filename}.json").read_text(encoding="utf-8"))
    if (report["split"], report["dataset_sha256"], report["case_count"]) != (
        split, canonical_digest(cases), len(cases)
    ):
        raise ValueError(f"{filename}: benchmark identity mismatch")
    result = report["results"][profile]
    rows = {row["id"]: row for row in result["cases"]}
    if len(rows) != len(result["cases"]) or rows.keys() != {case["id"] for case in cases}:
        raise ValueError(f"{filename}: missing, extra or duplicate sample IDs")
    samples = [
        Sample(id=case["id"], target=case["expected"],
               input=render_prompt(case, profile)[-1]["content"],
               metadata={"case": case, "model": report["model"], "reply": rows[case["id"]]["reply"]})
        for case in cases
    ]
    return samples, rows, result["metrics"]


def audit(root, output):
    results = {}
    for split, arms in ARM_SOURCES.items():
        cases = build_cases(split)
        results[split] = {}
        rows_by_arm = {}
        for arm, (filename, profile) in arms.items():
            samples, expected_rows, expected_metrics = load_arm(root, split, cases, filename, profile)
            task = Task(name=f"rag_{split}_{arm}", dataset=samples, solver=recorded_reply(),
                        scorer=rag_rubric(), metadata={"mode": "response_replay", "split": split,
                                                      "arm": arm, "profile": profile,
                                                      "dataset_sha256": canonical_digest(cases)})
            log, = inspect_eval(task, model="mockllm/model", log_dir=str(output / "logs"),
                                display="none", fail_on_error=True, log_samples=True)
            if log.status != "success" or not log.samples or len(log.samples) != len(cases):
                raise RuntimeError(f"Incomplete Inspect evaluation for {split}/{arm}")
            replay = []
            slices = {}
            for sample in log.samples:
                scored, = sample.scores.values()
                row = scored.metadata["benchmark_row"]
                if row != expected_rows[str(sample.id)]:
                    raise ValueError(f"Original and replayed scores disagree for {split}/{arm}/{sample.id}")
                replay.append(row)
                slice_name = row["family"] or row["kind"]
                slices.setdefault(slice_name, Counter())[scored.explanation] += 1
            metrics = summarize(replay)
            if metrics != expected_metrics:
                raise ValueError(f"Aggregate metrics disagree for {split}/{arm}")
            rows_by_arm[arm] = {row["id"]: row for row in replay}
            results[split][arm] = {"sample_count": len(replay), "metrics": metrics, "outcomes_by_slice": slices}
        changes = Counter(
            (outcome(base), outcome(rows_by_arm["lora"][case_id]))
            for case_id, base in rows_by_arm["base"].items()
        )
        results[split]["base_to_lora_transitions"] = [
            {"before": before, "after": after, "n": n} for (before, after), n in sorted(changes.items())
        ]
    output.mkdir(parents=True, exist_ok=True)
    summary = {"mode": "response_replay", "results": results}
    (output / "inspect-summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiments", type=Path, default=Path("experiments"))
    parser.add_argument("--output", type=Path, default=Path("artifacts/inspect-rag"))
    args = parser.parse_args()
    summary = audit(args.experiments, args.output)
    for split, arms in summary["results"].items():
        for arm, record in arms.items():
            if arm != "base_to_lora_transitions":
                print(f"{split}/{arm}: {record['sample_count']} case-level scores matched")


if __name__ == "__main__":
    main()
