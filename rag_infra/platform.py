"""Deploy, evaluate and promote versioned RAG fixtures in a disposable cluster."""

import argparse
import json
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from rag_infra.cluster import Kubectl, container, require, run_job
from rag_infra.core import read_suite, release_gate

BASELINE = "rag-stable-v1"
CANDIDATE = "rag-candidate-v2"
FRONT = "rag-live"
PORT = 8080
SCENARIOS = {
    "promotion": "guarded",
    "regression": "regressed",
    "unschedulable": "guarded",
    "rollback": "guarded",
}
PROBE = {"case_id": "release-probe", "question": "release probe", "documents": []}


def predictor(k, name):
    return k.get("service", f"{name}-predictor")


def inferenceservice(name, image, profile, fail_probe=False):
    args = ["--rag-profile", profile, "--model-version", name]
    if fail_probe:
        args.append("--fail-release-probe")
    spec = container(image, "kserve-container", ["python", "-m", "rag_infra.serve"], args)
    spec["ports"] = [{"name": "http1", "containerPort": PORT, "protocol": "TCP"}]
    spec["readinessProbe"] = {"httpGet": {"path": "/readyz", "port": PORT}, "periodSeconds": 2}
    return {
        "apiVersion": "serving.kserve.io/v1beta1",
        "kind": "InferenceService",
        "metadata": {"name": name, "annotations": {"serving.kserve.io/deploymentMode": "Standard"},
                     "labels": {"networking.kserve.io/visibility": "cluster-local"}},
        "spec": {"predictor": {
            "minReplicas": 1, "maxReplicas": 1, "automountServiceAccountToken": False,
            "containers": [spec],
        }},
    }


def deploy(k, image, scenario):
    k.create(inferenceservice(BASELINE, image, "guarded"))
    k.create(inferenceservice(CANDIDATE, image, SCENARIOS[scenario], scenario == "rollback"))
    k.run("wait", "--for=condition=Ready", "inferenceservice", BASELINE, CANDIDATE, "--timeout=115s")
    stable = predictor(k, BASELINE)["spec"]
    candidate = predictor(k, CANDIDATE)["spec"]
    require(stable["selector"] != candidate["selector"], "predictors must use separate selectors")
    require(all(p["ports"][0]["targetPort"] == PORT for p in (stable, candidate)),
            "predictors must listen on the expected container port")
    k.create({
        "apiVersion": "v1", "kind": "Service", "metadata": {"name": FRONT},
        "spec": {"selector": stable["selector"], "ports": [{"port": PORT, "targetPort": PORT}]},
    })
    samples = []
    require(wait_for_version(BASELINE, samples), f"initial stable endpoint failed: {samples}")
    record(k, "deployment-result", {"samples": samples, "initial_version": BASELINE})


def evaluate(k, image, scenario):
    suite = read_suite("/app/fixtures/rag-suite.json")
    output = Path("/tmp/eval")
    output.mkdir(exist_ok=True)
    reports = {}
    for model, name in ((BASELINE, "baseline"), (CANDIDATE, "candidate")):
        service = predictor(k, model)
        profile = SCENARIOS[scenario] if model == CANDIDATE else "guarded"
        reports[name] = run_job(
            k, suite, name, image, service["metadata"]["name"], model, profile, output,
            fault=model == CANDIDATE and scenario == "unschedulable",
            port=service["spec"]["ports"][0]["port"],
        )
    record(k, "evaluation-result", reports)


def record(k, name, report):
    k.create({"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": name},
              "data": {"report.json": json.dumps(report, ensure_ascii=False, indent=2)}})


def evaluate_probe():
    req = Request(f"http://{FRONT}:{PORT}/rag/answer", data=json.dumps(PROBE).encode(),
                  headers={"Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=2) as response:
            body = json.load(response)
            return response.status, response.headers.get("X-Model-Version"), (
                body == {"answer": "INSUFFICIENT_EVIDENCE", "citations": []})
    except HTTPError as exc:
        with exc:
            return exc.code, exc.headers.get("X-Model-Version"), False
    except (URLError, OSError, ValueError):
        return None, None, False


def wait_for_version(version, samples, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status, served_by, valid = evaluate_probe()
        samples.append({"status": status, "version": served_by, "valid_answer": valid})
        if served_by == version:
            return status == 200 and valid
        time.sleep(0.25)
    return False


def switch(k, previous, next_version):
    service = k.get("service", FRONT)
    before = predictor(k, previous)["spec"]["selector"]
    after = predictor(k, next_version)["spec"]["selector"]
    require(service["spec"]["selector"] == before, "live selector changed unexpectedly")
    patch = [
        {"op": "test", "path": "/spec/selector", "value": before},
        {"op": "replace", "path": "/spec/selector", "value": after},
    ]
    k.run("patch", "service", FRONT, "--type=json", "-p", json.dumps(patch))


def promote(k, reports, scenario):
    baseline = reports["baseline"]
    candidate = reports["candidate"]
    require(baseline["model"] == BASELINE and candidate["model"] == CANDIDATE,
            "evaluation reports must identify the deployment versions")
    gate = release_gate(baseline, candidate)
    result = {"scenario": scenario, "gate": gate, "action": "blocked", "active_version": BASELINE,
              "promotion_probes": [], "final_probes": []}
    if gate != "release_allowed":
        require(wait_for_version(BASELINE, result["final_probes"]), "blocked release lost stable traffic")
        return result

    switch(k, BASELINE, CANDIDATE)
    healthy = False
    try:
        healthy = wait_for_version(CANDIDATE, result["promotion_probes"])
    finally:
        if not healthy:
            failed_at = time.monotonic()
            switch(k, CANDIDATE, BASELINE)
            result["action"] = "rolled_back"
            recovered = wait_for_version(BASELINE, result["final_probes"])
            result["recovery_seconds"] = round(time.monotonic() - failed_at, 3)
            result["failed_probe_requests"] = sum(
                not sample["valid_answer"] for sample in result["promotion_probes"] + result["final_probes"])
            require(recovered, f"stable endpoint did not recover: {result}")
    if healthy:
        result.update(action="promoted", active_version=CANDIDATE)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=["deploy", "evaluate", "release"])
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--scenario", required=True, choices=SCENARIOS)
    args = parser.parse_args()
    if not args.namespace.startswith("rag-platform-"):
        parser.error("use a dedicated rag-platform-* namespace")
    k = Kubectl(None, args.namespace)
    if args.stage == "deploy":
        deploy(k, args.image, args.scenario)
    elif args.stage == "evaluate":
        evaluate(k, args.image, args.scenario)
    else:
        reports = json.loads(k.get("configmap", "evaluation-result")["data"]["report.json"])
        result = promote(k, reports, args.scenario)
        record(k, "release-result", result)
        print(json.dumps(result, indent=2), flush=True)


if __name__ == "__main__":
    main()
