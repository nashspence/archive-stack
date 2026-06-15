FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates tini \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md /riverhog/
COPY contracts /riverhog/contracts
COPY src /riverhog/src
RUN pip install --upgrade pip setuptools wheel \
 && pip install /riverhog

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["jeb", "run", "--config", "/config/jeb.toml"]
