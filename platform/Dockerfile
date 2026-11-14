FROM python:3.12-slim
ARG KUBECTL_VERSION=v1.34.2
ARG KUBECTL_SHA256=9591f3d75e1581f3f7392e6ad119aab2f28ae7d6c6e083dc5d22469667f27253
ADD --checksum=sha256:${KUBECTL_SHA256} https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl /usr/local/bin/kubectl
RUN chmod 755 /usr/local/bin/kubectl
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY rag_infra/ rag_infra/
COPY fixtures/rag-suite.json fixtures/rag-suite.json
USER 10001:10001
EXPOSE 8080
CMD ["python", "-m", "rag_infra.serve"]
