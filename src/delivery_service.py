"""Spool temporario limitado, processamento e uma fila duravel de envios."""
import asyncio
from collections import deque
from datetime import datetime, timezone
from html import escape
import json
import logging
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time
import uuid

from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TelegramError

from project_runtime import media_tool
from platforms import PLATFORMS,split_profile,profile_url
from i18n import tr,date_format
import contact_sheets

log = logging.getLogger(__name__)
MIN_VIDEO_SECONDS=10


class SpoolBudget:
    def __init__(self, slots):
        self.slots = slots
        self.ids = set()
        self.condition = threading.Condition()

    def acquire(self, clip_id, stop, allowed=None):
        while True:
            # Membership checks use the event loop, which must remain free to release slots.
            if stop.is_set() or (allowed and not allowed()):raise InterruptedError()
            with self.condition:
                if len(self.ids)<self.slots:
                    self.ids.add(clip_id);return
                self.condition.wait(.2)

    def release(self, clip_id):
        with self.condition:
            self.ids.discard(clip_id)
            self.condition.notify_all()


class PartTooLarge(RuntimeError):
    pass


def bounded_output(proc,path,limit,quota,deadline):
    """O FFmpeg escreve em pipe; somente este consumidor pode aumentar o arquivo."""
    errors=[]
    stderr=deque(maxlen=32)
    success=False
    def write():
        try:
            with path.open('wb') as out:
                while data:=proc.stdout.read(64*1024):
                    if out.tell()+len(data)>limit:
                        raise PartTooLarge('output_exceeds_limit')
                    out.write(data)
        except Exception as exc:
            errors.append(exc)
    def read_errors():
        for line in iter(lambda:proc.stderr.read(4096),b''):
            stderr.append(line)
    writer=threading.Thread(target=write)
    reader=threading.Thread(target=read_errors)
    writer.start();reader.start()
    try:
        while proc.poll() is None:
            if errors:
                raise errors[0]
            if time.monotonic()>deadline or (quota and not quota()):
                raise RuntimeError('media_timeout_or_spool_limit')
            time.sleep(.1)
        writer.join();reader.join()
        if errors:
            raise errors[0]
        if proc.returncode:
            log.error('ffmpeg rc=%s detalhe=%s',proc.returncode,b''.join(stderr).decode('utf-8',errors='replace')[-1500:])
            raise RuntimeError('ffmpeg_failed')
        success=True
        return b''
    finally:
        if proc.poll() is None:
            proc.kill();proc.wait()
        writer.join();reader.join()
        if not success:
            path.unlink(missing_ok=True)


