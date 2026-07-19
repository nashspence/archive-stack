FROM python:3.12-slim AS build

ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_LINK_MODE=copy

COPY --from=ghcr.io/astral-sh/uv:0.11.24 /uv /uvx /usr/local/bin/
WORKDIR /workspace

COPY pyproject.toml uv.lock ./
COPY apps/munchy/pyproject.toml apps/munchy/pyproject.toml
COPY apps/munchy/src apps/munchy/src
COPY packages/application-access packages/application-access
COPY packages/cli-support packages/cli-support
COPY packages/config-validation packages/config-validation
COPY packages/lifecycle-events packages/lifecycle-events
COPY packages/munchy-api-client packages/munchy-api-client
COPY packages/munchy-config packages/munchy-config
COPY packages/riverhog-age packages/riverhog-age
COPY packages/riverhog-api-client packages/riverhog-api-client
COPY packages/riverhog-protocol packages/riverhog-protocol
COPY packages/time-formats packages/time-formats
COPY packages/tus-transport packages/tus-transport
RUN uv sync --frozen --package munchy --no-dev --no-editable


FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

ARG SOURCE_REVISION=unknown
LABEL org.opencontainers.image.title="Munchy Runner" \
      org.opencontainers.image.revision="${SOURCE_REVISION}"

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl ffmpeg git libimage-exiftool-perl rclone tini zstd \
    && rm -rf /var/lib/apt/lists/*

COPY --from=build /opt/venv /opt/venv

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["munchy-runner"]
