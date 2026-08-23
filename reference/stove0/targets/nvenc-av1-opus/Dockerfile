FROM jdxcode/mise:2026.7.13@sha256:7fe9145156e33f95f7712632dbadb418592ed0f60fb46e5bcc8c113d372ad8a3 AS mise

FROM python:3.12-slim@sha256:090ba77e2958f6af52a5341f788b50b032dd4ca28377d2893dcf1ecbdfdfe203 AS locked-tools

COPY --from=mise /usr/local/bin/mise /usr/local/bin/mise
WORKDIR /workspace
COPY mise.toml mise.lock ./
RUN mise trust mise.toml \
    && mise install --locked uv \
    && mkdir -p /opt/riverhog-tools/bin \
    && cp --preserve=mode,timestamps "$(mise which uv)" /opt/riverhog-tools/bin/uv \
    && test "$(/opt/riverhog-tools/bin/uv --version)" = "$(mise exec uv -- uv --version)"

FROM nvidia/cuda:13.0.0-devel-ubuntu24.04@sha256:1e8ac7a54c184a1af8ef2167f28fa98281892a835c981ebcddb1fad04bdd452d AS ffmpeg-build

ARG SOURCE_DATE_EPOCH=0
ARG FFMPEG_REF=8885116f4dbe10527d4e7a14dc691b07e3915d55
ARG NV_CODEC_HEADERS_REF=33a9ede8d9914299d9262539c576a15bd0a19621
ARG FFMPEG_CUDA_FLAGS="--cuda-gpu-arch=sm_120 -O2"

ENV DEBIAN_FRONTEND=noninteractive \
    SOURCE_DATE_EPOCH=${SOURCE_DATE_EPOCH}

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential ca-certificates clang-20 git make pkg-config yasm nasm \
    libfreetype6-dev libharfbuzz-dev libfontconfig1-dev libfribidi-dev \
    libopus-dev zlib1g-dev \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY reference/stove0/targets/nvenc-av1-opus/verify-ffmpeg /build/verify-ffmpeg
RUN git init nv-codec-headers \
 && cd nv-codec-headers \
 && git remote add origin https://code.ffmpeg.org/FFmpeg/nv-codec-headers.git \
 && git -c http.version=HTTP/1.1 fetch --depth 1 origin "${NV_CODEC_HEADERS_REF}" \
 && git checkout --detach FETCH_HEAD \
 && test "$(git rev-parse HEAD)" = "${NV_CODEC_HEADERS_REF}" \
 && make install

RUN git init ffmpeg \
 && cd ffmpeg \
 && git remote add origin https://code.ffmpeg.org/FFmpeg/FFmpeg.git \
 && git -c http.version=HTTP/1.1 fetch --depth 1 origin "${FFMPEG_REF}" \
 && git checkout --detach FETCH_HEAD \
 && test "$(git rev-parse HEAD)" = "${FFMPEG_REF}" \
 && PKG_CONFIG_PATH=/usr/local/lib/pkgconfig ./configure \
      --prefix=/opt/ffmpeg \
      --pkg-config-flags=--static \
      --extra-cflags=-I/usr/local/cuda/include \
      --extra-ldflags=-L/usr/local/cuda/lib64 \
      --disable-debug \
      --disable-doc \
      --disable-ffplay \
      --disable-shared \
      --enable-static \
      --enable-gpl \
      --enable-libfreetype \
      --enable-libharfbuzz \
      --enable-libfontconfig \
      --enable-libfribidi \
      --enable-libopus \
      --enable-cuda-llvm \
      --enable-cuvid \
      --enable-nvdec \
      --enable-nvenc \
      --nvcc=clang-20 \
      --nvccflags="${FFMPEG_CUDA_FLAGS}" \
 && make -j"$(nproc)" \
 && make install \
 && sh /build/verify-ffmpeg /opt/ffmpeg/bin/ffmpeg

