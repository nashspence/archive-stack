FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl ffmpeg git rclone tini \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY services/munchy-runner/requirements.txt .
RUN pip install --upgrade pip setuptools wheel \
 && pip install -r requirements.txt

COPY pyproject.toml README.md /riverhog/
COPY contracts /riverhog/contracts
COPY src /riverhog/src
RUN pip install /riverhog

COPY services/munchy-runner/app app

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "app.main"]
