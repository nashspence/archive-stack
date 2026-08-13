FROM jdxcode/mise:2026.7.13@sha256:7fe9145156e33f95f7712632dbadb418592ed0f60fb46e5bcc8c113d372ad8a3 AS mise

FROM python:3.12-slim@sha256:090ba77e2958f6af52a5341f788b50b032dd4ca28377d2893dcf1ecbdfdfe203 AS age-tools

COPY --from=mise /usr/local/bin/mise /usr/local/bin/mise
WORKDIR /workspace
COPY mise.toml mise.lock ./
RUN mise trust mise.toml \
    && mise install --locked age \
    && mkdir -p /opt/riverhog-age/bin \
    && cp --preserve=mode,timestamps "$(mise which age)" /opt/riverhog-age/bin/age \
    && cp --preserve=mode,timestamps "$(mise which age-keygen)" /opt/riverhog-age/bin/age-keygen \
    && cp --preserve=mode,timestamps "$(mise which age-plugin-batchpass)" /opt/riverhog-age/bin/age-plugin-batchpass \
    && test "$(/opt/riverhog-age/bin/age --version)" = "$(mise exec age -- age --version)"


FROM python:3.12-slim@sha256:090ba77e2958f6af52a5341f788b50b032dd4ca28377d2893dcf1ecbdfdfe203 AS build

ARG SOURCE_DATE_EPOCH=0
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_LINK_MODE=copy \
    SOURCE_DATE_EPOCH=${SOURCE_DATE_EPOCH}

COPY --from=ghcr.io/astral-sh/uv:0.11.24@sha256:99ea34acedc870ba4ad11a1f540a1c04267c9f30aadc465a94406f52dfda2c36 /uv /uvx /usr/local/bin/
WORKDIR /workspace

COPY pyproject.toml uv.lock ./
COPY riverhog/server/pyproject.toml riverhog/server/pyproject.toml
COPY riverhog/server/src riverhog/server/src
COPY packages/application-access packages/application-access
COPY packages/http-api-contracts packages/http-api-contracts
COPY packages/lifecycle-events packages/lifecycle-events
COPY packages/riverhog-age packages/riverhog-age
COPY packages/riverhog-provenance packages/riverhog-provenance
COPY packages/riverhog-protocol packages/riverhog-protocol
COPY packages/state-schema packages/state-schema
COPY packages/time-formats packages/time-formats
RUN uv sync --frozen --package riverhog-server --no-dev --no-editable


FROM python:3.12-slim@sha256:090ba77e2958f6af52a5341f788b50b032dd4ca28377d2893dcf1ecbdfdfe203

ENV DEBIAN_FRONTEND=noninteractive \
    PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

ARG BUILD_CREATED=1970-01-01T00:00:00Z
ARG RELEASE_VERSION=development
ARG SOURCE_REVISION=unknown
LABEL org.opencontainers.image.title="Riverhog" \
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
    && apt-get install -y --no-install-recommends ca-certificates minisign \
    && rm -rf /var/lib/apt/lists/*

COPY --from=age-tools /opt/riverhog-age/bin/ /usr/local/bin/
RUN test -x /usr/local/bin/age \
    && test -x /usr/local/bin/age-keygen \
    && test -x /usr/local/bin/age-plugin-batchpass \
    && ! command -v mise

COPY --from=build /opt/venv /opt/venv

EXPOSE 8000
ENTRYPOINT ["riverhog-api"]
