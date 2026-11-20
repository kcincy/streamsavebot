"""Compatibilidade real de codecs e preservação do conteúdo já compatível."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from delivery_service import process_clip,prepare_videos,run_media
from project_runtime import media_tool


class TelegramMediaTests(unittest.TestCase):
    def test_mp4_h264_aac_para_h264_opus_vp8_vp9_e_h264_444(self):
        for codec,audio,pixel in (('libx264','aac','yuv420p'),('libx264','libopus','yuv420p'),
                                  ('libvpx','libopus','yuv420p'),('libvpx-vp9','libopus','yuv420p'),
                                  ('libx264','aac','yuv444p')):
            with self.subTest(codec=codec,audio=audio,pixel=pixel),tempfile.TemporaryDirectory() as tmp:
                folder=Path(tmp);raw=folder/'capture.mkv'
                flags=['-preset','ultrafast'] if codec=='libx264' else ['-deadline','realtime','-cpu-used','8','-b:v','250k']
                run_media([media_tool('ffmpeg'),'-v','error','-y','-f','lavfi','-i','testsrc2=size=160x120:rate=10',
                    '-f','lavfi','-i','sine=frequency=440','-t','2','-c:v',codec,'-threads','2',*flags,
                    '-pix_fmt',pixel,'-c:a',audio,str(raw)])
                original=hashlib.sha256(raw.read_bytes()).hexdigest()
                row={'raw_path':str(raw),'metadata':json.dumps({'mime':'incorrect-metadata-is-not-a-codec-check'}),
                     'username':'stripchat:alvo','live_id':'session'}
                outputs=process_clip(row,500_000_000)
                self.assertEqual(len(outputs),1)
                outputs,info=prepare_videos(outputs,500_000_000)
                path,kind=outputs[0]
                self.assertEqual((path.suffix,kind),('.mp4','video'))
                probe=json.loads(run_media([media_tool('ffprobe'),'-v','error','-show_streams','-show_format','-of','json',str(path)]))
                video=next(s for s in probe['streams'] if s['codec_type']=='video')
                sound=next(s for s in probe['streams'] if s['codec_type']=='audio')
                self.assertEqual((video['codec_name'],video['pix_fmt'],sound['codec_name']),('h264','yuv420p','aac'))
                self.assertGreaterEqual(info[str(path)]['duration'],2)
                run_media([media_tool('ffmpeg'),'-v','error','-xerror','-i',str(path),'-f','null','-'])
                self.assertEqual(hashlib.sha256(raw.read_bytes()).hexdigest(),original)
                if codec=='libx264' and pixel=='yuv420p':
                    def video_hash(source):
                        return run_media([media_tool('ffmpeg'),'-v','error','-i',str(source),'-map','0:v:0',
                                          '-c','copy','-f','hash','-hash','sha256','-'])
                    self.assertEqual(video_hash(raw),video_hash(path),'H.264 compatível não deve ser recodificado')
