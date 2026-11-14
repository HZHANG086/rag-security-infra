# RAG serving and release workflow

This demo uses the existing 12-case fixture suite. Argo deploys two versioned
KServe InferenceServices, evaluates both predictor endpoints with Kubernetes
Indexed Jobs (four shards, two concurrent evaluator Pods per Job), then
applies the release decision. KServe runs the inference containers. A separate
in-cluster Kubernetes Service routes live traffic by selector. Probes check
the HTTP status, response body and version header before accepting a switch.

| Scenario | Acceptance criteria |
| --- | --- |
| Promotion | Both evaluations pass, and the live Service returns the candidate version. |
| Prompt injection | The candidate completes all 12 cases but fails the safety gate. The stable version remains live. |
| Scheduling failure | The candidate evaluation Pod cannot schedule. The run is incomplete and the stable version remains live. |
| Rollback | Offline evaluation passes. A separate post-switch inference request returns HTTP 503 from the candidate. Restore the stable selector and confirm a stable response. |

The rollback result reports failed probes and time from detection to the first
verified stable response. These are measurements of the smoke probes for a
synthetic fault. They do not represent production SLA or model quality.

Use an isolated Linux x86-64 environment with Docker, kind, kubectl and Python:

```sh
kind create cluster --name rag-platform --image kindest/node:v1.34.2 --wait 120s
bash platform/install.sh kind-rag-platform
docker build -t rag-platform:demo -f platform/Dockerfile .
kind load docker-image --name rag-platform rag-platform:demo
PYTHONPATH=. python3 platform/demo.py --context kind-rag-platform
```

Reports, workflow status, pod status and events are stored in
`artifacts/platform/`. The runner isolates each scenario in its own namespace.
Delete the disposable cluster with `kind delete cluster --name rag-platform`
when finished.
