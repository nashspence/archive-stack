FROM jdxcode/mise:2026.7.13@sha256:7fe9145156e33f95f7712632dbadb418592ed0f60fb46e5bcc8c113d372ad8a3 AS mise

FROM python:3.12-slim@sha256:090ba77e2958f6af52a5341f788b50b032dd4ca28377d2893dcf1ecbdfdfe203 AS locked-tools

COPY --from=mise /usr/local/bin/mise /usr/local/bin/mise
WORKDIR /workspace
COPY mise.toml mise.lock ./
RUN mise trust mise.toml \
    && mise install --locked age "http:exiftool" minisign uv \
    && exiftool_path="$(mise which exiftool)" \
    && mkdir -p /opt/riverhog-tools/bin /opt/riverhog-tools/exiftool \
    && cp --preserve=mode,timestamps "$(mise which age)" /opt/riverhog-tools/bin/age \
    && cp --preserve=mode,timestamps "$(mise which age-keygen)" /opt/riverhog-tools/bin/age-keygen \
    && cp --preserve=mode,timestamps "$(mise which age-plugin-batchpass)" /opt/riverhog-tools/bin/age-plugin-batchpass \
    && cp --preserve=mode,timestamps "$(mise which minisign)" /opt/riverhog-tools/bin/minisign \
    && cp --preserve=mode,timestamps "$(mise which uv)" /opt/riverhog-tools/bin/uv \
    && cp --preserve=mode,timestamps "${exiftool_path}" /opt/riverhog-tools/exiftool/exiftool \
    && cp -a "$(dirname "${exiftool_path}")/lib" /opt/riverhog-tools/exiftool/lib \
    && test "$(/opt/riverhog-tools/bin/age --version)" = "$(mise exec age -- age --version)" \
    && test "$(/opt/riverhog-tools/bin/minisign -v)" = "$(mise exec minisign -- minisign -v)" \
    && test "$(/opt/riverhog-tools/bin/uv --version)" = "$(mise exec uv -- uv --version)"


FROM python:3.12-slim@sha256:090ba77e2958f6af52a5341f788b50b032dd4ca28377d2893dcf1ecbdfdfe203

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
    && apt-get install -y --no-install-recommends ca-certificates git perl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=locked-tools /opt/riverhog-tools/bin/ /usr/local/bin/
COPY --from=locked-tools /opt/riverhog-tools/exiftool /opt/exiftool
RUN ln -s /opt/exiftool/exiftool /usr/local/bin/exiftool \
    && test "$(age --version)" = "v1.3.1" \
    && test "$(age-plugin-batchpass --version)" = "v1.3.1" \
    && test "$(exiftool -ver)" = "13.59" \
    && test "$(minisign -v)" = "minisign 0.12" \
    && uv --version | grep -F "uv 0.11.24" \
    && ! command -v mise

WORKDIR /workspace

COPY pyproject.toml uv.lock ./
COPY companions companions
COPY reference reference
COPY packages packages
COPY riverhog riverhog
COPY utilities utilities
RUN uv sync --frozen --all-packages --group dev --no-editable

COPY scripts scripts
COPY tests tests

ENTRYPOINT ["python", "-m", "pytest"]
CMD ["-q"]
