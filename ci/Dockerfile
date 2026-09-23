# Trusted image recipe. Contributors run bin/ci without Docker.
FROM golang:1.27.1-bookworm@sha256:69a7b9788769bec032d238959b61854e9ae87f57be9029ec04e9885fabf99195 AS go
FROM ghcr.io/astral-sh/uv:0.9.28@sha256:59240a65d6b57e6c507429b45f01b8f2c7c0bbeee0fb697c41a39c6a8e3a4cfb AS uv
FROM python:3.11.14-slim-bookworm@sha256:65a93d69fa75478d554f4ad27c85c1e69fa184956261b4301ebaf6dbb0a3543d
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install --no-install-recommends -y \
    bash build-essential ca-certificates git libgomp1 libicu72 libssl3 libxml2 libxslt1.1 procps zlib1g \
    && rm -rf /var/lib/apt/lists/*
RUN useradd --uid 1000 --create-home --shell /bin/bash ci
COPY --from=go /usr/local/go /usr/local/go
COPY --from=uv /uv /uvx /usr/local/bin/
ENV PATH="/usr/local/go/bin:${PATH}"
# PostgreSQL requires a non-root uid with an OS account. The standalone runner
# supplies a writable checkout, private HOME/scratch and network/resource limits.
USER 1000:1000
WORKDIR /workspace
CMD ["bin/ci"]
