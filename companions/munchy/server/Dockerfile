FROM jdxcode/mise:2026.7.13@sha256:7fe9145156e33f95f7712632dbadb418592ed0f60fb46e5bcc8c113d372ad8a3 AS mise

FROM python:3.12-slim@sha256:090ba77e2958f6af52a5341f788b50b032dd4ca28377d2893dcf1ecbdfdfe203 AS locked-tools

COPY --from=mise /usr/local/bin/mise /usr/local/bin/mise
WORKDIR /workspace
COPY mise.toml mise.lock ./
RUN mise trust mise.toml \
    && mise install --locked "http:exiftool" uv \
    && exiftool_path="$(mise which exiftool)" \
    && mkdir -p /opt/exiftool /opt/riverhog-tools/bin \
    && cp --preserve=mode,timestamps "${exiftool_path}" /opt/exiftool/exiftool \
    && cp -a "$(dirname "${exiftool_path}")/lib" /opt/exiftool/lib \
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
COPY companions/munchy/server/pyproject.toml companions/munchy/server/pyproject.toml
COPY companions/munchy/server/src companions/munchy/server/src
COPY packages/application-access packages/application-access
COPY packages/config-validation packages/config-validation
COPY packages/file-download packages/file-download
COPY packages/http-api-contracts packages/http-api-contracts
COPY packages/lifecycle-events packages/lifecycle-events
COPY packages/munchy-api-client packages/munchy-api-client
COPY packages/munchy-config packages/munchy-config
COPY packages/munchy-target-support packages/munchy-target-support
COPY packages/munchy-workflows packages/munchy-workflows
COPY packages/riverhog-api-client packages/riverhog-api-client
COPY packages/riverhog-provenance packages/riverhog-provenance
COPY packages/riverhog-protocol packages/riverhog-protocol
COPY packages/state-schema packages/state-schema
COPY packages/time-formats packages/time-formats
COPY packages/tus-transport packages/tus-transport
RUN uv sync --frozen --package munchy-server --no-dev --no-editable


FROM python:3.12-slim@sha256:090ba77e2958f6af52a5341f788b50b032dd4ca28377d2893dcf1ecbdfdfe203

ENV DEBIAN_FRONTEND=noninteractive \
    PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

ARG BUILD_CREATED=1970-01-01T00:00:00Z
ARG RELEASE_VERSION=development
ARG SOURCE_REVISION=unknown
LABEL org.opencontainers.image.title="Munchy Server" \
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
    && apt-get install -y --no-install-recommends ca-certificates curl ffmpeg git perl rclone tini zstd \
    && rm -rf /var/lib/apt/lists/*

COPY --from=locked-tools /opt/exiftool /opt/exiftool
RUN ln -s /opt/exiftool/exiftool /usr/local/bin/exiftool \
    && test "$(exiftool -ver)" = "13.59" \
    && ! command -v mise

COPY --from=build /opt/venv /opt/venv

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["munchy-server"]
