FROM nvidia/cuda:13.0.0-devel-ubuntu24.04 AS ffmpeg-build

ARG FFMPEG_REF=8885116f4dbe10527d4e7a14dc691b07e3915d55
ARG NV_CODEC_HEADERS_REF=33a9ede8d9914299d9262539c576a15bd0a19621
ARG FFMPEG_NVCCFLAGS="-gencode arch=compute_120,code=sm_120 -O2"

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential ca-certificates git make pkg-config yasm nasm \
    libfreetype6-dev libharfbuzz-dev libfontconfig1-dev libfribidi-dev \
    libopus-dev zlib1g-dev \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /build
RUN git clone https://git.videolan.org/git/ffmpeg/nv-codec-headers.git \
 && cd nv-codec-headers \
 && git checkout "${NV_CODEC_HEADERS_REF}" \
 && make install

RUN git clone https://git.ffmpeg.org/ffmpeg.git \
 && cd ffmpeg \
 && git checkout "${FFMPEG_REF}" \
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
      --enable-nonfree \
      --enable-libfreetype \
      --enable-libharfbuzz \
      --enable-libfontconfig \
      --enable-libfribidi \
      --enable-libopus \
      --enable-cuda-nvcc \
      --enable-cuvid \
      --enable-nvdec \
      --enable-nvenc \
      --nvccflags="${FFMPEG_NVCCFLAGS}" \
 && make -j"$(nproc)" \
 && make install


FROM nvidia/cuda:13.0.0-runtime-ubuntu24.04

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    PIP_ROOT_USER_ACTION=ignore \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl fonts-dejavu-core jq mkvtoolnix python3 python3-pip rclone tini zstd \
    libfontconfig1 libfreetype6 libfribidi0 libharfbuzz0b libopus0 \
 && rm -rf /var/lib/apt/lists/*

COPY --from=ffmpeg-build /opt/ffmpeg /usr/local

WORKDIR /app

COPY services/munchy-av1-nvenc/requirements.txt .
RUN python3 -m pip install --break-system-packages --no-cache-dir -r requirements.txt

COPY pyproject.toml README.md /riverhog/
COPY contracts /riverhog/contracts
COPY src /riverhog/src
RUN python3 -m pip install --break-system-packages --no-cache-dir /riverhog

COPY services/munchy-av1-nvenc/app app

EXPOSE 8000
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python3", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
