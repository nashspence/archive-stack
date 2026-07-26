FROM python:3.12-slim AS build

ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_LINK_MODE=copy

COPY --from=ghcr.io/astral-sh/uv:0.11.24 /uv /uvx /usr/local/bin/
WORKDIR /workspace

COPY pyproject.toml uv.lock ./
COPY riverhog/server/pyproject.toml riverhog/server/pyproject.toml
COPY riverhog/server/src riverhog/server/src
COPY packages/application-access packages/application-access
COPY packages/lifecycle-events packages/lifecycle-events
COPY packages/riverhog-age packages/riverhog-age
COPY packages/riverhog-protocol packages/riverhog-protocol
COPY packages/time-formats packages/time-formats
RUN uv sync --frozen --package riverhog-server --no-dev --no-editable


FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

ARG AGE_VERSION=v1.3.1
ARG SOURCE_REVISION=unknown
LABEL org.opencontainers.image.title="Riverhog" \
      org.opencontainers.image.revision="${SOURCE_REVISION}"

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates minisign \
    && rm -rf /var/lib/apt/lists/*

RUN arch="$(dpkg --print-architecture)" \
    && case "${arch}" in \
    amd64) age_arch="amd64" ;; \
    arm64) age_arch="arm64" ;; \
    armhf) age_arch="arm" ;; \
    *) echo "Unsupported architecture: ${arch}" >&2; exit 1 ;; \
    esac \
    && url="https://github.com/FiloSottile/age/releases/download/${AGE_VERSION}/age-${AGE_VERSION}-linux-${age_arch}.tar.gz" \
    && AGE_URL="${url}" python -c "import os, urllib.request; urllib.request.urlretrieve(os.environ['AGE_URL'], '/tmp/age.tar.gz')" \
    && age_dir="$(tar -tzf /tmp/age.tar.gz | head -1 | cut -d/ -f1)" \
    && tar -xzf /tmp/age.tar.gz -C /tmp \
    && cp "/tmp/${age_dir}/age" "/tmp/${age_dir}/age-keygen" "/tmp/${age_dir}/age-plugin-batchpass" /usr/local/bin/ \
    && rm -rf /tmp/age.tar.gz "/tmp/${age_dir}"

COPY --from=build /opt/venv /opt/venv

EXPOSE 8000
ENTRYPOINT ["riverhog-api"]
