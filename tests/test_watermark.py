"""Real encodes: centered text, time boundary, original frames/audio and safe literal text."""
from contextlib import closing
import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from config import Config
from delivery_service import DeliveryService,process_clip,run_media,probe_video,video_info,watermark_outputs,PartTooLarge,bounded_output
from storage import Store
from project_runtime import media_tool


class WatermarkTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)

    def source(self,seconds=35):
        path=self.root/'original.mp4'
        run_media([media_tool('ffmpeg'),'-v','error','-nostdin','-y','-f','lavfi','-i','color=gray:s=320x180:r=10',
            '-f','lavfi','-i','sine=frequency=440:sample_rate=48000','-t',str(seconds),'-c:v','libx264',
            '-threads','1','-preset','ultrafast','-pix_fmt','yuv420p','-c:a','aac',str(path)])
        return path

    def frame(self,path,seconds):
        return run_media([media_tool('ffmpeg'),'-v','error','-threads','1','-ss',str(seconds),'-i',str(path),
                          '-frames:v','1','-pix_fmt','gray','-f','rawvideo','pipe:1'])

    def visible(self,path,seconds):
        data=self.frame(path,seconds);self.assertEqual(len(data),320*180)
        points=[(x,y) for y in range(180) for x in range(320) if data[y*320+x]>180]
        if points:
            self.assertAlmostEqual((min(x for x,y in points)+max(x for x,y in points))/2,160,delta=6)
            self.assertAlmostEqual((min(y for x,y in points)+max(y for x,y in points))/2,90,delta=6)
        return bool(points)

    def test_center_and_30_second_boundary_keep_frames_resolution_and_audio(self):
        source=self.source()
        original=probe_video(source)
        output=process_clip(dict(raw_path=str(source),metadata='{"seconds":35}'),300_000_000,
                            watermark_text='teste',output_folder=self.root/'marked')[0][0]
        final=probe_video(output)
        before=next(s for s in original['streams'] if s['codec_type']=='video')
        after=next(s for s in final['streams'] if s['codec_type']=='video')
        for key in ('width','height','r_frame_rate','nb_read_packets'):self.assertEqual(before[key],after[key])
        def timestamps(path):
            data=json.loads(run_media([media_tool('ffprobe'),'-v','error','-select_streams','v:0',
                '-show_packets','-show_entries','packet=pts_time','-of','json',str(path)]))
            return [float(p['pts_time']) for p in data['packets']]
        # AAC priming can shift timestamps by one audio packet without dropping video frames.
        for a,b in zip(timestamps(source),timestamps(output)):self.assertLessEqual(abs(a-b),.025)
        self.assertEqual({s['codec_name'] for s in final['streams']},{'h264','aac'})
        self.assertTrue(self.visible(output,0));self.assertTrue(self.visible(output,29.9))
        self.assertFalse(self.visible(output,30));self.assertFalse(self.visible(output,34))
        self.assertFalse(self.visible(source,1));self.assertTrue(source.exists())
        run_media([media_tool('ffmpeg'),'-v','error','-xerror','-threads','1','-i',str(output),'-f','null','-'])

    def test_text_and_paths_are_literal_not_filter_expressions(self):
        source=self.source(1);folder=self.root/"quote's [x],;"
        text="100% %{n}: ' [x],; café"
        outputs=process_clip(dict(raw_path=str(source),metadata='{}'),300_000_000,watermark_text=text,output_folder=folder)
        self.assertTrue(outputs[0][0].is_file())
        self.assertEqual((folder/'watermark.txt').read_text(encoding='utf-8'),text)

    def test_each_split_file_gets_its_own_30_seconds(self):
        source=self.source(70);calls=0
        def force_split(*args):
            nonlocal calls
            calls+=1
            if calls==1:raise PartTooLarge()
            return bounded_output(*args)
        with patch('delivery_service.bounded_output',side_effect=force_split):
            outputs=process_clip(dict(raw_path=str(source),metadata='{"seconds":70}'),300_000_000,
                                 watermark_text='teste',output_folder=self.root/'marked')
        self.assertEqual(len(outputs),2)
        for path,kind in outputs:
            self.assertTrue(self.visible(path,1));self.assertFalse(self.visible(path,31))
            self.assertLessEqual(path.stat().st_size,300_000_000)

    def test_disabled_setting_uses_original_copy_path(self):
        source=self.source(1)
        with patch('delivery_service.watermark_filter',side_effect=AssertionError):
            outputs=process_clip(dict(raw_path=str(source),metadata='{}'),300_000_000,output_folder=self.root/'clean')
        self.assertTrue(outputs[0][0].exists())

    def test_short_clips_that_will_be_skipped_do_not_use_cpu_for_watermark(self):
        source=self.source(1)
        with patch('delivery_service.process_clip',side_effect=AssertionError):
            outputs=watermark_outputs([(source,'video')],{str(source):dict(duration_seconds=1)},
                                      'teste',300_000_000,self.root,16*1024**3)
        self.assertEqual(outputs,[(source,'video')]);self.assertTrue(source.exists())

    def test_config_setting_and_invalid_control_characters(self):
        env={'TELEGRAM_BOT_TOKEN':'test','WATERMARK_TEXT':'teste'}
        with patch('config.dotenv_values',return_value={}),patch.dict(os.environ,env,clear=True):
            self.assertEqual(Config.from_env().watermark_text,'teste')
        for text in ('x\ny','a'*201):
            with patch('config.dotenv_values',return_value={}),patch.dict(os.environ,dict(env,WATERMARK_TEXT=text),clear=True):
                with self.assertRaises(ValueError):Config.from_env()
        for raw,enabled in (('true',True),('false',False),(' TRUE ',True)):
            with patch('config.dotenv_values',return_value={}),patch.dict(os.environ,dict(env,WATERMARK_ENABLED=raw),clear=True):
                config=Config.from_env()
                self.assertEqual(config.watermark_enabled,enabled);self.assertEqual(config.watermark_active,enabled)
                self.assertEqual(config.watermark_text,'teste')
        with patch('config.dotenv_values',return_value={}),patch.dict(os.environ,dict(env,WATERMARK_ENABLED='yes'),clear=True):
            with self.assertRaises(ValueError):Config.from_env()

    def test_variable_frame_timestamps_are_not_rounded_into_duplicate_dts(self):
        source=self.root/'variable.mkv'
        run_media([media_tool('ffmpeg'),'-v','error','-f','lavfi','-i','testsrc2=s=320x180:r=30:d=2',
            '-vf',"setpts='PTS+mod(N,2)*0.020/TB'",'-c:v','libx264','-threads','1','-preset','ultrafast',
            '-bf','0','-fps_mode:v','passthrough','-enc_time_base:v','1/1000',str(source)])
        output=process_clip(dict(raw_path=str(source),metadata='{}'),300_000_000,
                            watermark_text='teste',output_folder=self.root/'marked')[0][0]
        before=next(s for s in probe_video(source)['streams'] if s['codec_type']=='video')
        after=next(s for s in probe_video(output)['streams'] if s['codec_type']=='video')
        self.assertEqual(before['nb_read_packets'],after['nb_read_packets'])
        run_media([media_tool('ffmpeg'),'-v','error','-xerror','-threads','1','-i',str(output),'-f','null','-'])


