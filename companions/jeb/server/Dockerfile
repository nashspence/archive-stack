FROM jdxcode/mise:2026.8.3@sha256:92dbc3f2573926d8974e4641ad8449f16c323130b9f41c39aff19b7b2f500ef6 AS mise

FROM python:3.12-slim@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36 AS locked-tools

COPY --from=mise /usr/local/bin/mise /usr/local/bin/mise
WORKDIR /workspace
COPY mise.toml mise.lock ./
RUN mise trust mise.toml \
    && mise install --locked uv \
    && mkdir -p /opt/riverhog-tools/bin \
    && cp --preserve=mode,timestamps "$(mise which uv)" /opt/riverhog-tools/bin/uv \
    && test "$(/opt/riverhog-tools/bin/uv --version)" = "$(mise exec uv -- uv --version)"


FROM python:3.12-slim@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36 AS build

ARG SOURCE_DATE_EPOCH=0
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_LINK_MODE=copy \
    SOURCE_DATE_EPOCH=${SOURCE_DATE_EPOCH}

COPY --from=locked-tools /opt/riverhog-tools/bin/uv /usr/local/bin/uv
WORKDIR /workspace

COPY pyproject.toml uv.lock ./
COPY companions/jeb/server/pyproject.toml companions/jeb/server/pyproject.toml
COPY companions/jeb/server/src companions/jeb/server/src
COPY packages/http-api-contracts packages/http-api-contracts
COPY packages/riverhog-cli-support packages/riverhog-cli-support
COPY packages/riverhog-provenance packages/riverhog-provenance
COPY packages/jeb-cli-support packages/jeb-cli-support
COPY packages/jeb-protocol packages/jeb-protocol
COPY packages/file-download packages/file-download
COPY packages/lifecycle-events packages/lifecycle-events
COPY packages/media-preflight packages/media-preflight
COPY packages/munchy-api-client packages/munchy-api-client
COPY packages/state-schema packages/state-schema
COPY packages/time-formats packages/time-formats
COPY packages/tus-transport packages/tus-transport
RUN uv sync --frozen --package jeb-server --no-dev --no-editable


FROM python:3.12-slim@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36

ENV DEBIAN_FRONTEND=noninteractive \
    PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

ARG BUILD_CREATED=1970-01-01T00:00:00Z
ARG RELEASE_VERSION=development
ARG SOURCE_REVISION=unknown
LABEL org.opencontainers.image.title="Jeb" \
      org.opencontainers.image.licenses="CAL-1.0" \
      org.opencontainers.image.source="https://github.com/nashspence/riverhog" \
      org.opencontainers.image.revision="${SOURCE_REVISION}" \
      org.opencontainers.image.version="${RELEASE_VERSION}" \
      org.opencontainers.image.created="${BUILD_CREATED}" \
      org.opencontainers.image.documentation="https://nashspence.github.io/riverhog/v1/"

COPY LICENSES/Apache-2.0.txt /usr/share/licenses/riverhog/Apache-2.0.txt
COPY LICENSES/CAL-1.0.txt /usr/share/licenses/riverhog/CAL-1.0.txt
COPY THIRD_PARTY_NOTICES.md /usr/share/doc/riverhog/THIRD_PARTY_NOTICES.md

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates ffmpeg tini \
    && rm -rf /var/lib/apt/lists/*

COPY --from=build /opt/venv /opt/venv

EXPOSE 8081
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["jeb-service", "run"]
