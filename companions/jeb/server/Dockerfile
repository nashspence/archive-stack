FROM python:3.12-slim AS build

ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_LINK_MODE=copy

COPY --from=ghcr.io/astral-sh/uv:0.11.24 /uv /uvx /usr/local/bin/
WORKDIR /workspace

COPY pyproject.toml uv.lock ./
COPY companions/jeb/server/pyproject.toml companions/jeb/server/pyproject.toml
COPY companions/jeb/server/src companions/jeb/server/src
COPY packages/riverhog-cli-support packages/riverhog-cli-support
COPY packages/jeb-cli-support packages/jeb-cli-support
COPY packages/jeb-protocol packages/jeb-protocol
COPY packages/lifecycle-events packages/lifecycle-events
COPY packages/munchy-api-client packages/munchy-api-client
COPY packages/time-formats packages/time-formats
COPY packages/tus-transport packages/tus-transport
RUN uv sync --frozen --package jeb-server --no-dev --no-editable


FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

ARG SOURCE_REVISION=unknown
LABEL org.opencontainers.image.title="Jeb" \
      org.opencontainers.image.licenses="CAL-1.0" \
      org.opencontainers.image.revision="${SOURCE_REVISION}"

COPY LICENSES/Apache-2.0.txt /usr/share/licenses/riverhog/Apache-2.0.txt
COPY LICENSES/CAL-1.0.txt /usr/share/licenses/riverhog/CAL-1.0.txt
COPY THIRD_PARTY_NOTICES.md /usr/share/doc/riverhog/THIRD_PARTY_NOTICES.md

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates ffmpeg libimage-exiftool-perl tini \
    && rm -rf /var/lib/apt/lists/*

COPY --from=build /opt/venv /opt/venv

EXPOSE 8081
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["jeb-service", "run"]
