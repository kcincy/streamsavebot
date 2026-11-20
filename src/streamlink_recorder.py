"""Streamlink discovery and packet-copy capture for Kick/TikTok, using the shared spool."""
from collections import deque
from pathlib import Path
import logging
import os
import subprocess
import threading
import time
from urllib.parse import urlsplit,parse_qs

from streamlink import Streamlink
from recorder import StreamRecorder,Result,sync_playwright,open_context
from platforms import profile_url
from project_runtime import media_tool,TOOLS
from stripchat_capture import IdentityError

log=logging.getLogger(__name__)
MAX_BOX=8*1024*1024


def read_box(source):
    def exact(length):
        chunks=[];remaining=length
        while remaining:
            chunk=source.read(remaining)
            if not chunk:raise EOFError('truncated_media_box')
            chunks.append(chunk);remaining-=len(chunk)
        return b''.join(chunks)
    head=source.read(8)
    if not head:return None
    if len(head)<8:head+=exact(8-len(head))
    size=int.from_bytes(head[:4],'big')
    if size==1:
        head+=exact(8);size=int.from_bytes(head[8:16],'big')
    if size<len(head) or size>MAX_BOX:raise ValueError('media_box_limit')
    return head[4:8],head+exact(size-len(head))


def read_flv_tag(source):
    header=source.read(11)
    if not header:return None
    if len(header)!=11:raise EOFError('truncated_flv_tag')
    length=int.from_bytes(header[1:4],'big')
    if length>MAX_BOX:raise ValueError('flv_tag_limit')
    body=bytearray()
    while len(body)<length+4:
        chunk=source.read(length+4-len(body))
        if not chunk:raise EOFError('truncated_flv_tag')
        body.extend(chunk)
    if int.from_bytes(body[-4:],'big')!=length+11:raise ValueError('invalid_flv_tag_size')
    return header[0],bytes(body[:-4]),header+body


def original_stream(platform,streams):
    if platform=='tiktok':
        # Origin only: never fall back to hd/sd/auto when the source stream is unavailable.
        return streams.get('h264_origin') or streams.get('h265_origin')
    return streams.get('source') or streams.get('best')