def run_media(arguments, quota=None, sink=None, timeout=180):
    flags = ({'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
             if os.name == 'nt' else {'start_new_session': True})
    # Saida do FFmpeg e limitada ao ultimo erro; nada fica no console.
    proc = subprocess.Popen(arguments, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **flags)
    deadline = time.monotonic() + timeout
    try:
        if sink:
            return bounded_output(proc,*sink,quota,deadline)
        while True:
            try:
                stdout,stderr = proc.communicate(timeout=.5)
                break
            except subprocess.TimeoutExpired:
                if time.monotonic() > deadline or (quota and not quota()):
                    raise RuntimeError('media_timeout_or_spool_limit')
        if quota and not quota():
            raise RuntimeError('spool_limit')
        if proc.returncode:
            log.error('ffmpeg rc=%s detalhe=%s',proc.returncode,stderr.decode('utf-8',errors='replace')[-1500:])
            raise RuntimeError('ffmpeg_failed')
        return stdout
    finally:
        if proc.poll() is None:
            proc.kill();proc.communicate()
        proc.stdout.close();proc.stderr.close()


def filter_path(path):
    # Escape both FFmpeg's option parser and its filtergraph parser; no shell is used.
    value=str(path.resolve()).replace('\\','/')
    value=''.join('\\'+c if c in "\\':" else c for c in value)
    return ''.join('\\'+c if c in "\\'[],;" else c for c in value)


def watermark_filter(folder,text):
    filename=folder/'watermark.txt';filename.write_text(text,encoding='utf-8')
    font=(Path(os.environ.get('WINDIR','C:/Windows'))/'Fonts/arial.ttf' if os.name=='nt'
          else Path('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'))
    if not font.is_file():raise RuntimeError('watermark_font_missing')
    return (f'drawtext=fontfile={filter_path(font)}:textfile={filter_path(filename)}:expansion=none:'
            "fontsize='max(18,min(w,h)/16)':fontcolor=white@0.65:borderw=1:bordercolor=black@0.4:"
            "x=(w-text_w)/2:y=(h-text_h)/2:enable='lt(t,30)'")


def process_clip(row, max_bytes, spool=None, spool_bytes=None,*,watermark_text='',output_folder=None):
    """MP4 H.264/HEVC e AAC: copia os codecs compatíveis e converte somente o necessário."""
    raw = Path(row['raw_path'])
    meta = json.loads(row['metadata'])
    folder = Path(output_folder) if output_folder is not None else raw.parent
    folder.mkdir(parents=True,exist_ok=True)
    def quota():
        if spool is None:
            return True
        total = 0
        for path in spool.rglob('*'):
            try:
                if path.is_file():
                    total += path.stat().st_size
            except FileNotFoundError:
                pass
        return total <= spool_bytes
    timeout=max(180,min(3600,meta.get('seconds',0)))
    if watermark_text:timeout=max(1800,min(6*3600,meta.get('seconds',0)*6))
    def run(args):
        return run_media(args,quota,timeout=timeout)
    def bounded(args):
        path=Path(args[-1])
        fmt='mp4' if path.suffix=='.mp4' else 'matroska'
        command=[media_tool('ffmpeg'),'-hide_banner','-loglevel','error','-nostdin','-y',*args[:-1]]
        if watermark_text:command[1:1]=['-xerror','-filter_threads','1']
        if fmt=='mp4':
            command+=['-movflags','+frag_keyframe+empty_moov+default_base_moof','-frag_duration','2000000']
        else:
            command+=['-cluster_time_limit','2000']
        command+=['-f',fmt,'pipe:1']
        run_media(command,quota,sink=(path,max_bytes),timeout=timeout)
        return True
    streams=json.loads(run([media_tool('ffprobe'),'-v','error','-show_streams','-of','json',str(raw)]))['streams']
    video=next(s for s in streams if s['codec_type']=='video')
    audio=next((s for s in streams if s['codec_type']=='audio'),None)
    normalize=meta.get('normalize') and meta.get('video_size')
    copy_video=(not watermark_text and not normalize and (
        video['codec_name']=='h264' and video.get('pix_fmt') in ('yuv420p','yuvj420p') or
        video['codec_name']=='hevc' and video.get('pix_fmt') in ('yuv420p','yuvj420p','yuv420p10le')))
    video_args=(['-c:v','copy'] if copy_video else
                ['-c:v','libx264','-threads','1' if watermark_text else '2','-preset','veryfast','-crf','20','-pix_fmt','yuv420p'])
    if copy_video and video['codec_name']=='hevc':video_args+=['-tag:v','hvc1']
    audio_args=['-c:a','copy'] if audio and audio['codec_name']=='aac' else ['-c:a','aac','-b:a','160k']
    mark=watermark_filter(folder,watermark_text) if watermark_text else ''
    filters=[]
    if normalize:
        w,h=(max(2,int(n)//2*2) for n in meta['video_size'])
        filters=['-vf',f'scale={w}:{h}:force_original_aspect_ratio=decrease:force_divisible_by=2,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1']
    elif not copy_video:
        filters=['-vf','pad=ceil(iw/2)*2:ceil(ih/2)*2']
    if mark:filters[1]+=','+mark
    output=folder/'media.mp4'
    args=['-fflags','+genpts',*(['-threads','1'] if mark else []),'-i',str(raw),'-map','0:v:0','-map','0:a:0?',
          *video_args,*audio_args,*filters]
    if mark:args+=['-bf','0','-fps_mode:v','passthrough','-enc_time_base:v','demux','-noautoscale']
    try:
        bounded(args+[str(output)])
    except PartTooLarge:
        # O bruto permanece intacto: repete o processamento em intervalos menores.
        duration=media_duration(raw,run)
        offset=0.0
        window=duration/2
        outputs=[]
        while offset<duration-.001:
            seconds=min(window,duration-offset)
            path=folder/f'piece-{len(outputs):05d}.mp4'
            seek=(['-threads','1','-ss',str(offset),'-i',str(raw)] if mark else ['-i',str(raw),'-ss',str(offset)])
            args=[*seek,'-t',str(seconds),'-map','0:v:0','-map','0:a?',
                  '-c:v','libx264','-threads','1' if mark else '2','-preset','veryfast','-crf','23',
                  '-pix_fmt','yuv420p','-c:a','aac','-b:a','128k']
            if mark:args[args.index('-crf')+1]='20'
            if meta.get('normalize') and meta.get('video_size'):
                w,h=(max(2,int(n)//2*2) for n in meta['video_size'])
                args+=['-vf',f'scale={w}:{h}:force_original_aspect_ratio=decrease:force_divisible_by=2,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1']
            else:
                args+=['-vf','pad=ceil(iw/2)*2:ceil(ih/2)*2']
            if mark:
                args[args.index('-vf')+1]+=",setpts=PTS-STARTPTS,"+mark
                args+=['-bf','0','-fps_mode:v','passthrough','-enc_time_base:v','demux','-noautoscale']
            try:
                bounded(args+[str(path)])
            except PartTooLarge:
                window=seconds/2
                if window<.05:
                    raise RuntimeError('cannot_fit_video_frame_in_part')
                continue
            outputs.append((path,'video'))
            offset+=seconds
        return outputs
    if output.stat().st_size <= max_bytes:
        return [(output, 'video')]
    raise RuntimeError('part_exceeds_limit')


def media_duration(path,run=run_media):
    probe=json.loads(run([media_tool('ffprobe'),'-v','error','-show_format','-of','json',str(path)]))
    duration=float(probe['format'].get('duration') or 0)
    if duration<=0:
        progress=run([media_tool('ffmpeg'),'-v','error','-nostdin','-i',str(path),'-map','0',
                      '-c','copy','-f','null','-','-progress','pipe:1','-nostats']).decode()
        values=re.findall(r'^out_time_us=(\d+)$',progress,re.MULTILINE)
        duration=max((int(n)/1e6 for n in values),default=0)
    if duration<=0:
        raise RuntimeError('unknown_media_duration')
    return duration


def probe_video(path):
    return json.loads(run_media([media_tool('ffprobe'),'-v','error','-count_packets',
        '-show_streams','-show_format','-of','json',str(path)]))


def video_info(path,probe=None):
    probe=probe if probe is not None else probe_video(path)
    video=next(s for s in probe['streams'] if s['codec_type']=='video')
    duration=float(probe['format'].get('duration') or 0) or media_duration(path)
    return dict(duration=max(1,math.ceil(duration)),width=video['width'],height=video['height'],duration_seconds=duration)


def watermark_outputs(outputs,info,text,max_bytes,root,spool_bytes):
    marked=[]
    for index,(source,kind) in enumerate(outputs):
        if kind!='video' or info[str(source)]['duration_seconds']<MIN_VIDEO_SECONDS:
            marked.append((source,kind));continue
        folder=source.parent/f'watermark-{index:05d}';folder.mkdir(exist_ok=True)
        for old in folder.glob('*.mp4'):old.unlink()
        row=dict(raw_path=str(source),metadata=json.dumps(dict(seconds=info[str(source)]['duration_seconds'])))
        marked.extend(process_clip(row,max_bytes,root,spool_bytes,watermark_text=text,output_folder=folder))
        # The unmarked capture remains intact; this is only the intermediate prepared video.
        source.unlink()
    return marked


def prepare_videos(outputs,max_bytes):
    """Fecha um MP4 comum com moov no inicio; preserva a origem ate o envio confirmado."""
    prepared=[]
    info={}
    for source,kind in outputs:
        path=source
        if kind=='video':
            original=probe_video(source)
            candidate=source.with_name(source.stem+'.ready.mp4')
            try:
                run_media([media_tool('ffmpeg'),'-hide_banner','-loglevel','error','-nostdin','-y',
                    '-i',str(source),'-map','0','-c','copy','-movflags','+faststart',
                    '-fs',str(max(1,max_bytes-min(1024*1024,max_bytes//100))),str(candidate)])
                final=probe_video(candidate)
                def packets(data):
                    return [(s['codec_type'],s['codec_name'],s.get('nb_read_packets')) for s in data['streams']]
                if candidate.stat().st_size>max_bytes or packets(final)!=packets(original):
                    raise RuntimeError('remux_incomplete_or_oversized')
                path=candidate
                original=final
            except Exception as exc:
                candidate.unlink(missing_ok=True)
                log.warning('faststart indisponivel; origem preservada arquivo=%s erro=%s',source.name,type(exc).__name__)
            info[str(path)]=video_info(path,original)
        prepared.append((path,kind))
    return prepared,info


def process_recoverably(row,max_bytes,root,spool_bytes):
    try:
        return process_clip(row,max_bytes,root,spool_bytes)
    except Exception as exc:
        log.warning('tentando recuperar parte=%s erro=%s',row['id'],type(exc).__name__)
        raw=Path(row['raw_path'])
        recovered=raw.with_name('recovered.mkv')
        def quota():
            total=0
            for path in root.rglob('*'):
                try:
                    if path.is_file():
                        total+=path.stat().st_size
                except FileNotFoundError:
                    pass
            return total<=spool_bytes
        run_media([media_tool('ffmpeg'),'-hide_banner','-loglevel','error','-nostdin',
            '-fflags','+genpts+discardcorrupt','-err_detect','ignore_err','-i',str(raw),
            '-map','0:v:0','-map','0:a?','-c','copy','-f','matroska','pipe:1'],
            sink=(recovered,max_bytes),quota=quota)
        copy=dict(row,raw_path=str(recovered))
        result=process_clip(copy,max_bytes,root,spool_bytes)
        log.info('parte recuperada id=%s',row['id'])
        return result


class DeliveryService:
    def __init__(self, config, store, bot):
        self.config, self.store, self.bot = config, store, bot
        self.root = config.spool.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.budget = SpoolBudget(config.spool_slots)
        self.loop = None
        self.closed = False
        self.tasks = []
        self.inflight = None
        self.blocked_callback = None
        self.pending_deletes = {}
        self.alerted={}
        self.backing_up=False
        self.processing=None
        self.can_process=lambda:True
        self.sheets=contact_sheets.initialize(config.database.parent/'sheets')

    async def alert(self,key,message):
        now=time.monotonic()
        if now-self.alerted.get(key,-1e9)<900:return
        self.alerted[key]=now
        log.warning('operational_alert key=%s',key)
        uid=self.config.superuser_id
        if not uid:return
        row=self.store.db.execute('SELECT chat_id FROM users WHERE user_id=? AND blocked=0',(uid,)).fetchone()
        if row:
            try:await self.bot.send_message(chat_id=row['chat_id'],text='⚠️ '+tr(self.store.language(uid),message))
            except TelegramError as exc:log.warning('alert_delivery_failed error=%s',type(exc).__name__)

    def _folder(self, clip_id):
        if not re.fullmatch(r'[0-9a-f]{32}', clip_id):
            raise ValueError('invalid_clip_id')
        path = self.root / clip_id
        # Toda remocao recursiva fica restrita a um UUID direto do spool conhecido.
        if path.is_symlink() or path.resolve().parent != self.root:
            raise ValueError('invalid_spool_path')
        return path

    def _delete(self, clip_id):
        if self.pending_deletes.get(clip_id,0) > time.time():
            return False
        path = self._folder(clip_id)
        try:
            if path.exists():
                shutil.rmtree(path)
        except OSError:
            self.pending_deletes[clip_id] = time.time()+10
            log.warning('limpeza adiada: arquivo em uso id=%s',clip_id)
            return False
        self.pending_deletes.pop(clip_id,None)
        self.budget.release(clip_id)
        return True

    def begin_part(self, stop, allowed=None):
        clip_id = uuid.uuid4().hex
        waiting = len(self.budget.ids) >= self.budget.slots
        if waiting:
            log.warning('spool cheio: captura aguardando espaco; podem existir lacunas')
        self.budget.acquire(clip_id, stop, allowed)
        folder = self._folder(clip_id)
        try:
            folder.mkdir()
        except Exception:
            self.budget.release(clip_id)
            raise
        return folder / 'capture'

    def segment_ready(self, path, meta):
        # O thread da captura somente entrega metadados; o loop nunca aguarda Playwright.
        future = asyncio.run_coroutine_threadsafe(self._accept(path, meta), self.loop)
        future.result(timeout=30)

    async def _accept(self, path, meta):
        clip_id = path.parent.name
        if path.stat().st_size <= 2048 or not self.store.recipients(meta['username'],meta['live_id'],clip_id):
            self._delete(clip_id)
            log.info('parte descartada id=%s motivo=sem_dados_ou_inscritos',clip_id)
            return
        self.store.register_clip(clip_id, meta['username'], meta['live_id'], meta['started'] or time.time(),
                                 path, meta, self.config.retention_seconds)
        log.info('parte capturada id=%s streamer=%s live=%s bytes=%s',
                 clip_id, meta['username'], meta['live_id'], path.stat().st_size)

    async def start(self):
        self.loop = asyncio.get_running_loop()
        self.store.recover()
        for media in self.store.pending_video_metadata():
            path=Path(media['path'])
            if path.is_file():
                try:
                    prepared,info=await asyncio.to_thread(prepare_videos,[(path,'video')],self.config.part_bytes)
                    final=prepared[0][0]
                    self.store.video_metadata(media['id'],final,info[str(final)])
                except Exception as exc:
                    self.store.fail_media(media['id'],'metadata_recovery_failed')
                    log.error('midia pendente preservada; metadados indisponiveis parte=%s erro=%s',media['id'],type(exc).__name__)
        active = {r['id']: r for r in self.store.active_clips()}
        for path in self.root.iterdir():
            if re.fullmatch(r'[0-9a-f]{32}', path.name) and path.name not in active:
                if any(p.suffix in {'.mkv','.mp4','.webm','.ts'} for p in path.rglob('*') if p.is_file()):
                    self.budget.ids.add(path.name)
                    log.error('midia sem metadados preservada para recuperacao id=%s',path.name)
                    continue
                self._delete(path.name)
                log.warning('temporario sem registro removido id=%s', path.name)
        for clip_id, row in active.items():
            self.budget.ids.add(clip_id)
            folder = self._folder(clip_id)
            if row['status'] == 'raw' and (not row['raw_path'] or not Path(row['raw_path']).is_file()):
                self.store.set_clip(clip_id, 'failed', 'missing_temporary')
                self._delete(clip_id)
            elif row['status'] == 'raw':
                # Saidas parciais de um processamento interrompido podem ser refeitas.
                for path in folder.iterdir():
                    if path.is_file() and path != Path(row['raw_path']):
                        path.unlink()
        await self.cleanup()
        self.tasks = [asyncio.create_task(self._process_loop(), name='media-process'),
                      asyncio.create_task(self._send_loop(), name='media-send'),
                      asyncio.create_task(self._cleanup_loop(), name='media-cleanup')]

    async def _process_loop(self):
        while not self.closed:
            if self.backing_up or (self.config.watermark_active and not self.can_process()):
                await asyncio.sleep(.2)
                continue
            row = self.store.pending_clip()
            if row is None:
                await asyncio.sleep(.2)
                continue
            clip_id = row['id']
            self.processing=clip_id
            self.store.set_clip(clip_id, 'processing')
            try:
                outputs = await asyncio.to_thread(process_recoverably, dict(row), self.config.part_bytes,
                                                  self.root,self.config.spool_bytes)
                if self.config.watermark_active:
                    def inspect():return {str(path):video_info(path) for path,kind in outputs if kind=='video'}
                    info=await asyncio.to_thread(inspect)
                else:outputs,info=await asyncio.to_thread(prepare_videos,outputs,self.config.part_bytes)
                meta=json.loads(row['metadata'])
                platform,username=split_profile(row['username'])
                profile_id=str(meta.get('profile_id') or self.store.platform_id(row['username']))
                members=self.store.capture_snapshot(clip_id,row['username'])
                if members is None:
                    members=self.store.db.execute('SELECT user_id FROM clip_recipients WHERE clip_id=?',(clip_id,)).fetchall()
                ended=meta.get('ended') or row['started']+float(meta.get('seconds') or 0)
                record=dict(recording_id=clip_id,live_id=row['live_id'],part_number=row['part_no'],
                    title=meta.get('title') or None,profile_name=self.store.profile_name(row['username']) or username,
                    profile_url=profile_url('profile/'+profile_id) if platform=='superlive' and profile_id else profile_url(row['username']),
                    profile_id=profile_id,platform=platform,
                    recording_started_at=datetime.fromtimestamp(row['started'],timezone.utc).isoformat(),
                    recording_ended_at=datetime.fromtimestamp(ended,timezone.utc).isoformat(),
                    telegram_user_ids=json.dumps(sorted({m['user_id'] for m in members})))
                await asyncio.to_thread(contact_sheets.create_sheet,self.sheets,record,outputs,info,run_media)
                if self.config.watermark_active:
                    outputs=await asyncio.to_thread(watermark_outputs,outputs,info,self.config.watermark_text,
                                                   self.config.part_bytes,self.root,self.config.spool_bytes)
                    outputs,info=await asyncio.to_thread(prepare_videos,outputs,self.config.part_bytes)
                    await asyncio.to_thread(contact_sheets.set_video_count,self.sheets,clip_id,len(outputs))
                self.store.processed(clip_id, outputs,info)
                log.info('parte processada id=%s partes=%s bytes=%s', clip_id,len(outputs),
                         sum(p.stat().st_size for p,_ in outputs))
            except Exception as exc:
                self.store.set_clip(clip_id, 'failed', type(exc).__name__)
                log.error('recuperacao falhou; origem preservada id=%s erro=%s', clip_id,type(exc).__name__)
                await self.alert('processing','A recording could not be processed. Its source file has been preserved for recovery.')
            finally:self.processing=None

    async def _send_loop(self):
        while not self.closed:
            if self.backing_up:
                await asyncio.sleep(.2)
                continue
            row = self.store.next_delivery()
            if row is None:
                await asyncio.sleep(.2)
                continue
            self.inflight = row['clip_id']
            try:
                await self.send(row)
            finally:
                self.inflight = None
            await self.cleanup()

    async def send(self, row):
        self.store.sending(row)
        reused = bool(row['file_id'])
        try:
            media = row['file_id'] or Path(row['path'])
            if not reused and not media.is_file():
                self.store.delivery(row, 'failed', error='missing_temporary')
                return
            info=json.loads(row['info'])
            seconds=info.pop('duration_seconds',None)
            if row['kind']=='video' or Path(row['path']).suffix.lower() in {'.mp4','.mkv','.webm'}:
                if seconds is None:
                    path=Path(row['path'])
                    if path.is_file():
                        seconds=await asyncio.to_thread(media_duration,path)
                        self.store.video_metadata(row['media_id'],path,dict(info,duration_seconds=seconds))
                    elif reused and info.get('duration',0)>MIN_VIDEO_SECONDS:
                        # Metadados antigos foram arredondados para cima; use o limite inferior seguro.
                        seconds=info['duration']-1
                    else:
                        raise RuntimeError('unknown_media_duration')
                if not math.isfinite(seconds) or seconds<=0:
                    raise RuntimeError('unknown_media_duration')
                if seconds<MIN_VIDEO_SECONDS:
                    self.store.delivery(row,'skipped',error='below_minimum_duration')
                    log.info('envio dispensado parte=%s streamer=%s user_id=%s duracao=%.3fs minimo=%ss',
                             row['media_id'],row['username'],row['user_id'],seconds,MIN_VIDEO_SECONDS)
                    return
            if not reused and media.stat().st_size > min(500_000_000,self.config.part_bytes):
                self.store.delivery(row,'failed',error='file_exceeds_size_limit')
                log.error('upload bloqueado: tamanho excedido parte=%s',row['media_id'])
                return
            # 500 MB podem levar varios minutos mesmo com uma conexao saudavel.
            timeout = 180 if reused else 1800
            platform,username=split_profile(row['username'])
            profile_id=self.store.platform_id(row['username'])
            language=self.store.language(row['user_id'])
            name=self.store.profile_name(row['username'])
            if not name:name=('ID '+username.split('/',1)[1]) if platform=='superlive' and username.startswith('profile/') else '@'+username
            caption=f'👤 <code>{escape(name)}</code>\n'+tr(language,'Platform: {platform}',platform=PLATFORMS[platform])
            if profile_id:caption+='\n'+tr(language,'Profile ID: {profile_id}',profile_id=escape(profile_id))
            caption+='\n'+tr(language,'Recording started: {date}',date=datetime.fromtimestamp(row['started']).strftime(date_format(language)+':%S'))
            if seconds is not None:
                elapsed=max(0,math.ceil(seconds))
                caption+='\n'+tr(language,'Duration: {duration}',duration=f'{elapsed//3600:02d}:{elapsed//60%60:02d}:{elapsed%60:02d}')
            kwargs = dict(chat_id=row['chat_id'], caption=caption,parse_mode='HTML',
                          read_timeout=timeout, write_timeout=timeout, connect_timeout=15, pool_timeout=15)
            if row['kind'] == 'video':
                message = await self.bot.send_video(video=media, supports_streaming=True,
                                                    **info,**kwargs)
                file_id = message.video.file_id
            else:
                message = await self.bot.send_document(document=media, **kwargs)
                file_id = message.document.file_id
            self.store.delivery(row, 'sent', message_id=message.message_id, file_id=file_id)
            log.info('envio confirmado parte=%s streamer=%s user_id=%s chat_id=%s message_id=%s reutilizado=%s',
                     row['media_id'],row['username'],row['user_id'],row['chat_id'],message.message_id,reused)
            # A primeira confirmacao basta: os proximos destinatarios usam file_id.
            if not reused:
                try:
                    Path(row['path']).unlink(missing_ok=True)
                except OSError:
                    log.warning('limpeza aguardara liberacao do arquivo parte=%s',row['media_id'])
        except Forbidden:
            self.store.block(row['user_id'])
            log.warning('bot bloqueado user_id=%s chat_id=%s',row['user_id'],row['chat_id'])
            if self.blocked_callback:
                self.blocked_callback()
        except RetryAfter as exc:
            seconds = exc.retry_after.total_seconds() if hasattr(exc.retry_after,'total_seconds') else exc.retry_after
            self.store.delivery(row, 'retry', delay=float(seconds)+1, error='rate_limit')
            log.warning('rate_limit parte=%s user_id=%s espera=%s',row['media_id'],row['user_id'],seconds)
        except BadRequest:
            self.store.delivery(row, 'failed', error='telegram_bad_request')
            log.error('envio rejeitado parte=%s user_id=%s',row['media_id'],row['user_id'])
        except NetworkError as exc:
            # A Bot API nao tem chave de idempotencia. Timeout pode ocorrer DEPOIS do envio.
            cause = exc.__cause__
            safe = cause is not None and type(cause).__name__ in {'ConnectError','ConnectTimeout','PoolTimeout'}
            if safe and row['attempts'] < 4:
                self.store.delivery(row, 'retry', delay=min(60,2**(row['attempts']+1)),error=type(exc).__name__)
            elif safe:
                self.store.delivery(row, 'failed', error='connection_retries_exhausted')
            else:
                self.store.delivery(row, 'uncertain', error=type(exc).__name__)
            log.error('falha rede parte=%s user_id=%s erro=%s retentativa_segura=%s',
                      row['media_id'],row['user_id'],type(exc).__name__,safe)
        except TelegramError as exc:
            self.store.delivery(row, 'failed', error=type(exc).__name__)
            log.error('envio falhou parte=%s user_id=%s erro=%s',row['media_id'],row['user_id'],type(exc).__name__)
        except Exception as exc:
            self.store.delivery(row, 'uncertain', error=type(exc).__name__)
            log.error('envio sem confirmacao parte=%s user_id=%s erro=%s',row['media_id'],row['user_id'],type(exc).__name__)

    async def cleanup(self):
        if self.backing_up:return
        overdue=self.store.db.execute("SELECT COUNT(*) FROM clips WHERE expires<? AND status IN ('raw','ready','failed')",(time.time(),)).fetchone()[0]
        if overdue:await self.alert('overdue','Some recordings are taking longer than expected to send. Pending files are preserved; new recordings will wait if the delivery queue fills.')
        if self.backing_up:return
        for row in self.store.cleanup_candidates():
            if row['id'] == self.inflight:
                continue
            expired = row['expires'] < time.time()
            if expired:
                log.warning('prazo de recuperacao encerrado; removendo temporarios id=%s',row['id'])
            self.store.expire(row['id'])
            if not self._delete(row['id']):
                continue
            self.store.set_clip(row['id'], 'expired' if expired else 'done')
            log.info('temporarios removidos id=%s motivo=%s',row['id'],'prazo' if expired else 'fila concluida')
        # A pasta de arquivos auxiliares do servidor e separada dos bancos TDLib.
        # Apos a confirmacao, o file_id referencia o Telegram e dispensa esse cache.
        if self.config.api_mode == 'local' and self.inflight is None and not self.store.unconfirmed_uploads():
            cache = self.config.api_files.resolve()
            removed = size = 0
            for path in cache.rglob('*'):
                try:
                    if path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(cache):
                        length = path.stat().st_size
                        path.unlink()
                        removed += 1
                        size += length
                except OSError:
                    pass # Arquivos ainda em uso serao tentados na proxima passagem.
            if removed:
                log.info('cache de midia Bot API removido arquivos=%s bytes=%s',removed,size)

    async def _cleanup_loop(self):
        while not self.closed:
            for clip_id in list(self.pending_deletes):
                self._delete(clip_id)
            await self.cleanup()
            await asyncio.sleep(1)

    async def close(self):
        self.closed = True
        # Nao cancela uploads nem FFmpeg no meio: aguarda a resposta e persiste o estado.
        if self.tasks:
            results = await asyncio.gather(*self.tasks,return_exceptions=True)
            for result in results:
                if isinstance(result,Exception):
                    log.error('servico de midia encerrou com erro=%s',type(result).__name__)
        await self.cleanup()
