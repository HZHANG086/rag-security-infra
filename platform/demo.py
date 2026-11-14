"""Run the four Argo release scenarios and save their cluster evidence."""

import argparse
import json
import secrets
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from rag_infra.cluster import Kubectl, require
from rag_infra.platform import BASELINE, CANDIDATE, SCENARIOS, response_version
from rag_infra.registry import RELEASE, REPORT, Registry

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
    active_ref = result["registry"]["champion"]
    require(samples[-1]["valid_answer"] and samples[-1]["version"] == response_version(active_ref),
            "live endpoint must serve the reported version")
    require(evaluation["baseline"]["registry"]["version"] == "1"
            and evaluation["candidate"]["registry"]["version"] == "2",
            "expected two distinct registered versions")
    require(active_ref == evaluation["candidate" if scenario == "promotion" else "baseline"]["registry"]
            and result["registry"]["candidate"] == evaluation["candidate"]["registry"],
            "registry aliases do not match release result")
    candidate = evaluation["candidate"]
    if scenario == "unschedulable":
        require(any(isinstance(item, dict) and item.get("type") == "scheduling_failed"
                    for item in candidate["infrastructure_findings"]), "missing scheduler reason")
    else:
        require(candidate["shards"] == [0, 1, 2, 3], "candidate shards missing")
    if scenario == "regression":
        require(candidate["metrics"]["attack_success"]["count"] == 1, "expected one injected response")
    if scenario == "rollback":
        require(any(s["status"] == 503 and s["version"] == response_version(candidate["registry"])
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
        pods = directory / "pods.json"
        if pods.exists():
            for pod in json.loads(pods.read_text())["items"]:
                pod_name = pod["metadata"]["name"]
                if pod.get("status", {}).get("phase") not in {"Succeeded", "Failed"}:
                    continue
                try:
                    (directory / f"{pod_name}.log").write_text(k.run("logs", pod_name, "--all-containers=true"))
                except (RuntimeError, subprocess.TimeoutExpired):
                    pass
    require(completed == "Succeeded" and deployment and evaluation and result,
            f"{scenario}: workflow ended with {completed} or missing evidence, see {directory}")
    validate(scenario, result, evaluation)
    print(f"{scenario}: {result['gate']} -> {result['action']}", flush=True)
    return result


def verify_registry(uri, output, reports):
    for scenario, result in reports.items():
        directory = output / scenario
        evaluation = json.loads((directory / "evaluation-result.json").read_text())
        refs = {report["model"]: report["registry"] for report in evaluation.values()}
        registry = Registry(uri, refs[BASELINE]["name"])
        kubernetes = json.loads((directory / "inferenceservices.json").read_text())
        deployed = {item["metadata"]["name"]: item["metadata"]["annotations"]["rag-infra/registered-version"]
                    for item in kubernetes["items"]}
        require(deployed == {name: ref["version"] for name, ref in refs.items()}, "deployed registry versions mismatch")
        aliases = {alias: registry.alias(alias) for alias in ("champion", "candidate")}
        require(aliases == result["registry"], "MLflow aliases do not match the release report")
        runs = {}
        for report in evaluation.values():
            ref = report["registry"]
            run = registry.get_run(ref["run_id"])
            tags = {tag["key"]: tag["value"] for tag in run["data"]["tags"]}
            require(run["info"]["status"] == "FINISHED" and tags["model_version"] == ref["version"]
                    and tags["evaluation_status"] == report["status"] and tags["dataset_sha256"] == report["dataset_sha256"]
                    and tags["prompt_version"] == report["prompt"], "MLflow run tags do not match evaluation")
            metrics = {metric["key"]: metric["value"] for metric in run["data"]["metrics"]}
            require(metrics["completion_rate"] == report["metrics"]["completion_rate"],
                    "MLflow completion metric does not match evaluation")
            require(registry.artifact(run, REPORT) == report and registry.artifact(run, RELEASE) == result,
                    "MLflow artifacts do not match the workflow reports")
            require(registry.manifest(ref)["metadata"]["name"] == report["model"], "deployment artifact mismatch")
            runs[ref["version"]] = {"run_id": ref["run_id"], "tags": tags, "metrics": metrics}
        (directory / "mlflow-verification.json").write_text(
            json.dumps({"aliases": aliases, "runs": runs, "deployed": deployed}, indent=2) + "\n")
    print(f"Verified registry aliases, deployments, eight runs and report artifacts across {len(reports)} scenarios.",
          flush=True)


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
    parser.add_argument("--mlflow-uri", default="http://127.0.0.1:5000")
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
    verify_registry(args.mlflow_uri, args.output, report)


if __name__ == "__main__":
    main()
