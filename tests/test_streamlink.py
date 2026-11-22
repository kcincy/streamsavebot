"""Official plugins with controlled HTTP; real packet-copy media and bounded fragments."""
import io
from functools import partial
from http.server import SimpleHTTPRequestHandler,ThreadingHTTPServer
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit
import uuid

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from requests import Response
from requests.adapters import BaseAdapter
from streamlink import Streamlink
from streamlink.exceptions import StreamError
from streamlink.plugins.kick import Kick
from streamlink.stream.hls import HLSStream
from recorder import Live
from platforms import profile_key,profile_url
from streamlink_recorder import StreamlinkRecorder,best_stream,read_box,read_flv_tag,MAX_BOX
from stripchat_capture import IdentityError
from project_runtime import media_tool
from delivery_service import run_media,process_clip,prepare_videos,media_duration


class Fixtures(BaseAdapter):
    def __init__(self,responses):self.responses=responses;self.urls=[]
    def send(self,request,**kwargs):
        self.urls.append(request.url);key=urlsplit(request.url).path
        if key not in self.responses:raise AssertionError('Unexpected HTTP request: '+key)
        data=self.responses[key]
        response=Response();response.status_code=200;response.request=request;response.url=request.url
        response._content=(data if isinstance(data,str) else json.dumps(data)).encode()
        response._content_consumed=True
        return response
    def close(self):pass


class StreamlinkTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)

    def recorder(self,platform,uid=''):
        rec=StreamlinkRecorder(Live('tester','','',0,'',user_id=uid),self.root,platform=platform,
            stop_event=threading.Event(),segment_callback=lambda *args:None)
        def base():
            folder=self.root/uuid.uuid4().hex;folder.mkdir();return folder/'capture'
        rec._recording_base=base
        return rec

    def session(self,responses):
        session=Streamlink();adapter=Fixtures(responses)
        session.plugins._plugins['kick']=Kick
        session.http.mount('https://',adapter);self.addCleanup(session.http.close)
        return session,adapter

    def tiktok_api(self,quality='origin',uid='111',name='tester',status=2):
        stream=json.dumps({'data':{quality:{'main':{'flv':'https://cdn.invalid/live.flv',
            'sdk_params':json.dumps({'VCodec':'h264','vbitrate':3000000})}}}})
        return {'statusCode':0,'data':{'user':{'id':uid,'uniqueId':name},
            'liveRoom':{'status':status,'streamId':'1234','title':'Live title',
                        'streamData':{'pull_data':{'stream_data':stream}}}}}

    def tiktok_profile_page(self, room_id='9876'):
        user={'id':'111','uniqueId':'tester','roomId':room_id}
        payload={'__DEFAULT_SCOPE__':{'webapp.user-detail':{'userInfo':{'user':user}}}}
        return f'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" type="application/json">{json.dumps(payload)}</script>'

    def test_urls_and_best_available_quality(self):
        for url,key in [('https://kick.com/Tester','kick:tester'),('https://www.tiktok.com/@Tester/live','tiktok:tester'),
                        ('https://www.tiktok.com/@Tester','tiktok:tester'),('tiktok:@tester','tiktok:tester')]:
            self.assertEqual(profile_key(url),key)
            self.assertEqual(profile_key(profile_url(key)),key)
        for url in ('https://kick.com.evil.invalid/tester','https://kick.com@evil.invalid/tester',
                    'https://kick.com:8080/tester','https://kick.com/tester/videos/123',
                    'https://www.tiktok.com/@tester/video/123','https://evil.tiktok.com/@tester/live'):
            with self.assertRaises(ValueError,msg=url):profile_key(url)
        source,low=object(),object()
        self.assertIs(best_stream('tiktok',{'h264_origin':source,'best':low}),source)
        self.assertIs(best_stream('tiktok',{'h264_hd':low,'best':low}),low)
        self.assertIs(best_stream('tiktok',{'h264_sd':low,'h264_hd':source}),source)
        self.assertIsNone(best_stream('tiktok',{'best':low}))
        self.assertIs(best_stream('kick',{'source':source,'best':low}),source)

    def test_tiktok_configuration_change_produces_independently_decodable_original_parts(self):
        import subprocess
        sources=[]
        for index,size in enumerate(('432x864','720x1280')):
            source=self.root/f'source{index}.flv'
            run_media([media_tool('ffmpeg'),'-v','error','-f','lavfi','-i',f'testsrc2=size={size}:rate=15',
                '-f','lavfi','-i','sine=frequency=440:sample_rate=48000','-t','2','-c:v','libx264',
                '-threads','2','-preset','ultrafast','-pix_fmt','yuv420p','-g','15','-c:a','aac','-f','flv',str(source)])
            sources.append(source.read_bytes())
        rec=self.recorder('tiktok');rec.source_suffix='.flv';rec.part_limit=300_000_000
        published=[];rec.segment_callback=lambda path,meta:published.append((path,meta))
        # Shift the second source's timestamps, as in a continuous live with new AVC headers.
        shifted=bytearray();stream=io.BytesIO(sources[1][13:])
        while tag:=read_flv_tag(stream):
            kind,payload,raw=tag
            value=bytearray(raw);timestamp=int.from_bytes(value[4:7],'big')+(value[7]<<24)+2000
            value[4:7]=(timestamp&0xffffff).to_bytes(3,'big');value[7]=timestamp>>24
            shifted.extend(value)
        joined=self.root/'changed.flv';joined.write_bytes(sources[0]+shifted)
        rec.capture(type('FileStream',(),{'open':lambda _:joined.open('rb')})())
        self.assertEqual(len(published),2)
        sizes=[]
        for path,meta in published:
            probe=json.loads(run_media([media_tool('ffprobe'),'-v','error','-show_streams','-of','json',str(path)]))
            video=next(s for s in probe['streams'] if s['codec_type']=='video')
            sizes.append((video['width'],video['height']))
            result=subprocess.run([media_tool('ffmpeg'),'-v','error','-xerror','-threads','2','-i',str(path),'-f','null','-'],capture_output=True)
            self.assertEqual(result.returncode,0,result.stderr.decode(errors='replace'))
            outputs=process_clip(dict(raw_path=str(path),metadata=json.dumps(meta)),300_000_000,self.root,2_000_000_000)
            for output,_ in outputs:
                result=subprocess.run([media_tool('ffmpeg'),'-v','error','-xerror','-threads','2','-i',str(output),'-f','null','-'],capture_output=True)
                self.assertEqual(result.returncode,0,result.stderr.decode(errors='replace'))
        self.assertEqual(sizes,[(432,864),(720,1280)])

    def test_flv_rejects_truncated_or_oversized_tags(self):
        with self.assertRaises(EOFError):read_flv_tag(io.BytesIO(b'bad'))
        with self.assertRaises(ValueError):read_flv_tag(io.BytesIO(b'\x09'+(MAX_BOX+1).to_bytes(3,'big')+b'\0'*7))

    def test_source_that_stops_delivering_cannot_leave_ffmpeg_blocked_forever(self):
        source=self.root/'stalls.flv'
        run_media([media_tool('ffmpeg'),'-v','error','-f','lavfi','-i','testsrc2=size=720x1280:rate=10',
            '-t','6','-c:v','libx264','-threads','2','-preset','ultrafast','-g','10','-f','flv',str(source)])
        class Stalled:
            def __init__(self):self.buffer=io.BytesIO(source.read_bytes());self.closed=threading.Event()
            def read(self,size):
                data=self.buffer.read(size)
                if not data:self.closed.wait(15)
                return data
            def close(self):self.closed.set()
        stalled=Stalled();rec=self.recorder('tiktok');rec.stall_timeout=.1
        started=time.monotonic()
        try:
            with self.assertRaises((RuntimeError,EOFError,ValueError)):
                rec.capture(type('FileStream',(),{'open':lambda _:stalled})())
        finally:stalled.close()
        self.assertLess(time.monotonic()-started,12)
        self.assertIsNone(rec.proc);self.assertIsNone(rec.input)

    def test_tiktok_web_api_identity_title_and_best_available_quality(self):
        for quality,uid,name,expected in [('origin','111','tester','public'),('hd','111','tester','public'),
                                         ('origin','222','tester',IdentityError),('origin','111','other',IdentityError),
                                         ('origin','','tester','unavailable')]:
            with self.subTest(quality=quality,uid=uid,name=name):
                session,adapter=self.session({'/api-live/user/room':self.tiktok_api(quality,uid,name),
                    '/@tester':'<html></html>','/@tester/live':'<html></html>'})
                rec=self.recorder('tiktok','111')
                with patch('streamlink_recorder.Streamlink',return_value=session),patch('streamlink_recorder.sync_playwright',side_effect=AssertionError('No browser')):
                    if isinstance(expected,type):
                        with self.assertRaises(expected):rec.discover()
                    else:
                        stream,_,live,access=rec.discover()
                        self.assertEqual(access,expected)
                        self.assertNotIn('staleTime=',adapter.urls[0])
                        if not uid:
                            self.assertIsNone(stream);self.assertIsNone(live)
                            continue
                        self.assertEqual(live,'1234')
                        self.assertEqual(rec.live.title,'Live title');self.assertEqual(rec.live.user_id,'111')
                        self.assertIsNotNone(stream)
        session,_=self.session({'/api-live/user/room':self.tiktok_api(status=4),
            '/@tester':self.tiktok_profile_page('0'),'/@tester/live':'<html></html>'})
        with patch('streamlink_recorder.Streamlink',return_value=session):
            self.assertEqual(self.recorder('tiktok').discover()[-1],'offline')

    def test_tiktok_confirms_room_instead_of_trusting_stale_offline(self):
        detail={'status_code':0,'data':{'id_str':'9876','owner_user_id_str':'111',
            'status':2,'title':'Live now','stream_id_str':'12345',
            'stream_url':{'flv_pull_url':{'ORIGION':'https://cdn.invalid/original.flv',
                                         'HD1':'https://cdn.invalid/low.flv'},
                          'flv_pull_url_params':{'ORIGION':'{"VCodec":"hevc"}'}}}}
        responses={'/api-live/user/room':self.tiktok_api(status=4),
                   '/@tester':self.tiktok_profile_page(),'/@tester/live':'<html></html>','/webcast/room/info/':detail}
        session,adapter=self.session(responses);rec=self.recorder('tiktok','111')
        with patch('streamlink_recorder.Streamlink',return_value=session):
            stream,streams,token,access=rec.discover()
        self.assertEqual((token,access,rec.live.title),('12345','public','Live now'))
        self.assertEqual(set(streams),{'h265_origin','h264_hd'})
        self.assertEqual(stream.url,'https://cdn.invalid/original.flv')
        self.assertIn('room_id=9876',adapter.urls[-1])
        detail['data']['owner_user_id_str']='555'
        session,_=self.session(responses)
        with patch('streamlink_recorder.Streamlink',return_value=session):
            self.assertEqual(self.recorder('tiktok','111').discover()[-1],'unavailable')
        detail['data']['owner_user_id_str']='111'
        detail['data']['stream_url_filtered_info']={'is_gated_room':True}
        session,_=self.session(responses)
        with patch('streamlink_recorder.Streamlink',return_value=session):
            self.assertEqual(self.recorder('tiktok','111').discover()[-1],'restricted')

    def test_tiktok_live_page_records_available_hd_when_origin_is_unavailable(self):
        state={'LiveRoom':{'liveRoomUserInfo':{'user':{'uniqueId':'tester','id':'111','roomId':'9876'}}}}
        detail={'status_code':0,'data':{'id':'9876','owner':{'id':'111'},'status':2,
            'stream_id':'12345','stream_url':{'flv_pull_url':{'HD1':'https://cdn.invalid/low.flv'}}}}
        session,_=self.session({'/api-live/user/room':'<html></html>','/@tester':'<html></html>',
            '/@tester/live':f'<script type="application/json" id="SIGI_STATE">{json.dumps(state)}</script>',
            '/webcast/room/info/':detail})
        rec=self.recorder('tiktok','111')
        with patch('streamlink_recorder.Streamlink',return_value=session):
            stream,streams,token,access=rec.discover()
        self.assertTrue(rec.progress()['online'])
        self.assertEqual((token,access),('12345','public'))
        self.assertEqual(set(streams),{'h264_hd'})
        self.assertEqual(stream.url,'https://cdn.invalid/low.flv')

    def test_tiktok_waits_only_when_no_video_stream_is_advertised(self):
        payload=self.tiktok_api(quality='ao')
        session,_=self.session({'/api-live/user/room':payload,
            '/@tester':'<html></html>','/@tester/live':'<html></html>'})
        rec=self.recorder('tiktok','111')
        with patch('streamlink_recorder.Streamlink',return_value=session):
            stream,streams,token,access=rec.discover()
        self.assertEqual((stream,streams,token,access),(None,{},'1234','live_no_stream'))

    def test_tiktok_best_available_quality_does_not_open_subscriber_only_room(self):
        payload=self.tiktok_api(quality='hd')
        payload['data']['liveRoom']['liveSubOnly']=1
        session,_=self.session({'/api-live/user/room':payload})
        with patch('streamlink_recorder.Streamlink',return_value=session):
            self.assertEqual(self.recorder('tiktok','111').discover()[-1],'restricted')

    def test_tiktok_retries_available_hd_when_origin_fails_before_recording(self):
        rec=self.recorder('tiktok');origin,hd=object(),object()
        streams={'h264_origin':origin,'h264_sd':object(),'h264_hd':hd}
        with patch.object(rec,'capture',side_effect=[StreamError('unavailable'),None]) as capture:
            rec.capture_stream(origin,streams,'1234')
        self.assertEqual([call.args[0] for call in capture.call_args_list],[origin,hd])

    def test_kick_official_plugin_highest_quality_and_stable_channel_identity(self):
        live={'data':{'id':1234,'playback_url':'https://cdn.invalid/master.m3u8',
            'category':{'name':'Game'},'session_title':'Live title'}}
        playlist='#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=100000,RESOLUTION=640x360\nlow.m3u8\n#EXT-X-STREAM-INF:BANDWIDTH=6000000,RESOLUTION=1920x1080\nhigh.m3u8\n'
        responses={'/api/v2/channels/tester/livestream':live,'/master.m3u8':playlist,
                   '/api/v2/channels/tester':{'id':111,'slug':'tester'}}
        session,adapter=self.session(responses);rec=self.recorder('kick')
        with patch('streamlink_recorder.Streamlink',return_value=session),patch('streamlink.plugins.kick.KickAdapter',return_value=adapter),patch('streamlink_recorder.sync_playwright',side_effect=AssertionError('No browser')):
            stream,_,live,access=rec.discover()
        self.assertEqual(access,'public');self.assertEqual(live,'1234')
        self.assertTrue(stream.url.endswith('/high.m3u8'))
        self.assertEqual(rec.target_user_id,'111');self.assertEqual(rec.live.title,'Live title')
        session,adapter=self.session(responses)
        with patch('streamlink_recorder.Streamlink',return_value=session),patch('streamlink.plugins.kick.KickAdapter',return_value=adapter):
            with self.assertRaises(IdentityError):self.recorder('kick','222').discover()

    def test_mp4_box_limits_and_partial_reads(self):
        class Partial(io.BytesIO):
            def read(self,size=-1):return super().read(min(size,3))
        box=(12).to_bytes(4,'big')+b'mdatDATA'
        self.assertEqual(read_box(Partial(box)),(b'mdat',box))
        self.assertIsNone(read_box(io.BytesIO()))
        for data,error in [(box[:-1],EOFError),((MAX_BOX+1).to_bytes(4,'big')+b'mdat',ValueError),
                           ((4).to_bytes(4,'big')+b'mdat',ValueError)]:
            with self.assertRaises(error):read_box(io.BytesIO(data))

    def test_real_flv_copy_rotates_decodable_parts_without_losing_resolution_or_audio(self):
        raw=self.root/'source.flv'
        run_media([media_tool('ffmpeg'),'-v','error','-y','-f','lavfi','-i','testsrc2=size=720x1280:rate=10',
            '-f','lavfi','-i','sine=frequency=440:sample_rate=48000','-t','6',
            '-c:v','libx264','-threads','2','-preset','ultrafast','-g','10','-bf','0','-c:a','aac','-f','flv',str(raw)])
        outputs=[];rec=self.recorder('tiktok','111');rec.live.livestream_id='1234';rec.live.title='Live title'
        rec.segment_callback=lambda path,meta:outputs.append((path,meta))
        original=rec.fragment
        def fragment(init,data,**kwargs):
            result=original(init,data,**kwargs);rec.request_part();return result
        rec.fragment=fragment
        rec.capture(type('FileStream',(),{'open':lambda _:raw.open('rb')})())
        self.assertGreaterEqual(len(outputs),4);self.assertFalse(rec.progress()['recording'])
        self.assertIsNone(rec.proc);self.assertIsNone(rec.input)
        duration=0
        for path,meta in outputs:
            self.assertLess(path.stat().st_size,500_000_000)
            self.assertEqual(meta['username'],'tiktok:tester');self.assertEqual(meta['profile_id'],'111')
            processed=process_clip({'raw_path':str(path),'metadata':json.dumps(meta)},500_000_000)
            final,info=prepare_videos(processed,500_000_000)
            video=next(iter(info.values()));self.assertEqual((video['width'],video['height']),(720,1280))
            duration+=video['duration_seconds']
            streams=json.loads(run_media([media_tool('ffprobe'),'-v','error','-show_streams','-of','json',str(final[0][0])]))['streams']
            self.assertEqual({s['codec_name'] for s in streams},{'h264','aac'})
        self.assertAlmostEqual(duration,6,delta=.5)

    def test_stop_and_quota_publish_only_the_current_profile(self):
        rec=self.recorder('kick','111');other=self.recorder('kick','222')
        published=[];rec.segment_callback=lambda *args:published.append(args)
        self.assertTrue(rec.fragment(b'init',b'data'))
        rec.capture_allowed=lambda live:False
        self.assertFalse(rec.fragment(b'init',b'next'))
        self.assertEqual(len(published),1);self.assertIsNone(other.file)
        rec.stop_event.set();self.assertFalse(rec.fragment(b'init',b'next'))

    def test_old_cut_does_not_close_first_fragment_after_resuming(self):
        rec=self.recorder('tiktok');published=[];rec.segment_callback=lambda *args:published.append(args)
        rec.request_part()
        self.assertTrue(rec.fragment(b'init',b'first'))
        self.assertFalse(rec.flush_event.is_set())
        self.assertTrue(rec.fragment(b'init',b'second'))
        self.assertFalse(published)
        rec.request_part();rec.capture_allowed=lambda live:False
        self.assertFalse(rec.fragment(b'init',b'stopped'));self.assertEqual(len(published),1)
        rec.capture_allowed=lambda live:True
        self.assertTrue(rec.fragment(b'init',b'resumed'))
        self.assertTrue(rec.fragment(b'init',b'continued'))
        self.assertEqual(len(published),1);rec.publish()

    def test_tiktok_live_page_overrides_stale_offline_profile(self):
        state={'LiveRoom':{'liveRoomUserInfo':{'user':{'uniqueId':'tester','id':'111','roomId':'9999'},
                                             'liveRoom':{'status':2}}}}
        detail={'status_code':0,'data':{'id':'9999','owner':{'id':'111'},'status':2,
            'stream_id':'12345','stream_url':{'flv_pull_url':{'ORIGIN':'https://cdn.invalid/original.flv'}}}}
        responses={'/api-live/user/room':self.tiktok_api(status=4),'/@tester':self.tiktok_profile_page('0'),
            '/@tester/live':f'<script id="SIGI_STATE">{json.dumps(state)}</script>','/webcast/room/info/':detail}
        for status,expected in ((2,'public'),(4,'live_no_stream')):
            detail['data']['status']=status
            session,adapter=self.session(responses)
            with patch('streamlink_recorder.Streamlink',return_value=session):
                rec=self.recorder('tiktok','111');stream,_,_,access=rec.discover()
            self.assertEqual(access,expected)
            self.assertEqual(stream is not None,status==2)
            self.assertIn('room_id=9999',adapter.urls[-1])

    def test_tiktok_connection_failure_does_not_keep_previous_offline_status(self):
        rec=self.recorder('tiktok');rec._online(False)
        rec._progress['access']='offline'
        with patch.object(rec,'discover',side_effect=RuntimeError('stream_video_timeout')),patch.object(
                rec.stop_event,'wait',side_effect=lambda _:rec.stop_event.set()):
            rec.run()
        self.assertIsNone(rec.progress()['online'])
        self.assertEqual(rec.progress()['access'],'connection_error')

    def test_tiktok_api_error_is_unavailable_and_logs_safe_failure_reason(self):
        session,_=self.session({'/api-live/user/room':{'statusCode':10231,'data':None,'message':'The stream is inaccessible'},
            '/@tester':'<html></html>','/@tester/live':'<html></html>'})
        rec=self.recorder('tiktok')
        with patch('streamlink_recorder.Streamlink',return_value=session):
            self.assertEqual(rec.discover()[-1],'unavailable')
        with patch.object(rec,'discover',side_effect=RuntimeError('stream_remux_failed')),patch.object(
                rec.stop_event,'wait',side_effect=lambda _:rec.stop_event.set()),self.assertLogs('streamlink_recorder') as logs:
            rec.run()
        self.assertIn('error=stream_remux_failed',logs.output[0])

    def test_tiktok_does_not_switch_origin_after_writing_media_or_losing_access(self):
        primary,alternate=object(),object()
        origins={'h264_origin':primary,'h265_origin':alternate,'h264_hd':object()}
        for already_recorded in (True,False):
            rec=self.recorder('tiktok')
            rec.capture_allowed=lambda _:already_recorded
            def fail(_):
                if already_recorded:rec.total_bytes+=100
                raise StreamError('interrupted stream')
            with patch.object(rec,'capture',side_effect=fail) as capture, self.assertRaises(StreamError):
                rec.capture_stream(primary,origins,'1234')
            capture.assert_called_once_with(primary)

    def test_tiktok_tries_legacy_hevc_origin_when_h264_fails(self):
        enhanced=self.root/'enhanced.flv'
        run_media([media_tool('ffmpeg'),'-v','error','-y','-f','lavfi','-i','testsrc2=size=360x640:rate=5',
            '-f','lavfi','-i','sine=frequency=440:sample_rate=48000','-t','2',
            '-c:v','libx265','-preset','ultrafast','-x265-params','log-level=error:pools=none:frame-threads=1',
            '-c:a','aac','-f','flv',str(enhanced)])
        data=enhanced.read_bytes();offset=int.from_bytes(data[5:9],'big')+4
        legacy=bytearray(data[:offset]);converted=0
        while offset+11<=len(data):
            header=bytearray(data[offset:offset+11]);size=int.from_bytes(header[1:4],'big')
            payload=data[offset+11:offset+11+size]
            if header[0]==9 and payload[0]&0x80 and payload[1:5]==b'hvc1':
                frame=(payload[0]>>4)&7;packet=payload[0]&15
                self.assertIn(packet,(0,1,2,3,4))
                if packet==4:
                    # Enhanced FLV color metadata has no equivalent in the legacy fixture.
                    offset+=11+size+4;continue
                kind=1 if packet==3 else packet
                payload=bytes([frame<<4|12,kind])+(b'\0'*3 if packet!=1 else b'')+payload[5:]
                converted+=1
            header[1:4]=len(payload).to_bytes(3,'big')
            legacy+=header+payload+(11+len(payload)).to_bytes(4,'big');offset+=11+size+4
        self.assertGreater(converted,0)
        raw=self.root/'legacy.flv';raw.write_bytes(legacy)
        outputs=[];rec=self.recorder('tiktok');rec.segment_callback=lambda path,meta:outputs.append(path)
        def broken_origin():raise StreamError('source is unavailable')
        broken=type('UnavailableStream',(),{'open':staticmethod(broken_origin)})()
        hevc=type('FileStream',(),{'open':lambda _:raw.open('rb')})()
        origins={'h264_origin':broken,'h265_origin':hevc}
        with patch.object(rec,'discover',return_value=(broken,origins,'1234','public')),patch.object(
                rec.stop_event,'wait',side_effect=lambda _:rec.stop_event.set()):
            rec.run()
        self.assertEqual(len(outputs),1)
        streams=json.loads(run_media([media_tool('ffprobe'),'-v','error','-show_streams','-of','json',str(outputs[0])]))['streams']
        video=next(s for s in streams if s['codec_type']=='video')
        self.assertEqual((video['width'],video['height']),(360,640))
        self.assertEqual({s['codec_name'] for s in streams},{'hevc','aac'})
        self.assertAlmostEqual(media_duration(outputs[0]),2,delta=.5)

    def test_real_streamlink_hls_reads_video_audio_and_cleans_up(self):
        playlist=self.root/'live.m3u8'
        run_media([media_tool('ffmpeg'),'-v','error','-y','-f','lavfi','-i','testsrc2=size=1920x1080:rate=5',
            '-f','lavfi','-i','sine=frequency=440:sample_rate=48000','-t','4',
            '-c:v','libx264','-threads','2','-preset','ultrafast','-g','5','-bf','0','-c:a','aac',
            '-f','hls','-hls_time','1','-hls_list_size','0',str(playlist)])
        class Quiet(SimpleHTTPRequestHandler):
            def log_message(self,*args):pass
        server=ThreadingHTTPServer(('127.0.0.1',0),partial(Quiet,directory=str(self.root)))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        session=Streamlink();outputs=[];rec=self.recorder('kick','111')
        rec.segment_callback=lambda path,meta:outputs.append(path)
        try:
            stream=HLSStream(session,f'http://127.0.0.1:{server.server_port}/live.m3u8')
            rec.capture(stream)
        finally:
            session.http.close();server.shutdown();server.server_close();thread.join(timeout=5)
        self.assertEqual(len(outputs),1);self.assertIsNone(rec.proc);self.assertIsNone(rec.input)
        streams=json.loads(run_media([media_tool('ffprobe'),'-v','error','-show_streams','-of','json',str(outputs[0])]))['streams']
        video=next(s for s in streams if s['codec_type']=='video')
        self.assertEqual((video['width'],video['height']),(1920,1080))
        self.assertEqual({s['codec_name'] for s in streams},{'h264','aac'})
        self.assertAlmostEqual(media_duration(outputs[0]),4,delta=.5)
