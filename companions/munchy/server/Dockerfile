FROM python:3.12-slim AS build

ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_LINK_MODE=copy

COPY --from=ghcr.io/astral-sh/uv:0.11.24 /uv /uvx /usr/local/bin/
WORKDIR /workspace

COPY pyproject.toml uv.lock ./
COPY companions/munchy/server/pyproject.toml companions/munchy/server/pyproject.toml
COPY companions/munchy/server/src companions/munchy/server/src
COPY packages/application-access packages/application-access
COPY packages/config-validation packages/config-validation
COPY packages/lifecycle-events packages/lifecycle-events
COPY packages/munchy-api-client packages/munchy-api-client
COPY packages/munchy-config packages/munchy-config
COPY packages/munchy-target-support packages/munchy-target-support
COPY packages/munchy-workflows packages/munchy-workflows
COPY packages/riverhog-age packages/riverhog-age
COPY packages/riverhog-api-client packages/riverhog-api-client
COPY packages/riverhog-protocol packages/riverhog-protocol
COPY packages/time-formats packages/time-formats
COPY packages/tus-transport packages/tus-transport
RUN uv sync --frozen --package munchy-server --no-dev --no-editable


FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

ARG SOURCE_REVISION=unknown
LABEL org.opencontainers.image.title="Munchy Server" \
      org.opencontainers.image.licenses="CAL-1.0" \
      org.opencontainers.image.revision="${SOURCE_REVISION}"

COPY LICENSES/Apache-2.0.txt /usr/share/licenses/riverhog/Apache-2.0.txt
COPY LICENSES/CAL-1.0.txt /usr/share/licenses/riverhog/CAL-1.0.txt
COPY THIRD_PARTY_NOTICES.md /usr/share/doc/riverhog/THIRD_PARTY_NOTICES.md

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl ffmpeg git libimage-exiftool-perl rclone tini zstd \
    && rm -rf /var/lib/apt/lists/*

COPY --from=build /opt/venv /opt/venv

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["munchy-server"]
