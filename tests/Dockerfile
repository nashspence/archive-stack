FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_LINK_MODE=copy

ARG AGE_VERSION=v1.3.1
ARG SOURCE_REVISION=unknown

LABEL org.opencontainers.image.licenses="CAL-1.0 AND Apache-2.0" \
      org.opencontainers.image.revision="${SOURCE_REVISION}"

COPY LICENSES/Apache-2.0.txt /usr/share/licenses/riverhog/Apache-2.0.txt
COPY LICENSES/CAL-1.0.txt /usr/share/licenses/riverhog/CAL-1.0.txt
COPY THIRD_PARTY_NOTICES.md /usr/share/doc/riverhog/THIRD_PARTY_NOTICES.md

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git minisign \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.11.24 /uv /uvx /usr/local/bin/

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

WORKDIR /workspace

COPY pyproject.toml uv.lock ./
COPY companions companions
COPY packages packages
COPY riverhog riverhog
COPY utilities utilities
RUN uv sync --frozen --all-packages --group dev --no-editable

COPY scripts scripts
COPY tests tests

ENTRYPOINT ["python", "-m", "pytest"]
CMD ["-q"]
