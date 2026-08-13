FROM jdxcode/mise:2026.8.3@sha256:92dbc3f2573926d8974e4641ad8449f16c323130b9f41c39aff19b7b2f500ef6 AS mise

FROM python:3.14-slim@sha256:ce40764625a4ff50df3548277632e7f96c4e77fe75fa848aae9885476e7df5a4 AS age-tools

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


FROM python:3.14-slim@sha256:ce40764625a4ff50df3548277632e7f96c4e77fe75fa848aae9885476e7df5a4

ARG SOURCE_DATE_EPOCH=0
ENV DEBIAN_FRONTEND=noninteractive \
    PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_LINK_MODE=copy \
    SOURCE_DATE_EPOCH=${SOURCE_DATE_EPOCH}

ARG BUILD_CREATED=1970-01-01T00:00:00Z
ARG RELEASE_VERSION=development
ARG SOURCE_REVISION=unknown

LABEL org.opencontainers.image.title="Riverhog Test Suite" \
      org.opencontainers.image.licenses="CAL-1.0 AND Apache-2.0" \
      org.opencontainers.image.source="https://github.com/nashspence/riverhog" \
      org.opencontainers.image.revision="${SOURCE_REVISION}" \
      org.opencontainers.image.version="${RELEASE_VERSION}" \
      org.opencontainers.image.created="${BUILD_CREATED}" \
      org.opencontainers.image.documentation="https://nashspence.github.io/riverhog/v1/"

COPY LICENSES/Apache-2.0.txt /usr/share/licenses/riverhog/Apache-2.0.txt
COPY LICENSES/CAL-1.0.txt /usr/share/licenses/riverhog/CAL-1.0.txt
COPY THIRD_PARTY_NOTICES.md /usr/share/doc/riverhog/THIRD_PARTY_NOTICES.md

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git minisign \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.11.24@sha256:99ea34acedc870ba4ad11a1f540a1c04267c9f30aadc465a94406f52dfda2c36 /uv /uvx /usr/local/bin/

COPY --from=age-tools /opt/riverhog-age/bin/ /usr/local/bin/
RUN test -x /usr/local/bin/age \
    && test -x /usr/local/bin/age-keygen \
    && test -x /usr/local/bin/age-plugin-batchpass \
    && ! command -v mise

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
