FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/riverhog/src

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl ffmpeg git libimage-exiftool-perl rclone tini \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements-service.txt /tmp/requirements-service.txt
RUN pip install --no-cache-dir --require-hashes -r /tmp/requirements-service.txt

COPY contracts /riverhog/contracts
COPY src /riverhog/src

COPY services/munchy-runner/app app

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "app.main"]
