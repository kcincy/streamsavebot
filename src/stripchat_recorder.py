"""StripChat: identidade fixa, leitura ordenada e partes na fila compartilhada do bot."""
import asyncio
import base64
import json
import logging
import re
import time
from urllib.parse import urlsplit,unquote

import httpx

from recorder import StreamRecorder,Result,UA,launch_args,expendable_resource
from response_queue import ResponseQueue
from stripchat_capture import BASE,HEADERS,IdentityError,ProfileUnavailable,DirectUnavailable,SegmentWriter,model_info,parse_model_info,_pick_variant,hls_playlist,stream_urls

log=logging.getLogger(__name__)
POLL_SECONDS=15
SEGMENT=re.compile(r'_(\d+)_[^/]*?_(\d+)(?:_part(\d+))?\.mp4(?:\?|$)')


class StripchatRecorder(StreamRecorder):
    def access(self,status,online=None):
        if status!='public':self._part_finished()
        self._online(online)
        with self._progress_lock:
            previous=self._progress.get('access')
            self._progress['access']=status
        if previous!=status:
            log.info('StripChat estado streamer=%s status=%s',self.follow_username,status)
        self.status=status if status!='public' else 'checking'

    def run(self):
        error=''
        try:
            asyncio.run(self.monitor())
        except Exception as exc:
            error=type(exc).__name__
            if isinstance(exc,ValueError):
                self._identity_error=error
                error='ValueError: '+error
            log.error('monitor StripChat encerrado streamer=%s erro=%s',self.follow_username,error)
        self.status='stopped' if self.stop_event.is_set() else ('error' if error else 'offline')
        return Result(self.live,None,0,self.total_bytes,not error,error)

    async def pause(self,seconds):
        until=time.monotonic()+self.retry_delay(seconds)
        while not self.stop_event.is_set() and time.monotonic()<until:
            await asyncio.sleep(.2)

    async def monitor(self):
        while not self.stop_event.is_set():
            self.status='checking'
            try:
                def check():
                    with self.check_guard():return model_info(self.follow_username,15)
                info=await asyncio.to_thread(check)
                if self.stop_event.is_set():break
                uid=str(info['id'])
                if self.target_user_id and self.target_user_id!=uid:
                    raise IdentityError('a identidade da conta mudou')
                if not self.target_user_id:
                    await asyncio.to_thread(self.identity_callback,uid)
                    self.target_user_id=uid
                live_id=await asyncio.to_thread(self.live_id_callback,info['stream_name'],info['live'])
                self.live.livestream_id=live_id
                self._online(info['live'],live_id)
                self.access(info['status'],info['live'])
                if not info['recordable']:
                    await self.pause(POLL_SECONDS)
                    continue
                if not await asyncio.to_thread(self.capture_allowed,live_id):
                    self.status='watching'
                    await self.pause(POLL_SECONDS)
                    continue
                if self.stop_event.is_set():break
                if not await asyncio.to_thread(self.capture_acquire):
                    self.status='waiting_resources'
                    await self.pause(POLL_SECONDS)
                    continue
                try:await DirectSession(self,info).run()
                finally:await asyncio.to_thread(self.capture_release)
            except IdentityError:
                raise
            except DirectUnavailable as exc:
                self.capture_failure()
                if not self.stop_event.is_set():
                    log.warning('StripChat captura HTTP indisponível streamer=%s motivo=%s; nova tentativa sem navegador',
                                self.follow_username,exc)
                    self.access('unavailable')
                    await self.pause(POLL_SECONDS)
            except ProfileUnavailable as exc:
                self.access(exc.status)
                await self.pause(POLL_SECONDS)
            except Exception as exc:
                self.capture_failure()
                if not self.stop_event.is_set():
                    log.warning('StripChat reconectando streamer=%s erro=%s',self.follow_username,type(exc).__name__)
                    self.access('unavailable')
                    await self.pause(POLL_SECONDS)
            await self.pause(2)


