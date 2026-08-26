FROM jdxcode/mise:2026.7.13@sha256:7fe9145156e33f95f7712632dbadb418592ed0f60fb46e5bcc8c113d372ad8a3 AS mise

FROM python:3.12-slim@sha256:090ba77e2958f6af52a5341f788b50b032dd4ca28377d2893dcf1ecbdfdfe203 AS locked-tools

COPY --from=mise /usr/local/bin/mise /usr/local/bin/mise
WORKDIR /workspace
COPY mise.toml mise.lock ./
RUN mise trust mise.toml \
    && mise install --locked uv \
    && mkdir -p /opt/riverhog-tools/bin \
    && cp --preserve=mode,timestamps "$(mise which uv)" /opt/riverhog-tools/bin/uv \
    && test "$(/opt/riverhog-tools/bin/uv --version)" = "$(mise exec uv -- uv --version)"

FROM python:3.12-slim@sha256:090ba77e2958f6af52a5341f788b50b032dd4ca28377d2893dcf1ecbdfdfe203 AS build

ARG SOURCE_DATE_EPOCH=0
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_LINK_MODE=copy \
    SOURCE_DATE_EPOCH=${SOURCE_DATE_EPOCH}

COPY --from=locked-tools /opt/riverhog-tools/bin/uv /usr/local/bin/uv
WORKDIR /workspace
COPY pyproject.toml uv.lock ./
COPY companions/stove0/server companions/stove0/server
COPY packages/application-access packages/application-access
COPY packages/config-validation packages/config-validation
COPY packages/file-download packages/file-download
COPY packages/http-api-contracts packages/http-api-contracts
COPY packages/lifecycle-events packages/lifecycle-events
COPY packages/riverhog-api-client packages/riverhog-api-client
COPY packages/riverhog-protocol packages/riverhog-protocol
COPY packages/riverhog-provenance-contracts packages/riverhog-provenance-contracts
COPY packages/riverhog-provenance-linux-contracts packages/riverhog-provenance-linux-contracts
COPY packages/riverhog-provenance-macos-contracts packages/riverhog-provenance-macos-contracts
COPY packages/riverhog-provenance-windows-contracts packages/riverhog-provenance-windows-contracts
COPY packages/riverhog-provenance packages/riverhog-provenance
COPY packages/stove0-observer-client packages/stove0-observer-client
COPY packages/stove0-observer-protocol packages/stove0-observer-protocol
COPY packages/stove0-media-metadata-observer-contracts packages/stove0-media-metadata-observer-contracts
COPY packages/stove0-media-sampling-observer-contracts packages/stove0-media-sampling-observer-contracts
COPY packages/stove0-operator-contracts packages/stove0-operator-contracts
COPY packages/stove0-protocol packages/stove0-protocol
COPY packages/stove0-recipe-config packages/stove0-recipe-config
COPY packages/stove0-target-client packages/stove0-target-client
COPY packages/stove0-target-protocol packages/stove0-target-protocol
COPY packages/state-schema packages/state-schema
COPY packages/time-formats packages/time-formats
RUN uv sync --frozen --package stove0-server --extra first-party-observer-contracts --no-dev --no-editable

FROM python:3.12-slim@sha256:090ba77e2958f6af52a5341f788b50b032dd4ca28377d2893dcf1ecbdfdfe203

ENV DEBIAN_FRONTEND=noninteractive \
    PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

ARG BUILD_CREATED=1970-01-01T00:00:00Z
ARG RELEASE_VERSION=development
ARG SOURCE_REVISION=unknown
LABEL org.opencontainers.image.title="stove0" \
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
    && apt-get install -y --no-install-recommends ca-certificates tini \
    && rm -rf /var/lib/apt/lists/* \
    && ! command -v mise

COPY --from=build /opt/venv /opt/venv

USER 65532:65532
EXPOSE 8080
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["stove0-server", "serve", "--host", "0.0.0.0", "--port", "8080"]
