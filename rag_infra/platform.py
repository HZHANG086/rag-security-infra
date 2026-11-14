"""Deploy, evaluate and promote versioned RAG fixtures in a disposable cluster."""

import argparse
import json
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from rag_infra.cluster import Kubectl, container, require, run_job
from rag_infra.core import read_suite, release_gate
from rag_infra.registry import REPORT, Registry

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
MLFLOW_URI = "http://mlflow.mlflow.svc.cluster.local:5000"


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


def register(registry, image, scenario):
    experiment_id = registry.create()
    refs = {}
    for alias, manifest in (
        ("champion", inferenceservice(BASELINE, image, "guarded")),
        ("candidate", inferenceservice(CANDIDATE, image, SCENARIOS[scenario], scenario == "rollback")),
    ):
        version = registry.register(experiment_id, manifest, alias)
        registry.assign(alias, version)
        refs[alias] = registry.alias(alias)
        require(refs[alias] == version, f"registry resolved the wrong {alias}")
    return {ref["deployment"]: ref for ref in refs.values()}


def response_version(ref):
    return f"{ref['deployment']}@{ref['version']}"


def deploy(k, registry, image, scenario):
    refs = register(registry, image, scenario)
    for ref in refs.values():
        manifest = registry.manifest(ref)
        metadata = manifest["metadata"]
        metadata["annotations"]["rag-infra/registered-version"] = ref["version"]
        args = manifest["spec"]["predictor"]["containers"][0]["args"]
        args[args.index("--model-version") + 1] = response_version(ref)
        k.create(manifest)
    deadline = time.monotonic() + 115
    for name in (BASELINE, CANDIDATE):
        while True:
            service = k.get("inferenceservice", name)
            conditions = service.get("status", {}).get("conditions") or []
            if any(c["type"] == "Ready" and c["status"] == "True" for c in conditions):
                break
            require(time.monotonic() < deadline, f"{name} did not become Ready: {conditions}")
            time.sleep(2)
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
    require(wait_for_version(response_version(refs[BASELINE]), samples), f"initial endpoint failed: {samples}")
    record(k, "deployment-result", {"samples": samples, "initial_version": BASELINE, "registry": refs})


def evaluate(k, registry, refs, image, scenario):
    suite = read_suite("/app/fixtures/rag-suite.json")
    output = Path("/tmp/eval")
    output.mkdir(exist_ok=True)
    reports = {}
    for model, name in ((BASELINE, "baseline"), (CANDIDATE, "candidate")):
        ref = refs[model]
        service = predictor(k, model)
        profile = SCENARIOS[scenario] if model == CANDIDATE else "guarded"
        reports[name] = run_job(
            k, suite, name, image, service["metadata"]["name"], model, profile, output,
            fault=model == CANDIDATE and scenario == "unschedulable",
            port=service["spec"]["ports"][0]["port"],
            expected_version=response_version(ref),
        )
        reports[name]["registry"] = ref
        registry.evaluation(ref, reports[name])
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


def promote(k, reports, scenario, *, refs=None, on_promoted=None):
    baseline = reports["baseline"]
    candidate = reports["candidate"]
    require(baseline["model"] == BASELINE and candidate["model"] == CANDIDATE,
            "evaluation reports must identify the deployment versions")
    gate = release_gate(baseline, candidate)
    result = {"scenario": scenario, "gate": gate, "action": "blocked", "active_version": BASELINE,
              "promotion_probes": [], "final_probes": []}
    stable, next_version = (BASELINE, CANDIDATE) if refs is None else (
        response_version(refs[BASELINE]), response_version(refs[CANDIDATE]))
    if gate != "release_allowed":
        require(wait_for_version(stable, result["final_probes"]), "blocked release lost stable traffic")
        return result

    switch(k, BASELINE, CANDIDATE)
    healthy = False
    try:
        if wait_for_version(next_version, result["promotion_probes"]):
            if on_promoted is not None:
                on_promoted()
            healthy = True
    finally:
        if not healthy:
            failed_at = time.monotonic()
            switch(k, CANDIDATE, BASELINE)
            result["action"] = "rolled_back"
            recovered = wait_for_version(stable, result["final_probes"])
            result["recovery_seconds"] = round(time.monotonic() - failed_at, 3)
            result["failed_probe_requests"] = sum(
                not sample["valid_answer"] for sample in result["promotion_probes"] + result["final_probes"])
            require(recovered, f"stable endpoint did not recover: {result}")
    if healthy:
        result.update(action="promoted", active_version=CANDIDATE)
    return result


def release(k, registry, refs, scenario):
    reports = {}
    for role, model in (("baseline", BASELINE), ("candidate", CANDIDATE)):
        ref = refs[model]
        report = registry.artifact(registry.get_run(ref["run_id"]), REPORT)
        require(report["registry"] == ref and report["model"] == model,
                "report lineage does not match the deployed registry version")
        reports[role] = report
    require(registry.alias("champion") == refs[BASELINE] and registry.alias("candidate") == refs[CANDIDATE],
            "registry aliases changed after deployment")

    def advance_champion():
        try:
            registry.assign("champion", refs[CANDIDATE])
            require(registry.alias("champion") == refs[CANDIDATE], "champion alias did not advance")
        except Exception:
            registry.assign("champion", refs[BASELINE])
            raise

    result = promote(k, reports, scenario, refs=refs, on_promoted=advance_champion)
    result["registry"] = {alias: registry.alias(alias) for alias in ("champion", "candidate")}
    require(result["registry"]["champion"] == refs[result["active_version"]],
            "champion alias disagrees with the live endpoint")
    registry.release(refs, result)
    record(k, "release-result", result)
    print(json.dumps(result, indent=2), flush=True)


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
    registry = Registry(MLFLOW_URI, args.namespace)
    if args.stage == "deploy":
        deploy(k, registry, args.image, args.scenario)
    else:
        refs = json.loads(k.get("configmap", "deployment-result")["data"]["report.json"])["registry"]
        if args.stage == "evaluate":
            evaluate(k, registry, refs, args.image, args.scenario)
        else:
            release(k, registry, refs, args.scenario)


if __name__ == "__main__":
    main()
