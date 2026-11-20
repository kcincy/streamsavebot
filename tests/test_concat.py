"""Testa o caminho de concat/remux (reconexao) sem depender da rede.

Gera um clipe sintetico com ffmpeg, corta em 2 segmentos e verifica que
_finalize() junta tudo, remuxa e limpa os temporarios.

    python tests/test_concat.py
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from recorder import Live, StreamRecorder  # noqa: E402


def ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


def probe(path: Path) -> tuple[list[str], float]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_name",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout.split()
    codecs = [line for line in out if not line.replace(".", "").isdigit()]
    duration = float([line for line in out if line.replace(".", "").isdigit()][-1])
    return codecs, duration


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        src = out / "src.mkv"
        ffmpeg(
            "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=30:duration=10",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=10",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "libopus", "-shortest", "-f", "matroska", str(src),
        )

        for fmt, expected_ext in (("mp4", ".mp4"), ("mkv", ".mkv"), ("webm", ".mkv")):
            parts = []
            for i, (start, length) in enumerate([(0, 5), (5, 5)], start=1):
                p = out / f"seg_{fmt}.part{i:02d}.mkv"
                ffmpeg("-i", str(src), "-ss", str(start), "-t", str(length),
                       "-c", "copy", "-f", "matroska", str(p))
                parts.append(p)

            rec = StreamRecorder(Live("teste", "123", "t", 0, "BR"), out, fmt=fmt)
            rec.segments = parts
            rec.mime = "video/webm;codecs=h264,opus"
            final = rec._finalize()

            assert final is not None and final.exists(), f"[{fmt}] nada foi gerado"
            assert final.suffix == expected_ext, f"[{fmt}] extensao {final.suffix} != {expected_ext}"
            codecs, duration = probe(final)
            assert "h264" in codecs, f"[{fmt}] video inesperado: {codecs}"
            assert {"aac", "opus"} & set(codecs), f"[{fmt}] audio inesperado: {codecs}"
            assert duration > 9.5, f"[{fmt}] duracao curta: {duration}"
            assert not [p for p in parts if p.exists()], f"[{fmt}] temporarios nao limpos"
            print(f"ok {fmt}: {final.name} {codecs} {duration:.2f}s {final.stat().st_size}B")

        # O passthrough pode receber VP8; MP4 precisa de conversao para H.264.
        vp8 = out / "vp8.part01.mkv"
        ffmpeg("-i", str(src), "-t", "3", "-c:v", "libvpx", "-deadline", "realtime",
               "-c:a", "copy", str(vp8))
        rec = StreamRecorder(Live("vp8", "456", "t", 0, "BR"), out, fmt="mp4")
        rec.segments = [vp8]
        rec.mime = "video/webm;codecs=vp8,opus"
        final = rec._finalize()
        assert final is not None and final.suffix == ".mp4"
        codecs, duration = probe(final)
        assert "h264" in codecs and "aac" in codecs, codecs
        assert duration >= 2.9, duration
        assert not vp8.exists()
        print(f"ok VP8 -> MP4: {codecs} {duration:.2f}s")

        # MediaRecorder pode criar audio antes do video, variando entre reconexoes.
        # O concat associa streams por posicao; ambos precisam da mesma ordem.
        for fmt in ("mkv", "mp4"):
            parts = []
            for i, maps in enumerate((("0:a:0", "0:v:0"), ("0:v:0", "0:a:0")), 1):
                part = out / f"order-{fmt}-{i}.mkv"
                ffmpeg("-i", str(src), "-t", "3", "-map", maps[0], "-map", maps[1],
                       "-c", "copy", "-live", "1", str(part))
                parts.append(part)
            rec = StreamRecorder(Live("order", fmt, "", 0, ""), out, fmt=fmt)
            rec.segments = parts
            rec.mime = "video/x-matroska;codecs=avc1,opus"
            final = rec._finalize()
            assert final is not None and final.suffix == "." + fmt
            decoded = json.loads(subprocess.check_output([
                "ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
                "-show_entries", "stream=nb_read_frames", "-of", "json", str(final),
            ]))
            assert int(decoded["streams"][0]["nb_read_frames"]) >= 175, decoded
            ffmpeg("-xerror", "-i", str(final), "-f", "null", "-")
            assert 5.9 <= probe(final)[1] <= 6.3
            print(f"ok {fmt}: ordem audio/video invertida, todos os quadros decodificados")


        # Segmentos com resolucoes diferentes devem virar um unico video estavel.
        low = out / "low.part01.mkv"
        high = out / "high.part02.mkv"
        ffmpeg("-i", str(src), "-t", "3", "-c", "copy", str(low))
        ffmpeg("-i", str(src), "-t", "3", "-vf", "scale=640:360",
               "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "copy", str(high))
        rec = StreamRecorder(Live("resize", "789", "t", 0, "BR"), out, fmt="mp4")
        rec.segments = [low, high]
        rec.video_size = (320, 240)
        rec.normalize_video = True
        rec.mime = "video/x-matroska;codecs=avc1,opus"
        final = rec._finalize()
        assert final is not None and final.suffix == ".mp4"
        frames = subprocess.run([
            "ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
            "frame=width,height", "-of", "csv=p=0", str(final),
        ], capture_output=True, text=True, check=True).stdout.splitlines()
        sizes = {line.strip(",") for line in frames if line.strip(",")}
        assert sizes == {"320,240"}, sizes
        assert probe(final)[1] >= 5.9
        assert not low.exists() and not high.exists()
        print("ok resolucao fixa: 320x240, dois segmentos preservados")

        # Uma falha no concat nao pode apagar segmentos posteriores ao primeiro.
        failed = [out / "fail1.mkv", out / "fail2.mkv"]
        for part in failed:
            part.write_bytes(b"original")
        rec = StreamRecorder(Live("fail", "999", "", 0, ""), out)
        rec.segments = failed
        rec._ffmpeg = lambda args: False
        assert rec._finalize() is None
        assert all(p.read_bytes() == b"original" for p in failed)
        print("ok falha de concat preserva todos os originais")

    print("todos os formatos ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
