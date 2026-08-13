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
COPY companions/munchy/server/targets/av1-nvenc/verify-ffmpeg /build/verify-ffmpeg
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


FROM python:3.12-slim@sha256:090ba77e2958f6af52a5341f788b50b032dd4ca28377d2893dcf1ecbdfdfe203 AS python-build

ARG SOURCE_DATE_EPOCH=0
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_LINK_MODE=copy \
    SOURCE_DATE_EPOCH=${SOURCE_DATE_EPOCH}

COPY --from=locked-tools /opt/riverhog-tools/bin/uv /usr/local/bin/uv
WORKDIR /workspace

COPY pyproject.toml uv.lock ./
COPY companions/munchy/server/pyproject.toml companions/munchy/server/pyproject.toml
COPY companions/munchy/server/targets/av1-nvenc companions/munchy/server/targets/av1-nvenc
COPY packages/config-validation packages/config-validation
COPY packages/file-download packages/file-download
COPY packages/http-api-contracts packages/http-api-contracts
COPY packages/lifecycle-events packages/lifecycle-events
COPY packages/munchy-api-client packages/munchy-api-client
COPY packages/munchy-config packages/munchy-config
COPY packages/munchy-target-support packages/munchy-target-support
COPY packages/munchy-workflows packages/munchy-workflows
COPY packages/riverhog-provenance packages/riverhog-provenance
COPY packages/time-formats packages/time-formats
COPY packages/tus-transport packages/tus-transport
RUN uv sync --frozen --package munchy-av1-nvenc-target --no-dev --no-editable


FROM nvidia/cuda:13.0.0-runtime-ubuntu24.04@sha256:95318efecfd68ab3d109da5277863257b06137c84f34a87f38de970d5cd035d3

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    PIP_ROOT_USER_ACTION=ignore \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/opt/venv/bin:$PATH

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl fonts-dejavu-core jq mkvtoolnix python3 rclone tini zstd \
    libfontconfig1 libfreetype6 libfribidi0 libharfbuzz0b libopus0 \
 && ln -s /usr/bin/python3 /usr/local/bin/python3 \
 && python3 -c 'import sys; assert sys.version_info[:2] == (3, 12)' \
 && rm -rf /var/lib/apt/lists/*

COPY --from=ffmpeg-build /opt/ffmpeg /usr/local

COPY --from=python-build /opt/venv /opt/venv

ARG BUILD_CREATED=1970-01-01T00:00:00Z
ARG RELEASE_VERSION=development
ARG SOURCE_REVISION=unknown
LABEL org.opencontainers.image.title="Munchy AV1 NVENC Target" \
      org.opencontainers.image.licenses="CAL-1.0" \
      org.opencontainers.image.source="https://github.com/nashspence/riverhog" \
      org.opencontainers.image.revision="${SOURCE_REVISION}" \
      org.opencontainers.image.version="${RELEASE_VERSION}" \
      org.opencontainers.image.created="${BUILD_CREATED}" \
      org.opencontainers.image.documentation="https://nashspence.github.io/riverhog/v1/"

COPY LICENSES/Apache-2.0.txt /usr/share/licenses/riverhog/Apache-2.0.txt
COPY LICENSES/CAL-1.0.txt /usr/share/licenses/riverhog/CAL-1.0.txt
COPY THIRD_PARTY_NOTICES.md /usr/share/doc/riverhog/THIRD_PARTY_NOTICES.md

EXPOSE 8000
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["munchy-av1-nvenc"]
