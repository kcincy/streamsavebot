#!/usr/bin/env bash
set -euo pipefail

cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."

if [[ ! -r /etc/os-release ]]; then
    echo 'Entre no Ubuntu pelo proot-distro antes de executar este script.' >&2
    exit 1
fi
source /etc/os-release
if [[ ${ID:-} != ubuntu || ${VERSION_ID:-} != 24.04 || $EUID -ne 0 ]]; then
    echo 'Execute este script no Ubuntu 24.04 com proot-distro login ubuntu.' >&2
    exit 1
fi

case "$(uname -m)" in
    aarch64)
        platform=linuxarm64
        checksum=6a2f4f8dfe2ee8d346726cc76db774dba786933ed2afe0889451c847f8b08186
        ;;
    x86_64)
        platform=linux64
        checksum=abb42de254a7ce5ea173913668afc47b10c21867d2b7ebf3c60059684a4f1f95
        ;;
    *)
        echo 'Use uma instalação de 64 bits do Termux (aarch64 ou x86_64).' >&2
        exit 1
        ;;
esac

apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    ca-certificates curl fonts-dejavu-core libatomic1 python3 python3-venv xz-utils

python3 -m venv .venv
.venv/bin/python -m pip install --no-cache-dir -r requirements.txt
PLAYWRIGHT_BROWSERS_PATH="$PWD/tools/browsers" PLAYWRIGHT_SKIP_BROWSER_GC=1 \
    DEBIAN_FRONTEND=noninteractive .venv/bin/python -m playwright install --with-deps --no-shell chromium

# Mesmo build do Docker, com suporte ao HEVC legado em FLV do TikTok.
ffmpeg_dir=tools/ffmpeg
ffmpeg_version=n9.0.2-58-g8c7ef49002
if [[ $("$ffmpeg_dir/bin/ffmpeg" -version 2>/dev/null) != *"$ffmpeg_version"* ||
      $("$ffmpeg_dir/bin/ffprobe" -version 2>/dev/null) != *"$ffmpeg_version"* ]]; then
    mkdir -p data/cache "$ffmpeg_dir/bin"
    download_dir=$(mktemp -d "$PWD/data/cache/ffmpeg.XXXXXXXX")
    trap 'rm -rf -- "$download_dir"' EXIT
    archive="ffmpeg-$ffmpeg_version-$platform-gpl-9.0.tar.xz"
    release=https://github.com/FFmpeg-Builds/FFmpeg-Builds/releases/download/autobuild-2026-11-19-12-39
    curl -fsSL --retry 3 "$release/$archive" -o "$download_dir/$archive"
    printf '%s  %s\n' "$checksum" "$download_dir/$archive" | sha256sum --check
    tar -xJf "$download_dir/$archive" -C "$download_dir" --strip-components=1 \
        --wildcards '*/bin/ffmpeg' '*/bin/ffprobe' '*/LICENSE*'
    mv -fT -- "$download_dir/bin/ffmpeg" "$ffmpeg_dir/bin/ffmpeg"
    mv -fT -- "$download_dir/bin/ffprobe" "$ffmpeg_dir/bin/ffprobe"
    cp -- "$download_dir"/LICENSE* "$ffmpeg_dir/"
fi
"$ffmpeg_dir/bin/ffmpeg" -version
"$ffmpeg_dir/bin/ffprobe" -version

if [[ ! -e .env && ! -L .env ]]; then
    (umask 077; set -o noclobber; cat .env.telegram.example > .env)
fi
if [[ ! -e settings.env && ! -L settings.env ]]; then
    (set -o noclobber; cat scripts/termux-settings.env.example > settings.env)
fi
echo 'Instalação concluída. Preencha seu TELEGRAM_BOT_TOKEN no .env e execute .venv/bin/python -u main.py.'