class WatermarkPipelineTests(unittest.IsolatedAsyncioTestCase):
    frame=WatermarkTests.frame
    visible=WatermarkTests.visible
    source=WatermarkTests.source

    async def test_queue_waits_for_resources_preserves_capture_and_sheet_is_unmarked(self):
        await self.exercise(True)

    async def test_disabled_boolean_skips_encoding_and_resource_wait_even_with_text_configured(self):
        await self.exercise(False)

    async def exercise(self,enabled):
        with tempfile.TemporaryDirectory() as tmp:
            self.root=Path(tmp)
            config=replace(Config('test'),database=self.root/'database/bot.sqlite3',spool=self.root/'spool',
                           watermark_text='teste',watermark_enabled=enabled,part_bytes=300_000_000)
            store=Store(config.database);store.add(1,1,'alvo')
            service=DeliveryService(config,store,SimpleNamespace());service.can_process=lambda:False
            ident='a'*32;folder=config.spool/ident;folder.mkdir();raw=folder/'capture.mp4'
            self.source().replace(raw)
            store.register_clip(ident,'alvo','live',1,raw,dict(seconds=35,profile_id='111'),3600)
            worker=asyncio.create_task(service._process_loop())
            try:
                await asyncio.sleep(.25)
                if enabled:
                    self.assertEqual(store.db.execute('SELECT status FROM clips').fetchone()[0],'raw')
                    service.can_process=lambda:True
                async def wait():
                    while store.db.execute('SELECT status FROM clips').fetchone()[0]=='raw' or service.processing:
                        await asyncio.sleep(.05)
                await asyncio.wait_for(wait(),30)
                self.assertEqual(store.db.execute('SELECT status FROM clips').fetchone()[0],'ready')
                output=Path(store.db.execute('SELECT path FROM media').fetchone()[0])
                self.assertEqual(self.visible(output,1),enabled);self.assertFalse(self.visible(output,31))
                self.assertTrue(raw.is_file());self.assertFalse(self.visible(raw,1))
                sheet=service.sheets/'images'/(ident+'.jpg')
                data=run_media([media_tool('ffmpeg'),'-v','error','-threads','1','-i',str(sheet),
                                '-frames:v','1','-pix_fmt','gray','-f','rawvideo','pipe:1'])
                self.assertEqual(len(data),1440*810);self.assertLess(max(data),180)
            finally:
                service.closed=True;await worker;store.close()


if __name__=='__main__':unittest.main()