class Session:
    def __init__(self,owner,info):
        self.owner,self.info=owner,info
        self.page=None
        self.confirmed=False
        self.paused=False
        self.writer=None
        self.file=None
        self.path=None
        self.init=None
        self.mime='video/mp4'
        self.lock=asyncio.Lock()
        self.part_started=0
        self.last_data=time.monotonic()
        self.last_msn=-1
        self.wait_msn=-1
        self.encrypted=False
        self.media_recorder=False
        self.binding=False
        self.video_size=None
        self.normalize=False
        self.public=bool(info.get('recordable'))

    def valid(self):
        if not self.public or not self.confirmed or self.paused or not self.page or self.page.is_closed():
            return False
        actual,wanted=urlsplit(self.page.url),urlsplit(BASE)
        return (actual.scheme==wanted.scheme and actual.netloc==wanted.netloc
                and actual.path.rstrip('/').casefold()=='/'+self.info['username'].casefold())

    def response(self,response):
        path=urlsplit(response.url).path
        if '.mp4' not in path or '/cpa/' in path or path.endswith('/media.mp4'):
            return
        if '_init_' not in path and not SEGMENT.search(response.url):
            return
        # Nunca aceita mídia de recomendações, mesmo que o navegador faça preloads.
        identifiers=(str(self.info['id']),self.info['stream_name'])
        match=SEGMENT.search(path)
        identity_path=path[:match.start(1)]+path[match.end(1):] if match else path
        if not any(re.search(r'(?<![A-Za-z0-9])'+re.escape(uid)+r'(?![A-Za-z0-9])',identity_path) for uid in identifiers):
            return
        if response.status in (200,206) and not self.media_recorder:
            self.responses.submit(response)

    async def new_part(self,suffix):
        base=await asyncio.to_thread(self.owner._recording_base)
        self.path=base.with_suffix(suffix)
        self.part_started=time.time()
        self.owner._part_started(self.path)
        self.owner.status='recording'
        self.video_size=None
        self.normalize=False

    async def new_writer(self):
        await self.new_part('.mp4')
        self.writer=SegmentWriter(self.path,self.owner.part_limit,
                                  lambda data:self.owner._write(self.writer.fh,data))
        if self.init:
            self.writer.write_init(self.init)

    async def publish(self):
        if self.writer:
            try:
                if not self.writer.written and not self.writer.buf:
                    self.writer.fh.truncate(0)
                self.writer.close()
            except Exception as exc:
                log.warning('StripChat finalização bruta incompleta; enviando à recuperação streamer=%s erro=%s',
                            self.owner.follow_username,type(exc).__name__)
            finally:
                if not self.writer.fh.closed:self.writer.fh.close()
            self.writer=None
        if self.file:
            self.file.close()
            self.file=None
        self.owner._part_finished()
        if self.path:
            path,self.path=self.path,None
            await asyncio.to_thread(self.owner.segment_callback,path,{
                'username':'stripchat:'+self.owner.follow_username,
                'live_id':self.owner.live.livestream_id,'started':self.part_started,
                'ended':time.time(),'title':self.info.get('title'),'profile_id':str(self.info['id']),
                'mime':self.mime,'seconds':max(0,time.time()-self.part_started),
                'normalize':self.normalize,'video_size':self.video_size})

    async def allowed(self):
        return not self.owner.stop_event.is_set() and await asyncio.to_thread(
            self.owner.capture_allowed,self.owner.live.livestream_id)

    async def cut(self):
        await self.publish()
        self.owner.flush_event.clear()
        self.wait_msn=self.last_msn
        if not await self.allowed():
            self.paused=True

    async def accept(self,url,body):
        async with self.lock:
            if not self.valid() or self.encrypted:
                return
            if not body or body[4:8] not in (b'ftyp',b'styp',b'sidx',b'moof',b'free'):
                raise RuntimeError('invalid_mp4_segment')
            if '_init_' in urlsplit(url).path:
                if any(marker in body for marker in (b'encv',b'enca',b'sinf',b'pssh')):
                    if self.init:
                        await self.publish()
                    elif self.writer:
                        self.writer.buf.clear()
                    self.encrypted=True
                    self.public=False
                    self.owner.access('restricted',True)
                    return
                if self.init is not None and self.init!=body:
                    await self.publish()
                    self.last_msn=self.wait_msn=-1
                    log.info('StripChat configuração mudou; parte anterior finalizada streamer=%s',self.owner.follow_username)
                self.init=body
                self.mime='video/mp4;codecs=h264' if b'avc1' in body else 'video/mp4'
                if self.writer:
                    self.writer.write_init(body)
                return
            match=SEGMENT.search(url)
            msn=int(match.group(1));part=int(match.group(3)) if match.group(3) else -1
            if msn<=self.wait_msn:
                return
            if self.writer:
                size=self.writer.bytes_written+sum(len(item[1]) for item in self.writer.buf.values())
                if size>=self.owner.segment_bytes and msn>self.last_msn and part in (-1,0):
                    await self.cut()
                    if self.paused:return
                elif size+len(body)>self.owner.part_limit:
                    raise RuntimeError('next_segment_exceeds_limit')
            if not self.writer:
                await self.new_writer()
            if not self.valid():
                self.writer.buf.clear()
                return
            self.writer.add((msn,part),body)
            self.last_msn=max(self.last_msn,msn)
            self.last_data=time.monotonic()

    async def master(self,route):
        response=None
        try:
            response=await route.fetch(timeout=15000)
            body=await response.text()
            if '#EXT-X-STREAM-INF' in body:
                body=_pick_variant(body,'source')
            headers={k:v for k,v in response.headers.items() if k.lower() not in ('content-length','content-encoding')}
            await route.fulfill(status=response.status,headers=headers,body=body)
        except Exception:
            self.responses.error=RuntimeError('quality_selection_failed')
            try:await route.abort()
            except Exception:pass
        finally:
            if response:await response.dispose()

    async def guard_navigation(self,route):
        if self.owner.low_resource_mode and expendable_resource(route.request):
            await route.abort()
            return
        if (self.confirmed and route.request.is_navigation_request()
                and route.request.frame==self.page.main_frame
                and (urlsplit(route.request.url).netloc!=urlsplit(BASE).netloc
                     or urlsplit(route.request.url).scheme!=urlsplit(BASE).scheme
                     or urlsplit(route.request.url).path.rstrip('/').casefold()!='/'+self.info['username'].casefold())):
            self.responses.error=ProfileUnavailable()
            await route.abort()
        else:
            await route.fallback()

    async def recorder_chunk(self,source,data):
        if source['frame']!=self.page.main_frame or not self.valid():
            raise IdentityError('media_recorder_identity_changed')
        if len(data)>12*1024*1024:
            raise RuntimeError('media_recorder_buffer_full')
        self.owner._write(self.file,base64.b64decode(data,validate=True))
        self.last_data=time.monotonic()

    async def start_recorder(self):
        if not await self.refresh_access():
            raise ProfileUnavailable(self.owner.progress().get('access','restricted'))
        if not self.binding:
            await self.page.expose_binding('__scChunk',self.recorder_chunk)
            self.binding=True
        await self.page.wait_for_function("() => [...document.querySelectorAll('video')].some(v=>v.readyState>=2 && v.videoWidth && !v.ended)",timeout=25000)
        await self.new_part('.mkv')
        self.file=self.path.open('xb')
        if not await self.refresh_access():
            raise ProfileUnavailable(self.owner.progress().get('access','restricted'))
        self.mime=(await self.page.evaluate("""() => {
            const videos=[...document.querySelectorAll('video')].filter(v=>v.readyState>=2 && v.videoWidth && !v.ended);
            if(videos.length!==1) throw Error('ambiguous_player');
            const v=videos[0]; v.muted=true; v.play().catch(()=>{});
            const reuse=window.__scVideo===v && window.__scSource===v.currentSrc && window.__scInput===v.srcObject
                && window.__scStream?.getVideoTracks().some(t=>t.readyState==='live');
            const stream=reuse ? window.__scStream : (v.captureStream ? v.captureStream() : v.mozCaptureStream());
            const rec=new MediaRecorder(stream,{audioBitsPerSecond:128000});
            window.__scVideo=v; window.__scSource=v.currentSrc; window.__scStream=stream;
            window.__scInput=v.srcObject;
            window.__scChunks=Promise.resolve(); window.__scError=''; let pending=0;
            rec.ondataavailable=e=>{
                if(!e.data.size)return;
                pending+=e.data.size;
                if(pending>8*1024*1024){window.__scError='buffer_full';rec.stop();return;}
                window.__scChunks=window.__scChunks.then(async()=>{
                    const bytes=new Uint8Array(await e.data.arrayBuffer());let s='';
                    for(let i=0;i<bytes.length;i+=8192)s+=String.fromCharCode(...bytes.subarray(i,i+8192));
                    await window.__scChunk(btoa(s));pending-=e.data.size;
                }).catch(()=>{window.__scError='chunk_failed';});
            };
            rec.onerror=()=>{window.__scError='recorder_failed';};
            window.__scRec=rec; rec.start(1000);return rec.mimeType;
        }""")) or 'video/webm'
        self.media_recorder=True
        self.last_data=time.monotonic()
        log.info('StripChat MediaRecorder iniciado streamer=%s',self.owner.follow_username)

    async def refresh_access(self):
        try:
            info=await asyncio.to_thread(model_info,self.info['username'],10)
            if info['id']!=self.info['id']:
                raise IdentityError('model_id_changed')
            self.public=info['recordable'] and info['stream_name']==self.info['stream_name']
            if not self.public:
                self.owner.access(info['status'],info['live'])
        except ProfileUnavailable as exc:
            self.public=False
            self.owner.access(exc.status)
        except IdentityError:
            self.public=False
            raise
        except Exception:
            self.public=False
            self.owner.access('unavailable')
        if not self.public and self.writer:
            self.writer.buf.clear()
        return self.public

    async def stop_recorder(self):
        self.owner._part_finished()
        try:
            mime=await asyncio.wait_for(self.page.evaluate("""async()=>{
                const r=window.__scRec;
                if(r && r.state!=='inactive')await new Promise(resolve=>{r.addEventListener('stop',resolve,{once:true});r.stop();});
                await window.__scChunks;
                return r?.mimeType || '';
            }"""),30)
            self.mime=mime or self.mime
        except Exception:
            log.warning('StripChat último bloco indisponível; prefixo preservado streamer=%s',self.owner.follow_username)
        self.media_recorder=False

    async def run(self):
        from playwright.async_api import async_playwright
        async with async_playwright() as pw:
            endpoint=await asyncio.to_thread(self.owner.browser_endpoint_provider) if self.owner.browser_endpoint_provider else None
            browser=(await pw.chromium.connect_over_cdp(endpoint) if endpoint else
                     await pw.chromium.launch(channel='chromium',headless=True,handle_sigint=False,args=launch_args(False)))
            context=None
            try:
                context=await browser.new_context(user_agent=UA,locale='pt-BR',storage_state=self.owner.state,
                                                  viewport={'width':1280,'height':720})
                self.page=await context.new_page()
                self.responses=ResponseQueue(self.page,self.accept,self.valid)
                self.page.on('framenavigated',self.responses.navigation)
                self.page.on('response',self.response)
                await self.page.route(re.compile(r'.*/master/.*\.m3u8.*'),self.master)
                await self.page.route('**/*',self.guard_navigation)
                try:
                    await self.page.goto(f"{BASE}/{self.info['username']}",wait_until='domcontentloaded',timeout=30000)
                    current=parse_model_info(await self.page.content(),self.info['username'])
                    if current['id']!=self.info['id']:
                        raise IdentityError('browser_profile_identity_mismatch')
                    self.public=current['recordable']
                    if not self.public:
                        self.owner.access(current['status'],current['live'])
                        return
                    if current['stream_name']!=self.info['stream_name']:
                        raise RuntimeError('broadcast_changed_during_navigation')
                    self.confirmed=True
                    self.responses.ready.set()
                    for selector in ('video',"button[aria-label*='play' i]",'.video-player'):
                        try:
                            await self.page.click(selector,timeout=1000)
                            break
                        except Exception:pass
                    await self.page.evaluate("() => document.querySelectorAll('video').forEach(v=>{v.muted=true;v.play().catch(()=>{});})")
                    checked=time.monotonic()
                    previous_frames=None;last_video=checked
                    while not self.owner.stop_event.is_set() and not self.paused:
                        if self.responses.error:raise self.responses.error
                        if not self.public:break
                        if not self.valid():raise ProfileUnavailable()
                        now=time.monotonic()
                        if now-checked>3:
                            if not await self.refresh_access():break
                            checked=now
                        if self.init is None and now-self.last_data>25 and not self.media_recorder:
                            self.page.remove_listener('response',self.response)
                            await self.responses.drain()
                            async with self.lock:
                                await self.publish()
                            await self.start_recorder()
                        if self.media_recorder:
                            stats=await self.page.evaluate("""()=>{
                                const v=window.__scVideo;
                                return {frames:v?.getVideoPlaybackQuality?.().totalVideoFrames??v?.currentTime,
                                  valid:!!v?.isConnected && v.currentSrc===window.__scSource && v.srcObject===window.__scInput && !v.ended,
                                  width:v?.videoWidth,height:v?.videoHeight,error:window.__scError};
                            }""")
                            if not stats['valid'] or stats['error']:break
                            size=(stats.get('width'),stats.get('height'))
                            if all(size):
                                self.normalize=self.normalize or bool(self.video_size and size!=self.video_size)
                                self.video_size=self.video_size or size
                            if stats['frames']!=previous_frames:
                                previous_frames=stats['frames'];last_video=now
                            if now-last_video>30:break
                            if self.owner.flush_event.is_set() or self.owner.progress()['part_bytes']>=self.owner.segment_bytes:
                                await self.stop_recorder()
                                await self.cut()
                                if not self.paused:await self.start_recorder()
                        else:
                            async with self.lock:
                                if self.writer:self.writer.flush()
                                if self.owner.flush_event.is_set():await self.cut()
                        if now-self.last_data>30:break
                        await asyncio.sleep(.25)
                finally:
                    self.page.remove_listener('response',self.response)
                    await self.responses.drain()
                    if self.media_recorder:await self.stop_recorder()
                    async with self.lock:await self.publish()
            finally:
                try:
                    if self.page and not self.page.is_closed():
                        await self.page.unroute_all(behavior='wait')
                finally:
                    try:
                        if context:await context.close()
                    finally:await browser.close()


