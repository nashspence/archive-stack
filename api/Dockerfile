FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ARG AGE_VERSION=v1.3.1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl xorriso \
    && rm -rf /var/lib/apt/lists/*

RUN arch="$(dpkg --print-architecture)" \
    && case "$arch" in \
        amd64) age_arch=amd64 ;; \
        arm64) age_arch=arm64 ;; \
        *) echo "unsupported architecture: $arch" >&2; exit 1 ;; \
    esac \
    && curl -L "https://github.com/FiloSottile/age/releases/download/${AGE_VERSION}/age-${AGE_VERSION}-linux-${age_arch}.tar.gz" -o /tmp/age.tar.gz \
    && tar -xzf /tmp/age.tar.gz -C /tmp \
    && install -m 0755 /tmp/age/age /usr/local/bin/age \
    && install -m 0755 /tmp/age/age-plugin-batchpass /usr/local/bin/age-plugin-batchpass \
    && rm -rf /tmp/age /tmp/age.tar.gz \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-activation-dir -r requirements.txt

COPY app ./app

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
