"""Run the four Argo release scenarios and save their cluster evidence."""

import argparse
import json
import secrets
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from rag_infra.cluster import Kubectl, require
from rag_infra.platform import BASELINE, CANDIDATE, SCENARIOS

ROOT = Path(__file__).resolve().parent
EXPECTED = {
    "promotion": ("release_allowed", "promoted", CANDIDATE),
    "regression": ("regression", "blocked", BASELINE),
    "unschedulable": ("incomplete", "blocked", BASELINE),
    "rollback": ("release_allowed", "rolled_back", BASELINE),
}


def retrieve(k, kind, name):
    try:
        return k.get(kind, name)
    except (RuntimeError, subprocess.TimeoutExpired):
        return None


def export(k, directory, kind, name):
    if value := retrieve(k, kind, name):
        if "data" in value:
            value = json.loads(value["data"]["report.json"])
        (directory / f"{name}.json").write_text(json.dumps(value, indent=2) + "\n")
        return value


def validate(scenario, result, evaluation):
    expected = EXPECTED[scenario]
    actual = (result["gate"], result["action"], result["active_version"])
    require(actual == expected, f"{scenario}: expected {expected}, got {actual}")
    require(evaluation["baseline"]["shards"] == [0, 1, 2, 3], "baseline shards missing")
    samples = result["final_probes"] or result["promotion_probes"]
    require(samples[-1]["valid_answer"] and samples[-1]["version"] == result["active_version"],
            "live endpoint must serve the reported version")
    candidate = evaluation["candidate"]
    if scenario == "unschedulable":
        require(any(isinstance(item, dict) and item.get("type") == "scheduling_failed"
                    for item in candidate["infrastructure_findings"]), "missing scheduler reason")
    else:
        require(candidate["shards"] == [0, 1, 2, 3], "candidate shards missing")
    if scenario == "regression":
        require(candidate["metrics"]["attack_success"]["count"] == 1, "expected one injected response")
    if scenario == "rollback":
        require(any(s["status"] == 503 and s["version"] == CANDIDATE
                    for s in result["promotion_probes"]), "candidate fault not observed through the live service")
        require(result["failed_probe_requests"] >= 1 and result["recovery_seconds"] >= 0, "missing rollback metrics")


def run(scenario, context, prefix, output):
    directory = output / scenario
    directory.mkdir(parents=True, exist_ok=True)
    k = Kubectl(context, f"{prefix}-{scenario}")
    k.create({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": k.namespace}}, scoped=False)
    k.run("apply", "-f", str(ROOT / "rbac.yaml"))
    manifest = (ROOT / "workflow.yaml").read_text().replace("value: promotion", f"value: {scenario}")
    name = "rag-release"
    k.run("create", "-f", "-", payload=manifest)
    try:
        completed = poll(k, name)
    finally:
        deployment = export(k, directory, "configmap", "deployment-result")
        evaluation = export(k, directory, "configmap", "evaluation-result")
        result = export(k, directory, "configmap", "release-result")
        export(k, directory, "workflow", name)
        for kind in ("inferenceservices", "pods", "events"):
            try:
                (directory / f"{kind}.json").write_text(k.run("get", kind, "-o", "json"))
            except (RuntimeError, subprocess.TimeoutExpired):
                pass
    require(completed == "Succeeded" and deployment and evaluation and result,
            f"{scenario}: workflow ended with {completed} or missing evidence, see {directory}")
    validate(scenario, result, evaluation)
    print(f"{scenario}: {result['gate']} -> {result['action']}", flush=True)
    return result


def poll(k, name):
    deadline = time.monotonic() + 565
    phase = "Pending"
    while time.monotonic() < deadline:
        phase = k.get("workflow", name).get("status", {}).get("phase", "Pending")
        if phase in {"Succeeded", "Failed", "Error"}:
            return phase
        time.sleep(3)
    return phase


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", required=True, choices=["kind-rag-platform"])
    parser.add_argument("--output", type=Path, default=Path("artifacts/platform"))
    args = parser.parse_args()
    prefix = "rag-platform-" + secrets.token_hex(3)
    with ThreadPoolExecutor(max_workers=len(SCENARIOS)) as pool:
        futures = {scenario: pool.submit(run, scenario, args.context, prefix, args.output)
                   for scenario in SCENARIOS}
        report = {}
        failures = {}
        for scenario, future in futures.items():
            try:
                report[scenario] = future.result()
            except (RuntimeError, subprocess.TimeoutExpired) as exc:
                failures[scenario] = str(exc)
    (args.output / "summary.json").write_text(json.dumps({"scenarios": report, "failures": failures}, indent=2) + "\n")
    require(not failures, f"release scenario failures: {failures}")


if __name__ == "__main__":
    main()