class DirectSession(Session):
    """HTTP persistente, um segmento por vez, sem player ou recodificação na captura."""
    def valid(self):
        return self.confirmed and self.public and not self.paused and not self.owner.stop_event.is_set()

    async def fetch(self,url,limit=8*1024*1024):
        target=urlsplit(url);root=urlsplit(self.root)
        path=unquote(target.path)
        match=SEGMENT.search(path)
        identity_path=path[:match.start(1)]+path[match.end(1):] if match else path
        identifiers=(str(self.info['id']),self.info['stream_name'])
        if (target.scheme!=root.scheme or target.netloc not in getattr(self,'hosts',{root.netloc})
                or target.username or target.password or '..' in path.split('/') or '/cpa/' in path
                or path.endswith('/media.mp4') or not any(re.search(r'(?<![A-Za-z0-9])'+re.escape(uid)
                    +r'(?![A-Za-z0-9])',identity_path) for uid in identifiers)):
            raise DirectUnavailable('stream_url_mismatch')
        return await self.read(url,limit)

    async def read(self,url,limit):
        async with asyncio.timeout(15):
            async with self.client.stream('GET',url) as response:
                response.raise_for_status()
                if response.status_code!=200:raise DirectUnavailable('partial_response')
                if int(response.headers.get('content-length','0'))>limit:
                    raise DirectUnavailable('response_too_large')
                data=bytearray()
                async for chunk in response.aiter_bytes():
                    if self.owner.stop_event.is_set():raise InterruptedError()
                    if len(data)+len(chunk)>limit:raise DirectUnavailable('response_too_large')
                    data.extend(chunk)
                return bytes(data)

    async def run(self):
        urls=self.info.get('hls_urls',[])
        async with httpx.AsyncClient(headers={**HEADERS,'Referer':f"{BASE}/{self.info['username']}"},
                timeout=10,limits=httpx.Limits(max_connections=2,max_keepalive_connections=2)) as self.client:
            if not urls:
                try:
                    body=await self.read(BASE+'/api/front/v3/config/static',1024*1024)
                    urls=stream_urls(json.loads(body)['static'],self.info['id'])
                except (DirectUnavailable,httpx.HTTPError,TimeoutError,ValueError,KeyError,TypeError,AttributeError):pass
                if not urls:
                    urls=[f"https://edge-hls.doppiocdn.{suffix}/hls/{self.info['id']}/master/{self.info['id']}_auto.m3u8"
                          for suffix in ('com','org','net')]
            self.hosts={urlsplit(url).netloc for url in urls}
            self.hosts.update('media-hls.'+host[len('edge-hls.'):] for host in tuple(self.hosts) if host.startswith('edge-hls.'))
            self.hosts.update(f'media-hls.doppiocdn.{suffix}' for suffix in ('com','org','net'))
            deadline=time.monotonic()+15
            reason='no_compatible_playlist'
            for url in urls:
                if self.owner.stop_event.is_set():return
                self.root=url.split('/master/',1)[0]+'/'
                url+=('&' if '?' in url else '?')+'playlistType=standard&preferredVideoCodec=h264'
                try:
                    async with asyncio.timeout_at(deadline):
                        for _ in range(3):
                            body=await self.fetch(url,1024*1024)
                            variant,segments,delay=hls_playlist(body.decode('utf-8-sig'),url)
                            if not variant:break
                            url=variant
                        else:raise DirectUnavailable('nested_playlists')
                    break
                except InterruptedError:
                    if self.owner.stop_event.is_set():return
                    raise
                except DirectUnavailable as exc:reason=str(exc)
                except (httpx.HTTPError,TimeoutError,UnicodeError) as exc:reason=type(exc).__name__
            else:raise DirectUnavailable(reason)
            self.confirmed=True
            try:
                await self.capture(url,segments,delay)
            except InterruptedError:
                if not self.owner.stop_event.is_set():raise
            except (httpx.HTTPError,TimeoutError,UnicodeError) as exc:
                raise DirectUnavailable('direct_connection_failed') from exc
            finally:
                await self.publish()

    async def capture(self,url,segments,delay):
        checked=0;last_sequence=None;configuration=None
        # shortcut: solicita HLS standard; formatos sem segmentos completos ficam indisponíveis até adicionar suporte.
        segments=segments[-2:]
        log.info('StripChat captura HTTP direta streamer=%s',self.owner.follow_username)
        while self.valid():
            for sequence,discontinuity,init_url,segment_url in segments:
                if last_sequence is not None and sequence<=last_sequence:continue
                if last_sequence is not None and sequence!=last_sequence+1:
                    # Segmentos removidos pelo CDN não significam que a live terminou.
                    log.warning('StripChat lacuna HLS streamer=%s anterior=%s proximo=%s ausentes=%s; captura mantida',
                                self.owner.follow_username,last_sequence,sequence,sequence-last_sequence-1)
                if time.monotonic()-checked>=3:
                    if not await self.refresh_access():return
                    checked=time.monotonic()
                if not self.valid():return
                if self.owner.flush_event.is_set():
                    await self.cut()
                    if not self.valid():return
                if not await self.allowed():
                    self.paused=True
                    return
                if configuration!=(discontinuity,init_url):
                    await self.publish()
                    self.init=await self.fetch(init_url) if init_url else None
                    if self.init and (self.init[4:8]!=b'ftyp' or any(
                            marker in self.init for marker in (b'encv',b'enca',b'sinf',b'pssh'))):
                        raise DirectUnavailable('unsupported_init')
                    configuration=(discontinuity,init_url)
                data=await self.fetch(segment_url)
                if self.init:
                    if data[4:8] not in (b'styp',b'sidx',b'moof',b'free'):
                        raise DirectUnavailable('invalid_mp4_segment')
                elif not data or len(data)%188 or any(data[i]!=0x47 for i in range(0,len(data),188)):
                    raise DirectUnavailable('unsupported_segment')
                if time.monotonic()-checked>=3:
                    if not await self.refresh_access():return
                    checked=time.monotonic()
                if not self.valid():return
                if self.owner.flush_event.is_set() or (self.file and (
                        self.file.tell()>=self.owner.segment_bytes or self.file.tell()+len(data)>self.owner.part_limit)):
                    await self.cut()
                    if not self.valid():return
                if not await self.allowed():
                    self.paused=True
                    return
                if not self.file:
                    await self.new_part('.mp4' if self.init else '.ts')
                    self.file=self.path.open('xb')
                    self.mime='video/mp4' if self.init else 'video/mp2t'
                    if not await self.refresh_access() or not self.valid() or not await self.allowed():return
                    checked=time.monotonic()
                    if self.init:self.owner._write(self.file,self.init)
                if self.file.tell()+len(data)>self.owner.part_limit:
                    raise DirectUnavailable('segment_exceeds_part_limit')
                self.owner._write(self.file,data);self.file.flush()
                last_sequence=sequence;self.last_data=time.monotonic()
            if time.monotonic()-self.last_data>30:raise DirectUnavailable('direct_stream_stalled')
            await self.owner.pause(delay)
            if self.owner.flush_event.is_set():
                await self.cut()
                if not self.valid():return
            if time.monotonic()-checked>=3:
                if not await self.refresh_access():return
                checked=time.monotonic()
            if not self.valid():return
            body=await self.fetch(url,1024*1024)
            variant,segments,delay=hls_playlist(body.decode('utf-8-sig'),url)
            if variant:raise DirectUnavailable('playlist_changed')
