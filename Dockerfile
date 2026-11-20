FROM ubuntu:24.04 AS bot-api-build

ARG DEBIAN_FRONTEND=noninteractive
ARG TELEGRAM_BOT_API_REF=7232881b39dfcd048a0d569f1983c5184249ee05
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates cmake g++ git gperf libssl-dev make zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*
RUN git init /src \
    && git -C /src remote add origin https://github.com/tdlib/telegram-bot-api.git \
    && git -C /src fetch --depth 1 origin "${TELEGRAM_BOT_API_REF}" \
    && git -C /src checkout --detach FETCH_HEAD \
    && git -C /src submodule update --init --recursive --depth 1 \
    && cmake -S /src -B /src/build -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=/opt/bot-api \
    && cmake --build /src/build --target install --parallel 2

FROM ubuntu:24.04 AS ffmpeg-build

# Pinned release build: Ubuntu's FFmpeg 6.1 cannot read TikTok's legacy HEVC FLV tags.
ADD --checksum=sha256:abb42de254a7ce5ea173913668afc47b10c21867d2b7ebf3c60059684a4f1f95 \
    https://github.com/FFmpeg-Builds/FFmpeg-Builds/releases/download/autobuild-2026-11-19-12-39/ffmpeg-n9.0.2-58-g8c7ef49002-linux64-gpl-9.0.tar.xz /tmp/ffmpeg.tar.xz
RUN apt-get update && apt-get install -y --no-install-recommends xz-utils \
    && mkdir /ffmpeg \
    && tar -xJf /tmp/ffmpeg.tar.xz -C /ffmpeg --strip-components=1

FROM ubuntu:24.04

ARG DEBIAN_FRONTEND=noninteractive
ENV TZ=America/Sao_Paulo \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PLAYWRIGHT_BROWSERS_PATH=/app/tools/browsers \
    PLAYWRIGHT_SKIP_BROWSER_GC=1 \
    HOME=/app/cache/home \
    PATH=/app/.venv/bin:$PATH
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates ffmpeg fonts-dejavu-core libatomic1 libssl3t64 python3 python3-venv tzdata zlib1g \
    && python3 -m venv /app/.venv \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && python -m playwright install --with-deps --no-shell chromium \
    && rm -rf /var/lib/apt/lists/* /root/.cache
COPY --from=bot-api-build /opt/bot-api/bin/telegram-bot-api /app/tools/telegram-bot-api/bin/telegram-bot-api
COPY --from=ffmpeg-build /ffmpeg/bin/ffmpeg /ffmpeg/bin/ffprobe /usr/local/bin/
COPY --from=ffmpeg-build /ffmpeg/LICENSE* /app/licenses/ffmpeg/
RUN mkdir -p tools/ffmpeg/bin \
    && ln -s /usr/local/bin/ffmpeg tools/ffmpeg/bin/ffmpeg \
    && ln -s /usr/local/bin/ffprobe tools/ffmpeg/bin/ffprobe \
    && tools/ffmpeg/bin/ffmpeg -version \
    && tools/telegram-bot-api/bin/telegram-bot-api --version
COPY main.py ./
COPY src/ ./src/
COPY docs/stripchat-mouflon-LICENSE.txt /app/licenses/stripchat-mouflon.txt
COPY tests/ ./tests/
COPY scripts/docker_import.py ./scripts/docker_import.py
COPY docker/entrypoint.sh /usr/local/bin/streamsavebot
RUN sed -i 's/\r$//' /usr/local/bin/streamsavebot \
    && chmod 755 /usr/local/bin/streamsavebot \
    && useradd --uid 10001 --create-home bot \
    && mkdir -p database logs cache/tmp cache/home \
    && chown -R bot:bot database logs cache
USER bot
ENTRYPOINT ["/usr/local/bin/streamsavebot"]