RUN mkdir -p \
      "/opt/ffmpeg/share/licenses/riverhog-third-party/ffmpeg/${FFMPEG_REF}" \
      "/opt/ffmpeg/share/licenses/riverhog-third-party/nv-codec-headers/${NV_CODEC_HEADERS_REF}" \
 && cp /build/ffmpeg/COPYING.GPLv2 \
      "/opt/ffmpeg/share/licenses/riverhog-third-party/ffmpeg/${FFMPEG_REF}/COPYING.GPLv2" \
 && header_count="$(find /build/nv-codec-headers/include/ffnvcodec -maxdepth 1 -type f -name '*.h' | wc -l)" \
 && test "${header_count}" -gt 0 \
 && { \
      for header in /build/nv-codec-headers/include/ffnvcodec/*.h; do \
        printf '===== %s =====\n' "$(basename "${header}")"; \
        awk '1; /\*\// { exit }' "${header}"; \
      done; \
    } > "/opt/ffmpeg/share/licenses/riverhog-third-party/nv-codec-headers/${NV_CODEC_HEADERS_REF}/ATTRIBUTION" \
 && test "$(grep -c 'Permission is hereby granted' \
      "/opt/ffmpeg/share/licenses/riverhog-third-party/nv-codec-headers/${NV_CODEC_HEADERS_REF}/ATTRIBUTION")" \
      = "${header_count}"

FROM python:3.12-slim@sha256:090ba77e2958f6af52a5341f788b50b032dd4ca28377d2893dcf1ecbdfdfe203 AS python-build

ARG SOURCE_DATE_EPOCH=0
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_LINK_MODE=copy \
    SOURCE_DATE_EPOCH=${SOURCE_DATE_EPOCH}

COPY --from=locked-tools /opt/riverhog-tools/bin/uv /usr/local/bin/uv
WORKDIR /workspace
COPY pyproject.toml uv.lock ./
COPY reference/stove0/targets/nvenc-av1-opus/target reference/stove0/targets/nvenc-av1-opus/target
COPY reference/stove0/targets/nvenc-av1-opus/review-sampler reference/stove0/targets/nvenc-av1-opus/review-sampler
COPY packages/file-download packages/file-download
COPY packages/http-api-contracts packages/http-api-contracts
COPY packages/lifecycle-events packages/lifecycle-events
COPY packages/riverhog-api-client packages/riverhog-api-client
COPY packages/riverhog-protocol packages/riverhog-protocol
COPY packages/riverhog-provenance packages/riverhog-provenance
COPY packages/riverhog-transform-sdk packages/riverhog-transform-sdk
COPY packages/stove0-media-archive-contracts packages/stove0-media-archive-contracts
COPY packages/stove0-observer-protocol packages/stove0-observer-protocol
COPY packages/stove0-protocol packages/stove0-protocol
COPY packages/stove0-review-contracts packages/stove0-review-contracts
COPY packages/stove0-review-sampler-client packages/stove0-review-sampler-client
COPY packages/stove0-review-sampler-protocol packages/stove0-review-sampler-protocol
COPY packages/stove0-review-sampler-support packages/stove0-review-sampler-support
COPY packages/stove0-target-client packages/stove0-target-client
COPY packages/stove0-target-protocol packages/stove0-target-protocol
COPY packages/stove0-target-support packages/stove0-target-support
COPY packages/time-formats packages/time-formats
RUN uv sync --frozen --package stove0-nvenc-av1-opus-target --package stove0-nvenc-av1-opus-review-sampler --no-dev --no-editable

FROM nvidia/cuda:13.0.0-runtime-ubuntu24.04@sha256:95318efecfd68ab3d109da5277863257b06137c84f34a87f38de970d5cd035d3

ARG SOURCE_DATE_EPOCH=0
ENV DEBIAN_FRONTEND=noninteractive \
    PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SOURCE_DATE_EPOCH=${SOURCE_DATE_EPOCH}

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates fonts-dejavu-core mkvtoolnix python3 tini zstd \
    libfontconfig1 libfreetype6 libfribidi0 libharfbuzz0b libopus0 \
 && ln -s /usr/bin/python3 /usr/local/bin/python3 \
 && python3 -c 'import sys; assert sys.version_info[:2] == (3, 12)' \
 && install -d -o 65532 -g 65532 -m 0700 /var/lib/stove0-nvenc-av1-opus-target \
 && rm -rf /var/lib/apt/lists/* \
 && ! command -v mise

COPY --from=ffmpeg-build /opt/ffmpeg /usr/local
COPY --from=python-build /opt/venv /opt/venv

ARG BUILD_CREATED=1970-01-01T00:00:00Z
ARG RELEASE_VERSION=development
ARG SOURCE_REVISION=unknown
LABEL org.opencontainers.image.title="stove0 NVENC AV1 + Opus target" \
      org.opencontainers.image.licenses="CAL-1.0" \
      org.opencontainers.image.source="https://github.com/nashspence/riverhog" \
      org.opencontainers.image.revision="${SOURCE_REVISION}" \
      org.opencontainers.image.version="${RELEASE_VERSION}" \
      org.opencontainers.image.created="${BUILD_CREATED}" \
      org.opencontainers.image.documentation="https://nashspence.github.io/riverhog/v1/"

COPY LICENSES/Apache-2.0.txt /usr/share/licenses/riverhog/Apache-2.0.txt
COPY LICENSES/CAL-1.0.txt /usr/share/licenses/riverhog/CAL-1.0.txt
COPY THIRD_PARTY_NOTICES.md /usr/share/doc/riverhog/THIRD_PARTY_NOTICES.md

USER 65532:65532
EXPOSE 8080
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["stove0-nvenc-av1-opus-target", "--host", "0.0.0.0", "--port", "8080"]
