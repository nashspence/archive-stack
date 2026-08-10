FROM python:3.14-slim@sha256:a7fb1e634c4a578f9e0bd6327f11a3cde11b7a9395f48e24360c0988bcc5c2bc AS build

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


FROM python:3.14-slim@sha256:a7fb1e634c4a578f9e0bd6327f11a3cde11b7a9395f48e24360c0988bcc5c2bc

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

COPY mise.lock /usr/local/share/riverhog/mise.lock
COPY scripts/install_locked_age.py /usr/local/libexec/riverhog/install_locked_age.py
RUN test "$(dpkg --print-architecture)" = amd64 \
    && python /usr/local/libexec/riverhog/install_locked_age.py \
      --lock /usr/local/share/riverhog/mise.lock \
      --destination /usr/local/bin

COPY --from=build /opt/venv /opt/venv

EXPOSE 8000
ENTRYPOINT ["riverhog-api"]
