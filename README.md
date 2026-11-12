# RAG Security Infra

An independent engineering extension of a RAG safety evaluation project. Two fixture backends act as the baseline and candidate. The evaluator checks paired clean and indirect prompt-injection questions, plus questions that lack evidence. All included examples and backends are synthetic. This repo contains no lab scripts, datasets, or credentials and does not report safety results for a real model.

## Run without Kubernetes

```bash
python3 -m unittest discover -s tests -v
python3 -m rag_infra.core local --suite fixtures/rag-suite.json --model fixture-v1 --prompt guarded-v1
python3 -m rag_infra.core local --suite fixtures/rag-suite.json --model fixture-v1 --prompt regressed-v1 --fixture regressed
```

The first run passes. The intentionally regressed run returns code 1: it completes all 12 cases, but one of four attack cases triggers its synthetic marker. The CLI returns 2 for incomplete runs. Reports include model and prompt IDs, a dataset digest, sample coverage, attack success, grounded accuracy, and correct abstention.

## Run on Kubernetes

Requires Docker, kind, kubectl and a Kubernetes version with per-index Job retries (v1.33+ recommended).

```bash
kind create cluster --name rag-security --wait 120s
docker build -t rag-security:demo .
kind load docker-image --name rag-security rag-security:demo
python3 -m rag_infra.cluster --context kind-rag-security --image rag-security:demo
```

The demo creates an isolated test namespace, deploys two HTTP backends, and runs three real Indexed Jobs with four shards and parallelism two:

| Run | Expected observation |
| --- | --- |
| `baseline` | All shards finish and the release gate can compare against it. |
| `injection` | All samples are scored; a deliberately vulnerable fixture emits the canary on one case, so the release is blocked. |
| `unschedulable` | Worker Pods ask for 1000 CPU cores. The scheduler reports `Unschedulable`; the run is incomplete and attack rate is undefined. |

API timeouts and retryable HTTP errors have one request retry. Failed shard attempts cannot replace a successful shard result. Reports with missing shards, conflicting retries or a mismatched dataset/configuration cannot pass. Reports are written to `artifacts/rag-eval/` and uploaded by CI. The namespace is left for inspection; the demo prints its exact name so you can clean it up when done.

## Connect an existing RAG pipeline locally

Keep any internal corpus and configuration outside Git. The adapter endpoint accepts `POST` JSON with `case_id`, `question` and `documents` (a list of objects with `id`, `topic` and `text`) and returns `{"answer":"…","citations":["document-id"]}`. Put a thin wrapper in front of your existing RAG code: index or retrieve from the supplied case documents, invoke the existing generator, and map its evidence IDs to `citations`. The fixture grader expects short canonical answers or `INSUFFICIENT_EVIDENCE`; add a suitable grader before using free-form production answers.

```bash
python3 -m rag_infra.core local --suite /path/to/private/suite.json \
  --backend-mode rag --endpoint http://127.0.0.1:8090/rag/answer \
  --model your-model-version --prompt your-prompt-version
```

The real-backend interface is optional. The automated demo always uses local deterministic backends, so it requires no model keys, cloud account, or original project files. See the [Indexed Job guide](https://kubernetes.io/docs/tasks/job/indexed-parallel-processing-static/) for shard assignment and the [Job retry documentation](https://kubernetes.io/docs/concepts/workloads/controllers/job/#backoff-limit-per-index) for per-index retries.
