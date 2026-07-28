FROM python:3.12-slim AS build

ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_LINK_MODE=copy

COPY --from=ghcr.io/astral-sh/uv:0.11.24 /uv /uvx /usr/local/bin/
WORKDIR /workspace

COPY pyproject.toml uv.lock ./
COPY utilities/mango-fish/pyproject.toml utilities/mango-fish/pyproject.toml
COPY utilities/mango-fish/src utilities/mango-fish/src
COPY packages/lifecycle-events packages/lifecycle-events
RUN uv sync --frozen --package mango-fish --no-dev --no-editable


FROM python:3.12-slim

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

ARG SOURCE_REVISION=unknown
LABEL org.opencontainers.image.title="Mango Fish" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.revision="${SOURCE_REVISION}"

COPY LICENSES/Apache-2.0.txt /usr/share/licenses/riverhog/Apache-2.0.txt
COPY LICENSES/CAL-1.0.txt /usr/share/licenses/riverhog/CAL-1.0.txt
COPY THIRD_PARTY_NOTICES.md /usr/share/doc/riverhog/THIRD_PARTY_NOTICES.md

COPY --from=build /opt/venv /opt/venv

ENTRYPOINT ["mango-fish"]