class StreamlinkRecorder(StreamRecorder):
    def __init__(self,*args,platform,**kwargs):
        super().__init__(*args,**kwargs)
        self.platform=platform
        self.key=platform+':'+self.follow_username
        self.url=profile_url(self.key)
        self.live.url_override=self.url
        self.path=None;self.file=None;self.part_started=0
        self.session=None;self.input=None;self.proc=None
        self.live_id_callback=None
        self.source_suffix='.mp4'
        self.last_video=0

    def kick_challenge(self,plugin):
        # Use the project's browser lifecycle so the official plugin works in read-only Docker too.
        with sync_playwright() as pw:
            endpoint=self.browser_endpoint_provider() if self.browser_endpoint_provider else None
            browser,context=open_context(pw,False,endpoint=endpoint)
            try:
                page=context.new_page();headers={};status=[]
                def request(request):
                    if request.is_navigation_request() and request.url.rstrip('/')==self.url.rstrip('/'):
                        headers.update({k:v for k,v in request.all_headers().items() if k.lower()!='cookie'})
                page.on('request',request)
                page.on('response',lambda response:status.append(response.status) if response.url.rstrip('/')==self.url.rstrip('/') else None)
                page.route('**/*',lambda route:route.abort() if route.request.resource_type in ('image','font','media')
                    or urlsplit(route.request.url).path.endswith(('.m3u8','.ts','.m4s','.flv')) else route.continue_())
                page.goto(self.url,wait_until='domcontentloaded',timeout=20000)
                for _ in range(10):
                    page.wait_for_timeout(500)
                    if status and status[-1]==200:break
                if not status or status[-1]!=200:return False
                plugin.session.http.headers.update(headers)
                plugin.cache.set(plugin._CACHE_HEADERS,headers,expires=plugin._CACHE_EXPIRATION)
                for cookie in context.cookies(self.url):
                    plugin.session.http.cookies.set(cookie['name'],cookie['value'],domain=cookie['domain'],path=cookie['path'])
                plugin.save_cookies()
                return True
            finally:context.close();browser.close()

    def discover(self):
        session=Streamlink();self.session=session
        for name,value in (('http-timeout',15),('stream-timeout',15),('hls-segment-timeout',15),
                           ('ringbuffer-size',8*1024*1024),('ffmpeg-ffmpeg',media_tool('ffmpeg')),
                           ('webbrowser-headless',True)):
            session.set_option(name,value)
        relative='chrome-win64/chrome.exe' if os.name=='nt' else 'chrome-linux64/chrome'
        browsers=list((TOOLS/'browsers').glob('chromium-*/'+relative))
        if browsers:session.set_option('webbrowser-executable',str(max(browsers,key=lambda p:p.stat().st_mtime)))
        identity={};observation={}
        def response(response,*args,**kwargs):
            parsed=urlsplit(response.url)
            if self.platform=='tiktok' and parsed.hostname=='www.tiktok.com' and parsed.path=='/api-live/user/room':
                if parse_qs(parsed.query).get('uniqueId',[''])[0].casefold()!=self.follow_username:return
                try:data=response.json().get('data',{})
                except ValueError:return
                observation.update(data.get('liveRoom') or {})
                user=data.get('user') or observation.get('owner') or {}
                identity.update(id=user.get('id') or observation.get('ownerUserId'),
                                name=user.get('uniqueId') or self.follow_username)
            elif self.platform=='kick' and parsed.hostname=='kick.com' and parsed.path==f'/api/v2/channels/{self.follow_username}/livestream':
                try:data=response.json().get('data') or {}
                except ValueError:return
                identity.update(id=data.get('channel_id'),name=self.follow_username)
        session.http.hooks['response'].append(response)
        name,plugin_class,resolved=session.resolve_url(self.url,follow_redirect=False)
        if name!=self.platform:raise ValueError('unexpected_streamlink_plugin')
        plugin=plugin_class(session,resolved)
        if self.platform=='kick':plugin._get_cookies_from_webbrowser=lambda:self.kick_challenge(plugin)
        streams=plugin.streams()
        if not streams:
            status='offline' if self.platform=='kick' or observation.get('status')==4 else 'unavailable'
            return None,None,None,status
        if self.platform=='kick' and not identity.get('id'):
            response=session.http.get(f'https://kick.com/api/v2/channels/{self.follow_username}',
                headers=plugin._get_api_headers(),timeout=15)
            try:data=response.json()
            finally:response.close()
            data=data.get('data') or data
            identity.update(id=data.get('id'),name=data.get('slug') or self.follow_username)
        if not identity.get('id'):raise RuntimeError('profile_identity_unavailable')
        if str(identity['name']).casefold()!=self.follow_username:raise IdentityError('profile_name_changed')
        uid=str(identity['id'])
        if self.target_user_id and self.target_user_id!=uid:raise IdentityError('profile_identity_changed')
        if not self.target_user_id:
            if self.identity_callback:self.identity_callback(uid)
            self.target_user_id=uid
        self.live.user_id=uid;self.live.title=plugin.title or ''
        if not plugin.id:raise RuntimeError('live_identity_unavailable')
        token=str(plugin.id)
        live_id=self.live_id_callback(token,True) if self.live_id_callback else token
        self.live.livestream_id=live_id;self._online(True,live_id)
        selected=original_stream(self.platform,streams)
        return selected,streams,live_id,'public' if selected else 'unavailable'

    def publish(self,ended=None):
        if not self.file:return
        ended=time.time() if ended is None else ended
        path,self.path=self.path,None
        self.file.close();self.file=None;self._part_finished();self.flush_event.clear()
        self.segment_callback(path,dict(username=self.key,live_id=self.live.livestream_id,
            started=self.part_started,ended=ended,seconds=max(0,ended-self.part_started),
            title=self.live.title,profile_id=self.target_user_id,
            mime='video/x-flv' if self.source_suffix=='.flv' else 'video/mp4',normalize=False,video_size=None))

    def fragment(self,init,data,allow_rotate=True,check_allowed=True):
        allowed=not check_allowed or not self.capture_allowed or self.capture_allowed(self.live.livestream_id)
        if self.stop_event.is_set() or not allowed:
            self.publish();return False
        limit=self.part_limit or 500_000_000
        if len(init)+len(data)>limit:raise RuntimeError('source_fragment_exceeds_part_limit')
        if self.file and allow_rotate and (self.flush_event.is_set() or self.file.tell()+len(data)>limit
                          or (self.rotate and time.time()-self.part_started>=self.rotate)):
            self.publish()
        if not self.file:
            self.flush_event.clear()
            self.status='waiting_space'
            self.path=self._recording_base().with_suffix(self.source_suffix)
            self.file=self.path.open('xb');self.part_started=time.time();self._part_started(self.path)
            if self.stop_event.is_set() or (self.capture_allowed and not self.capture_allowed(self.live.livestream_id)):
                self.publish();return False
            self._write(self.file,init)
        self._write(self.file,data);self.file.flush();self.status='recording'
        if self.platform!='tiktok':self.last_video=time.monotonic()
        return True

    def capture_flv(self,source):
        header=source.read(13)
        if len(header)!=13 or header[:3]!=b'FLV' or int.from_bytes(header[5:9],'big')!=9:
            raise ValueError('invalid_flv_header')
        configs={};waiting_key=True;last_check=-1e9
        while not self.stop_event.is_set():
            tag=read_flv_tag(source)
            if tag is None:return 'ended'
            kind,payload,raw=tag
            if not payload:continue
            sequence=(kind==8 and payload[0]>>4==10 and len(payload)>1 and payload[1]==0)
            if kind==9:
                enhanced=bool(payload[0]&128)
                sequence=(payload[0]&15)==0 if enhanced else len(payload)>1 and payload[0]&15==7 and payload[1]==0
                if (enhanced and payload[0]&15==2) or (not enhanced and len(payload)>1 and payload[1]==2):continue
            if sequence:
                if configs.get(kind)!=payload:
                    if kind in configs:
                        self.publish();waiting_key=True
                        log.info('TikTok codec configuration changed; closing part profile=%s',self.follow_username)
                    configs[kind]=payload
                # Preserve the full sequence header and its original timestamp.
                configs[(kind,'tag')]=raw
                continue
            if kind==9:self.last_video=time.monotonic()
            key=kind==9 and ((payload[0]>>4)&7)==1
            if key and 9 not in configs:continue
            if key and (waiting_key or (self.file and (self.flush_event.is_set() or (self.segment_bytes and self.file.tell()>=self.segment_bytes)))):
                self.publish();waiting_key=False
            if waiting_key:continue
            if self.file and self.file.tell()+len(raw)>(self.part_limit or 500_000_000):
                self.publish();waiting_key=True
                continue
            init=header+configs.get((8,'tag'),b'')+configs.get((9,'tag'),b'')
            now=time.monotonic();check=now-last_check>=1
            if check:last_check=now
            if not self.fragment(init,raw,allow_rotate=False,check_allowed=check):return 'watching'
        return 'stopped'

    def capture(self,stream):
        self.status='connecting'
        self.input=stream.open()
        command=[media_tool('ffmpeg'),'-v','error','-nostdin','-fflags','+genpts+discardcorrupt',
            '-i','pipe:0','-map','0:v:0','-map','0:a:0?','-c','copy']
        if self.platform=='kick':command+=['-bsf:a','aac_adtstoasc']
        if self.platform=='tiktok':
            self.source_suffix='.flv'
            command+=['-flvflags','no_duration_filesize','-f','flv','pipe:1']
        else:command+=['-movflags','+frag_keyframe+empty_moov+default_base_moof','-f','mp4','pipe:1']
        flags={'creationflags':getattr(subprocess,'CREATE_NO_WINDOW',0)} if os.name=='nt' else {'start_new_session':True}
        try:self.proc=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,**flags)
        except Exception:
            self.input.close();self.input=None
            raise
        self.status='waiting_data'
        proc=self.proc;cancel=threading.Event();errors=deque(maxlen=8)
        self.last_video=time.monotonic()
        def pump():
            try:
                while not cancel.is_set() and not self.stop_event.is_set():
                    data=self.input.read(64*1024)
                    if not data:break
                    proc.stdin.write(data)
            except Exception as exc:errors.append(type(exc).__name__)
            finally:
                try:proc.stdin.close()
                except OSError:pass
        def stderr():
            for line in iter(proc.stderr.readline,b''):errors.append(line[-256:])
        def guard():
            while not cancel.wait(1):
                try:allowed=not self.capture_allowed or self.capture_allowed(self.live.livestream_id)
                except Exception:allowed=False
                if self.stop_event.is_set() or not allowed or time.monotonic()-self.last_video>max(5,self.stall_timeout):
                    if proc.poll() is None:
                        try:
                            proc.terminate()
                            try:proc.wait(timeout=2)
                            except subprocess.TimeoutExpired:proc.kill()
                        except OSError:pass
                    return
        reader=threading.Thread(target=pump,daemon=True);error_reader=threading.Thread(target=stderr,daemon=True)
        watchdog=threading.Thread(target=guard,daemon=True)
        reader.start();error_reader.start();watchdog.start();init=b'';fragment=None;ended=False
        try:
            if self.platform=='tiktok':
                reason=self.capture_flv(proc.stdout)
                if reason=='ended' and not self.stop_event.is_set() and proc.wait(timeout=5):raise RuntimeError('stream_remux_failed')
                return
            while not self.stop_event.is_set():
                box=read_box(proc.stdout)
                if box is None:
                    ended=True;break
                kind,data=box
                if kind in (b'ftyp',b'moov'):
                    init+=data
                    if len(init)>MAX_BOX:raise ValueError('media_init_limit')
                elif kind==b'moof':fragment=data
                elif kind==b'mdat':
                    if not init or fragment is None:raise ValueError('invalid_media_fragment')
                    if not self.fragment(init,fragment+data):break
                    fragment=None
            if ended and not self.stop_event.is_set() and proc.wait(timeout=5):raise RuntimeError('stream_remux_failed')
        finally:
            ended=time.time();self._part_finished()
            cancel.set()
            try:self.input.close()
            except Exception:pass
            if proc.poll() is None:proc.terminate()
            try:proc.wait(timeout=5)
            except subprocess.TimeoutExpired:proc.kill();proc.wait()
            reader.join(timeout=16);error_reader.join(timeout=5);watchdog.join(timeout=2)
            for pipe in (proc.stdout,proc.stderr):pipe.close()
            self.input=None;self.proc=None
            self.publish(ended)

    def run(self):
        error=''
        while not self.stop_event.is_set():
            try:
                self.status='checking'
                with self.check_guard():stream,streams,live_id,access=self.discover()
                with self._progress_lock:self._progress['access']=access
                if stream is None:
                    self._online(False if access=='offline' else None)
                    if access=='offline' and self.live_id_callback:self.live_id_callback('',False)
                    self.status=access
                elif self.capture_allowed and not self.capture_allowed(live_id):self.status='watching'
                elif not self.capture_acquire():self.status='waiting_resources'
                else:
                    try:self.capture(stream)
                    finally:self.capture_release()
                    if not self.stop_event.is_set():self.status='reconnecting'
            except IdentityError as exc:
                self._identity_error=error=type(exc).__name__;break
            except InterruptedError:
                if self.stop_event.is_set():break
                self.status='watching'
            except Exception as exc:
                self.capture_failure()
                self.status='reconnecting'
                reason=str(exc) if isinstance(exc,RuntimeError) and str(exc) in (
                    'profile_identity_unavailable','live_identity_unavailable','stream_remux_failed',
                    'source_fragment_exceeds_part_limit') else type(exc).__name__
                log.warning('Streamlink retry platform=%s profile=%s error=%s',self.platform,self.follow_username,reason)
            finally:
                if self.session:
                    self.session.http.close();self.session=None
            self.stop_event.wait(self.retry_delay(15))
        self._part_finished();self.status='stopped' if not error else 'error'
        return Result(self.live,None,0,self.total_bytes,not error,error)
