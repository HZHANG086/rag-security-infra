"""MLflow REST operations for the synthetic RAG release workflow."""

import json
import time
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from rag_infra.core import canonical_digest

REPORT = "evaluation.json"
MANIFEST = "deployment/inference-service.json"
RELEASE = "release.json"


class Registry:
    def __init__(self, uri, name):
        self.uri = uri.rstrip("/")
        self.name = name

    def request(self, resource, *, query=None, payload=None, method=None):
        url = f"{self.uri}/api/2.0/{resource}"
        if query:
            url += "?" + urlencode(query)
        data = None if payload is None else json.dumps(payload).encode()
        headers = {"Content-Type": "application/json"} if data is not None else {}
        request = Request(url, data=data, headers=headers, method=method)
        with urlopen(request, timeout=12) as response:
            body = response.read()
            return json.loads(body) if body else {}

    def rest(self, operation, **kwargs):
        return self.request(f"mlflow/{operation}", **kwargs)

    def artifact(self, run, path, payload=None):
        root = run["info"]["artifact_uri"]
        if not root.startswith("mlflow-artifacts:/"):
            raise ValueError(f"expected proxied MLflow artifacts, got {root}")
        key = root.removeprefix("mlflow-artifacts:/").strip("/")
        return self.request(f"mlflow-artifacts/artifacts/{quote(key + '/' + path)}",
                            payload=payload, method="PUT" if payload is not None else "GET")

    def get_run(self, run_id):
        return self.rest("runs/get", query={"run_id": run_id})["run"]

    def create(self):
        experiment = self.rest("experiments/create", payload={"name": self.name})
        self.rest("registered-models/create", payload={"name": self.name})
        return experiment["experiment_id"]

    def register(self, experiment_id, manifest, role):
        name = manifest["metadata"]["name"]
        digest = canonical_digest(manifest)
        tags = {"deployment_name": name, "manifest_sha256": digest, "release_role": role,
                "backend": "synthetic_fixture"}
        run = self.rest("runs/create", payload={"experiment_id": experiment_id, "run_name": role,
                                                "tags": [{"key": k, "value": v} for k, v in tags.items()]})["run"]
        self.artifact(run, MANIFEST, manifest)
        run_id = run["info"]["run_id"]
        version = self.rest("model-versions/create", payload={
            "name": self.name, "source": run["info"]["artifact_uri"].rstrip("/") + "/deployment", "run_id": run_id,
            "tags": [{"key": k, "value": v} for k, v in tags.items()],
        })["model_version"]
        return self.reference(version)

    @staticmethod
    def reference(version):
        tags = {tag["key"]: tag["value"] for tag in version["tags"]}
        return {"name": version["name"], "version": version["version"], "run_id": version["run_id"],
                "deployment": tags["deployment_name"], "manifest_sha256": tags["manifest_sha256"]}

    def alias(self, alias):
        response = self.rest("registered-models/alias", query={"name": self.name, "alias": alias})
        return self.reference(response["model_version"])

    def assign(self, alias, version):
        self.rest("registered-models/alias", payload={
            "name": self.name, "alias": alias, "version": version["version"],
        })

    def manifest(self, ref):
        manifest = self.artifact(self.get_run(ref["run_id"]), MANIFEST)
        if canonical_digest(manifest) != ref["manifest_sha256"] or manifest["metadata"]["name"] != ref["deployment"]:
            raise ValueError("registry artifact does not match the registered deployment and digest")
        return manifest

    def evaluation(self, ref, report):
        run_id = ref["run_id"]
        run = self.get_run(run_id)
        self.artifact(run, REPORT, report)
        rates = report["metrics"]
        metrics = {"completion_rate": rates["completion_rate"]}
        for key, values in rates.items():
            if key != "completion_rate" and values["rate"] is not None:
                metrics[key] = values["rate"]
        tags = {"model_version": ref["version"], "evaluation_status": report["status"],
                "dataset_sha256": report["dataset_sha256"], "prompt_version": report["prompt"],
                "evaluation_report": REPORT}
        timestamp = int(time.time() * 1000)
        self.rest("runs/log-batch", payload={
            "run_id": run_id,
            "metrics": [{"key": k, "value": v, "timestamp": timestamp, "step": 0} for k, v in metrics.items()],
            "tags": [{"key": k, "value": v} for k, v in tags.items()],
        })
        self.rest("model-versions/set-tag", payload={
            "name": self.name, "version": ref["version"], "key": "evaluation_status", "value": report["status"],
        })

    def release(self, refs, result):
        for ref in refs.values():
            run_id = ref["run_id"]
            self.artifact(self.get_run(run_id), RELEASE, result)
            self.rest("runs/update", payload={"run_id": run_id, "status": "FINISHED"})
