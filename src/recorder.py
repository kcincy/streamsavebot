"""Motor de captura WebRTC com identidade estrita da conta e da transmissao."""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from project_runtime import configure_runtime, media_tool

configure_runtime()

try:
    from playwright.sync_api import Error as PWError
    from playwright.sync_api import TimeoutError as PWTimeout
    from playwright.sync_api import sync_playwright
except ImportError as exc:  # pragma: no cover
    raise RuntimeError("Execute scripts/setup.ps1 para instalar as dependencias locais.") from exc

BASE = "https://superlivechat.tv"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Mime types em ordem de preferencia: h264 permite remux para mp4 sem transcode.
MIME_TYPES = [
    "video/webm;codecs=h264,opus",
    "video/webm;codecs=vp8,opus",
    "video/webm;codecs=vp9,opus",
    "video/webm",
]

STOP = threading.Event()
PROFILE_POLL_SECONDS = 15.0


def media_eval(page,expression,arg=None,timeout=5000):
    # evaluate() nao limita promessas pendentes; o prazo pertence ao driver Playwright.
    handle=page.wait_for_function(f'async (arg) => ({{value: await ({expression})}})',
                                  arg=arg,timeout=timeout,polling=100)
    try:
        return handle.json_value()['value']
    finally:
        handle.dispose()


# ---------------------------------------------------------------------------
# JS injetado antes de qualquer script da pagina.
# Associa o player confirmado aos tracks RTC de video e audio do mesmo grupo.
# ---------------------------------------------------------------------------
INIT_JS = r"""
(() => {
  if (window.__slRec) return;
  const S = window.__slRec = {
    rec: null, chunks: [], finished: null, pendingBytes: 0,
    state: 'idle', reason: '', startedAt: 0, bytes: 0, mimeType: '', timer: null,
    video: null, audio: null, peers: [], armed: false, observer: null,
    livestreamId: '', channelId: '', quiet: false
  };

  const raf=window.requestAnimationFrame.bind(window);
  window.requestAnimationFrame=callback=>raf(time=>{if(!S.quiet)callback(time);});
  const play=HTMLMediaElement.prototype.play;
  HTMLMediaElement.prototype.play=function(...args){
    return S.quiet ? Promise.resolve() : play.apply(this,args);
  };
  const videoFrame=HTMLVideoElement.prototype.requestVideoFrameCallback;
  if(videoFrame)HTMLVideoElement.prototype.requestVideoFrameCallback=function(callback){
    return videoFrame.call(this,(...args)=>{if(!S.quiet)callback(...args);});
  };
  S.quietPreview=()=>{
    S.quiet=true;
    document.querySelectorAll('video,audio').forEach(element=>{if(!element.paused)element.pause();});
    return true;
  };

  S.arm = ({livestreamId, channelId}) => {
    if (!livestreamId || !channelId) throw new Error('sala sem identidade completa');
    S.livestreamId = String(livestreamId); S.channelId = String(channelId); S.armed = true;
  };

  const remoteDescription = RTCPeerConnection.prototype.setRemoteDescription;
  RTCPeerConnection.prototype.setRemoteDescription = function (...args) {
    if (!S.peers.includes(this)) S.peers.push(this);
    return remoteDescription.apply(this, args);
  };
  const sources = new WeakMap();
  const srcObject = Object.getOwnPropertyDescriptor(HTMLMediaElement.prototype, 'srcObject');
  Object.defineProperty(HTMLMediaElement.prototype, 'srcObject', {
    configurable: true,
    get() { return srcObject.get.call(this); },
    set(stream) {
      // O site troca o video por um track processado; preserva o RTC deste elemento.
      const video = stream?.getVideoTracks?.()[0];
      const receiver = video && S.peers.flatMap(pc => pc.getReceivers()).find(r => r.track === video);
      if (receiver) sources.set(this, {track: receiver.track, channelId: this.dataset.pipChannel || ''});
      return srcObject.set.call(this, stream);
    }
  });

  S.videoStats = async () => {
    const v = S.video;
    if (!v) return { frames: null };
    for (const pc of S.peers) {
      const receiver = pc.getReceivers().find(r => r.track === v);
      if (!receiver) continue;
      try {
        const reports = await receiver.getStats();
        let frames = 0, width = 0, height = 0, found = false;
        for (const r of reports.values()) {
          if (r.type !== 'inbound-rtp' || r.kind !== 'video') continue;
          found = true;
          frames += Math.max(r.framesReceived ?? 0,r.framesDecoded ?? 0);
          if (r.frameWidth && r.frameHeight) { width = r.frameWidth; height = r.frameHeight; }
        }
        if (found) return { frames, width, height };
      } catch (e) {}
    }
    const element = Array.from(document.querySelectorAll('video')).find(e =>
      e.srcObject && e.srcObject.getVideoTracks().includes(v));
    return { frames: element ? element.getVideoPlaybackQuality().totalVideoFrames : null,
      width: element ? element.videoWidth : 0, height: element ? element.videoHeight : 0 };
  };

  const pickElement = () => {
    if (!S.armed) return null;
    const root = document.getElementById('teleport-video-' + S.livestreamId)
      || document.getElementById('video-container-' + S.livestreamId);
    if (!root) return null;
    const videos = Array.from(root.querySelectorAll('video')).filter(v =>
      v.dataset.pipChannel === S.channelId && v.srcObject
      && v.srcObject.getVideoTracks().some(t => t.readyState === 'live'));
    return videos.length === 1 ? videos[0] : null;
  };
  const pickVideo = () => {
    const element = pickElement();
    const original = element && sources.get(element);
    if (original && original.channelId !== S.channelId) return null;
    const tracks = original ? [original.track] : element?.srcObject.getVideoTracks() || [];
    return tracks.find(t => {
      const s = t.getSettings();
      return t.readyState === 'live' && s.width && s.height;
    }) || null;
  };
  const pickAudio = (video) => {
    if (!video) return null;
    const element = pickElement();
    const direct = element && element.srcObject.getAudioTracks().find(t => t.readyState === 'live');
    if (direct) return direct;
    for (const pc of S.peers) {
      const receivers = pc.getReceivers();
      if (!receivers.some(r => r.track === video)) continue;
      // Os msids do SDP associam o audio ao video, inclusive em salas com convidados.
      const groups = new Map();
      for (const line of (pc.remoteDescription?.sdp || '').split(/\r?\n/)) {
        const m = line.match(/^(?:a=ssrc:\d+ msid:|a=msid:)(\S+)\s+(\S+)/);
        if (m) groups.set(m[2], m[1]);
      }
      const group = groups.get(video.id);
      const audio = receivers.map(r => r.track).filter(t => t.kind === 'audio'
        && t.readyState === 'live' && group && groups.get(t.id) === group);
      return audio.length === 1 ? audio[0] : null;
    }
    return null;
  };
  const bound = () => {
    return pickVideo() === S.video;
  };

  S.info = () => {
    const v = S.video || pickVideo();
    const s = v ? v.getSettings() : {};
    return {
      state: S.state, width: s.width || 0, height: s.height || 0,
      hasVideo: !!v && v.readyState === 'live', hasAudio: !!(S.audio || pickAudio(v)), bytes: S.bytes, reason: S.reason,
      mimeType: S.mimeType, identityValid: !S.video || !!bound(),
      videoTrackId: v?.id || '', audioTrackId: (S.audio || pickAudio(v))?.id || '',
      livestreamId: S.livestreamId, channelId: S.channelId
    };
  };

  S.start = (opts) => {
    if (!S.armed) return { ok: false, err: 'identidade da live nao confirmada' };
    if (S.rec) return { ok: false, err: 'ja gravando' };
    const v = pickVideo();
    if (!v) return { ok: false, err: 'sem track de video' };
    const tracks = [v];
    const a = pickAudio(v);
    if (a) tracks.push(a);
    S.video = v; S.audio = a;
    const st = new MediaStream(tracks);
    const types = (opts && opts.mimeTypes) || [];
    const mimeType = types.find((t) => MediaRecorder.isTypeSupported(t));
    // Sem mimeType, o Chromium preserva o video remoto sem reencode (passthrough).
    const cfg = { audioBitsPerSecond: 128000 };
    if (opts && opts.videoBitrate > 0) {
      cfg.videoBitsPerSecond = opts.videoBitrate;
      if (mimeType) cfg.mimeType = mimeType;
    }
    try { S.rec = new MediaRecorder(st, cfg); }
    catch (e) { S.rec = null; return { ok: false, err: String(e) }; }
    S.chunks = []; S.pendingBytes = 0; S.bytes = 0; S.reason = ''; S.state = 'recording'; S.startedAt = Date.now();
    S.mimeType = S.rec.mimeType || '';
    const rec = S.rec;
    rec.ondataavailable = (e) => {
      S.mimeType = rec.mimeType || S.mimeType;
      if (e.data && e.data.size) {
        if (S.pendingBytes + e.data.size > 8 * 1024 * 1024) {
          S.reason = 'buffer RTC excedeu 8 MiB'; S.stop(); return;
        }
        S.chunks.push(e.data); S.bytes += e.data.size; S.pendingBytes += e.data.size;
      }
    };
    rec.onerror = (e) => { S.reason = String(e.error || 'erro no MediaRecorder'); S.stop(); };
    S.rec.start(1000);
    // O track RTC vai direto ao gravador; a previa DOM nao precisa ser reproduzida.
    if (opts?.lowResource !== false && S.peers.some(pc => pc.getReceivers().some(r => r.track === v))) {
      S.quietPreview();
    }
    const checkIdentity = () => {
      if (S.rec && !bound()) { S.reason = 'identidade do player mudou'; S.stop(); }
    };
    S.observer = new MutationObserver(checkIdentity);
    S.observer.observe(document.documentElement, {subtree: true, childList: true,
      attributes: true, attributeFilter: ['id', 'data-pip-channel']});
    S.timer = setInterval(() => {
      if (!S.rec) return;
      if (S.quiet) S.quietPreview();
      checkIdentity();
      if (v.readyState !== 'live') { S.reason = 'track de video encerrou'; S.stop(); }
      else if (a && a.readyState !== 'live') { S.reason = 'track de audio encerrou'; S.stop(); }
    }, 1000);
    return { ok: true, mimeType: S.mimeType, hasAudio: !!a };
  };

  S.stop = () => {
    if (S.observer) { S.observer.disconnect(); S.observer = null; }
    if (S.timer) { clearInterval(S.timer); S.timer = null; }
    if (!S.rec) { S.state = 'stopped'; return S.finished || Promise.resolve({ ok: true }); }
    const rec = S.rec; S.rec = null; S.state = 'stopped';
    S.finished = new Promise(resolve => {
      rec.addEventListener('stop', () => resolve({ ok: true }), {once: true});
      try { rec.stop(); } catch (e) { resolve({ ok: true }); }
    });
    return S.finished;
  };

  S.drain = async () => {
    if (!S.chunks.length) return '';
    const blobs = S.chunks.splice(0, S.chunks.length);
    S.pendingBytes = 0;
    const bufs = await Promise.all(blobs.map((b) => b.arrayBuffer()));
    let len = 0;
    for (const b of bufs) len += b.byteLength;
    const out = new Uint8Array(len);
    let off = 0;
    for (const b of bufs) { out.set(new Uint8Array(b), off); off += b.byteLength; }
    let s = '';
    for (let i = 0; i < out.length; i += 8192) {
      s += String.fromCharCode.apply(null, out.subarray(i, i + 8192));
    }
    return btoa(s);
  };
})();
"""


# ---------------------------------------------------------------------------
# util
# ---------------------------------------------------------------------------
def log(msg: str, *, tag: str = "") -> None:
    logging.getLogger(__name__).info("[%s] %s", tag, msg)


def sanitize(name: str, fallback: str = "live") -> str:
    name = re.sub(r"[^\w\-.]+", "_", name, flags=re.UNICODE).strip("._-")
    return (name or fallback)[:60]


def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}GB"


def human_time(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def reserve_recording_base(root: Path, username: str, started: datetime) -> tuple[Path, Path]:
    username = sanitize(username)
    folder = root / username / started.strftime("%d.%m.%Y")
    folder.mkdir(parents=True, exist_ok=True)
    stem = f"{username}_{started:%H.%M}"
    index = 1
    while True:
        name = stem if index == 1 else f"{stem}_{index}"
        base = folder / name
        lock = folder / f".{name}.lock"
        try:
            lock.touch(exist_ok=False)
        except FileExistsError:
            index += 1
            continue
        # A reserva exclusiva tambem protege gravadores em processos diferentes.
        if any(folder.glob(name + ".*")):
            lock.unlink()
            index += 1
            continue
        return base, lock


def closed_target(exc: Exception) -> bool:
    return isinstance(exc, PWError) and "has been closed" in exc.message


def launch_args(headful: bool) -> list[str]:
    args = [
        "--autoplay-policy=no-user-gesture-required",
        "--disable-blink-features=AutomationControlled",
        "--disable-features=CalculateNativeWinOcclusion",
        "--no-first-run",
        "--no-default-browser-check",
        "--log-level=3",
    ]
    if not headful:
        # Permite usar a GPU em vez do renderizador de software.
        args.extend(["--mute-audio", "--enable-gpu"])
    return args


def expendable_resource(request):
    return request.resource_type in ('image','font')


def open_context(pw, headful: bool, state: str | None = None,endpoint=None):
    # O shell recebe Ctrl+C direto no Windows; o Chromium completo permite fechar depois.
    browser = pw.chromium.connect_over_cdp(endpoint) if endpoint else pw.chromium.launch(
        channel="chromium",
        headless=not headful,
        handle_sigint=False,
        args=launch_args(headful),
    )
    ctx = browser.new_context(
        user_agent=UA,
        locale="pt-BR",
        timezone_id="America/Sao_Paulo",
        viewport={"width": 1280, "height": 720},
        ignore_https_errors=True,
        storage_state=state if state and Path(state).exists() else None,
    )
    ctx.add_init_script(INIT_JS)
    return browser, ctx


# ---------------------------------------------------------------------------
# descoberta
# ---------------------------------------------------------------------------
@dataclass
class Live:
    username: str
    livestream_id: str
    title: str
    viewers: int
    country: str
    channel_id: str = ""
    agora_id: str = ""
    token: str = ""
    recordable: bool = False
    url_override: str = ""
    user_id: str = ""
    profile_id: str = ""
    shared_id: str = ""

    @property
    def url(self) -> str:
        if self.url_override:
            return self.url_override
        if self.livestream_id:
            return f"{BASE}/pt/livestream/{self.livestream_id}"
        if self.profile_id or self.shared_id:
            return f"{BASE}/pt/profile/{self.profile_id or self.shared_id}"
        if self.username:
            return f"{BASE}/pt/{self.username}"
        return f"{BASE}/pt/livestream/{self.livestream_id}"


def identify_live_target(live: Live) -> Live:
    for value in (live.profile_id,live.shared_id):
        if value and not re.fullmatch(r'[1-9]\d{0,18}',value):raise ValueError('ID de perfil invalido')
    if live.url_override:
        parsed = urlsplit(live.url_override)
        hosts = {urlsplit(BASE).hostname, "www.superlivechat.tv"}
        if parsed.scheme not in {"http", "https"} or parsed.hostname not in hosts or parsed.username or parsed.password:
            raise ValueError("use uma URL de perfil ou transmissão do SuperLive")
    target_path = urlsplit(live.url).path
    target_profile=re.fullmatch(r'/(?:[a-z]{2}/)?profile/([1-9]\d{0,18})/?',target_path)
    if target_profile and not live.shared_id:live.profile_id=target_profile[1]
    target_id = re.search(r"/livestream/(\d+)(?:/|$)", target_path)
    if target_id and not live.livestream_id:
        live.livestream_id = target_id.group(1)
    target_user = re.fullmatch(r"/(?:[a-z]{2}/)?([^/]+)/?", target_path)
    if live.url_override and not live.username and not live.livestream_id and target_user:
        live.username = target_user.group(1)
    if not live.username and not live.livestream_id and not live.profile_id and not live.shared_id:
        raise ValueError("informe um username ou ID; a URL precisa identificar um perfil ou transmissão")
    if live.username and (not re.fullmatch(r"[\w.-]+", live.username) or live.username in {".", ".."}):
        raise ValueError("username inválido: use letras, números, ponto, hífen ou sublinhado")
    return live


def parse_record_target(target: str) -> Live:
    raw = target.strip()
    if raw.startswith("http"):
        live = Live("", "", "", 0, "", url_override=raw)
    elif target_is_id(raw):
        live = Live("", raw, "", 0, "")
    else:
        live = Live(raw.lstrip("@"), "", "", 0, "")
    return identify_live_target(live)


# ---------------------------------------------------------------------------
# gravacao
# ---------------------------------------------------------------------------
@dataclass
class Result:
    live: Live
    path: Path | None
    seconds: float
    size: int
    ok: bool
    error: str = ""
    paths: tuple[Path, ...] = ()


class StreamRecorder:
    """Grava um ID ou acompanha uma conta. Cada instancia usa seu Playwright."""

    def __init__(
        self,
        live: Live,
        outdir: Path,
        duration: float = 0.0,
        fmt: str = "mp4",
        ffmpeg: str | None = None,
        headful: bool = False,
        video_bitrate: int = 0,
        reconnect: int = 3,
        stall_timeout: float = 15.0,
        kill_after: float = 0.0,
        state: str | None = None,
        rotate: float = 0.0,
        stop_event: threading.Event | None = None,
        segment_callback: Callable | None = None,
        segment_bytes: int = 0,
        identity_callback: Callable | None = None,
        capture_allowed: Callable | None = None,
        part_limit: int = 0,
        low_resource_mode: bool = True,
    ) -> None:
        self.live = identify_live_target(live)
        self.stop_event = stop_event if stop_event is not None else STOP
        self.status = "starting"
        self.follow_username = live.username if not (live.livestream_id or live.profile_id or live.shared_id) else ""
        self.follow_profile_id=live.profile_id if not live.livestream_id else ''
        self.follow_shared_id=live.shared_id if not live.livestream_id else ''
        self.following=bool(self.follow_username or self.follow_profile_id or self.follow_shared_id)
        self.target_user_id = live.user_id or self.follow_profile_id
        self.monitor_key=''
        self.profile_user={}
        self.profile_callback=None
        self.outdir = outdir
        self.duration = duration
        self.fmt = fmt
        self.ffmpeg = ffmpeg or media_tool("ffmpeg")
        self.headful = headful
        self.video_bitrate = video_bitrate
        self.reconnect = reconnect
        self.stall_timeout = stall_timeout
        self.kill_after = kill_after
        self.state = state
        self.rotate = rotate
        self.tag = live.username or live.profile_id or live.shared_id or live.livestream_id or "live"
        self._session_base: Path | None = None
        self._session_lock: Path | None = None
        self.segments: list[Path] = []
        self.started = 0.0
        self.total_bytes = 0
        self.mime = ""
        self.source_mime = ""
        self.video_size: tuple[int, int] | None = None
        self.normalize_video = False
        self._live_ended = False
        self._live_confirmed = False
        self.outputs: list[Path] = []
        self._identity_error = ""
        self.segment_callback = segment_callback
        self.segment_bytes = segment_bytes
        self.identity_callback = identity_callback
        self.capture_allowed = capture_allowed
        self.part_limit = part_limit
        self.low_resource_mode=low_resource_mode and not headful
        self.browser_endpoint_provider=None
        from contextlib import nullcontext
        self.check_guard=nullcontext
        self.capture_acquire=lambda:True
        self.capture_release=lambda:None
        self.retry_delay=lambda delay:delay
        self.capture_failure=lambda:None
        self.capture_recovered=lambda:None
        self._profile_request=None
        self._viewer_request=None
        self.flush_event = threading.Event()
        self._progress_lock = threading.Lock()
        self._progress = dict(online=None, live_id='', part_id='', recording=False,
                              live_bytes=0, part_bytes=0, recorded_seconds=0.0, part_started=0.0)

    def progress(self):
        with self._progress_lock:
            value = dict(self._progress)
        value['part_seconds'] = 0.0
        if value['recording'] and value['part_bytes']:
            value['part_seconds'] = max(0,time.monotonic()-value['part_started'])
            value['recorded_seconds'] += value['part_seconds']
        return value

    def request_part(self):
        self.flush_event.set()

    def _online(self, online, live_id=''):
        with self._progress_lock:
            self._progress['online'] = online
            if live_id and live_id != self._progress['live_id']:
                self._progress.update(live_id=live_id,live_bytes=0,recorded_seconds=0.0,part_bytes=0,
                                      part_id='',recording=False,part_started=0.0)

    def _part_started(self, path):
        with self._progress_lock:
            self._progress.update(recording=True,part_id=path.parent.name,part_bytes=0,
                                  part_started=time.monotonic())

    def _part_finished(self):
        with self._progress_lock:
            if self._progress['recording']:
                self._progress['recorded_seconds'] += max(0,time.monotonic()-self._progress['part_started'])
            self._progress['recording'] = False

    def _write(self, fh, data):
        if self.part_limit and fh.tell()+len(data) > self.part_limit:
            raise RuntimeError('bloco RTC excede o limite seguro da parte; captura interrompida')
        if fh.tell()==0:self.capture_recovered()
        fh.write(data)
        self.total_bytes += len(data)
        with self._progress_lock:
            self._progress['part_bytes'] = fh.tell()
            self._progress['live_bytes'] += len(data)

    @property
    def stem(self) -> str:
        return sanitize(self.live.username or self.live.livestream_id or "live", "live")

    def _recording_base(self) -> Path:
        if self._session_base is None:
            self._session_base, self._session_lock = reserve_recording_base(
                self.outdir, self.stem, datetime.now())
        return self._session_base

    # -- helpers ---------------------------------------------------------
    def _elapsed(self) -> float:
        return (time.time() - self.started) if self.started else 0.0

    def _time_left(self) -> float:
        if self.duration <= 0:
            return 1e12
        return self.duration - self._elapsed()

    def _log(self, msg: str) -> None:
        log(msg, tag=self.tag)

    # -- main ------------------------------------------------------------
    def _profile_live(self, data: dict[str, Any]) -> Live | None:
        user = data.get("user")
        if not isinstance(user, dict) or not user.get("user_id"):
            raise ValueError("perfil nao encontrado ou sem identidade confirmada")
        uid = str(user["user_id"])
        username = str(user.get("username") or "")
        if self.follow_profile_id and uid!=self.follow_profile_id:
            raise ValueError('outra conta retornada para o ID de perfil solicitado')
        if self.follow_username and username.casefold() != self.follow_username.casefold():
            raise ValueError("perfil retornado nao corresponde ao username solicitado")
        if self.follow_shared_id and str(user.get('shared_id') or '')!=self.follow_shared_id:
            raise ValueError('perfil retornado nao corresponde ao ID compartilhado solicitado')
        if self.target_user_id and uid != self.target_user_id:
            raise ValueError("o username passou a apontar para outra conta; acompanhamento encerrado")
        if not self.target_user_id:
            if self.identity_callback:
                self.identity_callback(uid)
            self.target_user_id = uid
            self._log(f"conta fixada: username={username} user_id={uid}")
        self.profile_user=user
        if self.profile_callback:self.profile_callback(user)
        lid = str(user.get("livestream_id") or "")
        if not lid or lid == "0":
            self._online(False)
            return None
        self._online(True,lid)
        return Live(username, lid, "", 0, "", user_id=uid,
                    profile_id=uid if self.follow_profile_id or self.follow_shared_id else '')

    def _resolve_profile(self, ctx) -> Live | None:
        if self._profile_request:
            response=None;profile=None
            try:
                response=ctx.request.fetch(timeout=15000,**self._profile_request)
                value=response.json() if response.status==200 else None
                if isinstance(value,dict) and isinstance(value.get('user'),dict):profile=value
            except (PWError,ValueError):pass
            finally:
                if response:response.dispose()
            if profile is not None:return self._profile_live(profile)
            self._profile_request=None
        page = ctx.new_page()
        profile: dict[str, Any] | None = None
        failure = ""

        def read_profile(route) -> None:
            nonlocal profile, failure
            response = None
            try:
                # APIResponse retém o corpo até dispose; não depende do recurso da página após navegação.
                post_data=route.request.post_data
                if self.follow_shared_id:
                    payload=dict(route.request.post_data_json or {})
                    api=urlsplit(route.request.url)
                    search=ctx.request.post(f'{api.scheme}://{api.netloc}/api/web/users/search',
                        headers={k:v for k,v in route.request.all_headers().items() if k.lower() not in ('host','content-length','cookie')},
                        data={'client_params':payload.get('client_params',{}),'search_query':self.follow_shared_id},timeout=15000)
                    try:
                        data=search.json() if search.status==200 else {}
                        matches=[u for u in data.get('items',[]) if isinstance(u,dict)
                                 and str(u.get('shared_id') or '')==self.follow_shared_id and u.get('user_id')]
                        if len(matches)!=1:raise RuntimeError('ID compartilhado nao encontrado ou indisponivel')
                        payload['user_id']=str(matches[0]['user_id']);payload.pop('username',None)
                        post_data=json.dumps(payload)
                    finally:search.dispose()
                response = route.fetch(timeout=15000,post_data=post_data)
                if response.status == 200:
                    value = response.json()
                    if isinstance(value, dict):
                        profile = value
                        self._profile_request=dict(url_or_request=route.request.url,method=route.request.method,
                            headers={k:v for k,v in route.request.all_headers().items() if k.lower() not in ('host','content-length')},
                            data=post_data)
                else:
                    failure = f"consulta do perfil: HTTP {response.status}"
                route.fulfill(response=response)
            except Exception:
                failure = "resposta invalida ao consultar o perfil"
                try:route.abort()
                except PWError:pass
            finally:
                if response:
                    response.dispose()

        page.route("**/api/web/users/profile*", read_profile)
        if self.low_resource_mode:
            page.route('**/*',lambda route:route.abort() if expendable_resource(route.request) else route.fallback())
        # Consulta o perfil com o handshake do site, sem abrir players adicionais.
        page.route("**/api/web/livestream/**", lambda route: route.abort())
        try:
            profile_path=('profile/'+(self.follow_profile_id or self.follow_shared_id)) if self.follow_profile_id or self.follow_shared_id else self.follow_username
            page.goto(f"{BASE}/pt/{profile_path}", wait_until="domcontentloaded",
                      timeout=max(1, min(30_000, int(self._time_left() * 1000))))
            deadline = time.time() + min(20, self._time_left())
            while profile is None and not failure and time.time() < deadline and not self.stop_event.is_set():
                page.wait_for_timeout(200)
            if profile is None:
                if self.stop_event.is_set():
                    return None
                raise RuntimeError(failure or "timeout ao consultar o perfil")
            return self._profile_live(profile)
        finally:
            try:
                page.close()
            except PWError as exc:
                if not closed_target(exc):
                    raise

    def _finish_session(self) -> bool:
        try:
            if self.segments:
                self.status = "finalizing"
                # shortcut: normalizacao sincrona pode atrasar retomadas; usar fila se o atraso for significativo.
                final = self._finalize()
                if not final:
                    return False
                self.outputs.append(final)
                self._log(f"arquivo finalizado: {final} ({human_bytes(final.stat().st_size)})")
            self.segments = []
            self.mime = self.source_mime = ""
            self.video_size = None
            self.normalize_video = False
            self._session_base = None
            return True
        finally:
            if self._session_lock:
                self._session_lock.unlink(missing_ok=True)
                self._session_lock = None

    def run(self) -> Result:
        if self.stop_event.is_set():
            self.status = "stopped"
            return Result(self.live, None, 0, 0, True)
        attempts = 0
        segment_index = 0
        error = ""
        finalized = True
        if self.following:
            # No acompanhamento, o limite inclui a espera pelo perfil ficar online.
            self.started = time.time()
        try:
            with sync_playwright() as pw:
                endpoint=self.browser_endpoint_provider() if self.browser_endpoint_provider else None
                browser, ctx = open_context(pw, self.headful, self.state,endpoint=endpoint)
                try:
                    while True:
                        if self.stop_event.is_set() or self._time_left() <= 0:
                            break
                        if self.reconnect >= 0 and attempts > self.reconnect:
                            self._log(f"limite de {self.reconnect} reconexoes atingido")
                            break
                        if self.following:
                            self.status = "checking"
                            try:
                                with self.check_guard():candidate = self._resolve_profile(ctx)
                            except (PWError, RuntimeError) as exc:
                                self.capture_failure()
                                self.status = "reconnecting"
                                if self.stop_event.is_set() and closed_target(exc):
                                    break
                                attempts += 1
                                self._log(str(exc).splitlines()[0])
                                if self.reconnect >= 0 and attempts > self.reconnect:
                                    break
                                self.stop_event.wait(min(self.retry_delay(PROFILE_POLL_SECONDS), self._time_left()))
                                continue
                            if candidate is None or candidate.livestream_id != self.live.livestream_id:
                                finalized = self._finish_session()
                                if not finalized:
                                    error = "finalizacao falhou; segmentos originais preservados"
                                    break
                            if candidate is None:
                                self.status = "offline"
                                self._log("perfil offline; aguardando a mesma conta")
                                if self.reconnect == 0:
                                    break
                                self.stop_event.wait(min(PROFILE_POLL_SECONDS, self._time_left()))
                                continue
                            if candidate.livestream_id != self.live.livestream_id:
                                self.live = candidate
                                attempts = 0
                                self._log(f"transmissao atual: {candidate.livestream_id} user_id={self.target_user_id}")
                        if self.stop_event.is_set() or self._time_left() <= 0:
                            break
                        if self.capture_allowed and not self.capture_allowed(self.live.livestream_id):
                            self.status = 'watching'
                            self.stop_event.wait(PROFILE_POLL_SECONDS)
                            continue
                        if attempts:
                            self.status = "reconnecting"
                            self._log(
                                f"reconectando ({attempts}/{self.reconnect if self.reconnect >= 0 else 'inf'})..."
                            )
                            delay = min(5.0, max(1.0, 2.0 * attempts), self._time_left())
                            if self.stop_event.wait(max(0.0, delay)) or self._time_left() <= 0:
                                break
                        if not self.capture_acquire():
                            self.status='waiting_resources'
                            self.stop_event.wait(PROFILE_POLL_SECONDS)
                            continue
                        attempts += 1
                        segment_index += 1
                        try:seg_path, reason = self._record_segment(ctx, segment_index)
                        finally:self.capture_release()
                        if reason in ("ended", "stalled", "error", "timeout"):
                            self.status = "reconnecting"
                            if reason in ('stalled','error','timeout'):self.capture_failure()
                        if seg_path and seg_path.stat().st_size > 2048:
                            self.segments.append(seg_path)
                        else:
                            if seg_path:
                                seg_path.unlink(missing_ok=True)
                        if self.stop_event.is_set() or self._time_left() <= 0:
                            break
                        if self._identity_error:
                            error = self._identity_error
                            break
                        if reason == "rotate":
                            # sessao nova (token novo, memoria liberada) sem
                            # gastar o orcamento de reconexao
                            self._log(f"rotacionando sessao ({human_time(self.rotate)})")
                            attempts -= 1
                            continue
                        if self.following and reason in ("finished", "novideo", "ended", "stalled", "error", "timeout"):
                            if reason == "finished":
                                finalized = self._finish_session()
                                if not finalized:
                                    error = "finalizacao falhou; segmentos originais preservados"
                                    break
                                attempts = 0
                                self.status = "offline"
                            if self.reconnect == 0:
                                break
                            self.stop_event.wait(min(self.retry_delay(PROFILE_POLL_SECONDS), self._time_left()))
                            continue
                        # falhas definitivas encerram; "ended"/"stalled"/"error"
                        # caem no laco de reconexao.
                        if reason in ("novideo", "norec", "stopped", "finished"):
                            break
                finally:
                    try:
                        if finalized:
                            finalized = self._finish_session()
                            if not finalized:
                                error = "finalizacao falhou; segmentos originais preservados"
                    finally:
                        for target in (ctx, browser):
                            try:
                                target.close()
                            except PWError as exc:
                                if not closed_target(exc):
                                    error = error or f"{type(exc).__name__}: {str(exc).splitlines()[0]}"
        except Exception as exc:  # noqa: BLE001
            if not (self.stop_event.is_set() and (closed_target(exc) or isinstance(exc, InterruptedError))):
                error = f"{type(exc).__name__}: {str(exc).splitlines()[0]}"
                self._log(f"erro: {error}")

        paths = tuple(self.outputs)
        self.status = "error" if error or (not paths and not self.stop_event.is_set()) else "stopped"
        return Result(self.live, paths[-1] if paths else None, self._elapsed(),
                      sum(p.stat().st_size for p in paths), (bool(paths) or self.stop_event.is_set()) and not error,
                      error or ("" if paths or self.stop_event.is_set() else "nada gravado"), paths)

    def _accept_live(self, data: dict[str, Any]) -> bool:
        details = data.get("stream_details")
        details = details if isinstance(details, dict) else {}
        user = data.get("user")
        user = user if isinstance(user, dict) else {}
        lid = str(details.get("livestream_id") or "")
        username = str(user.get("username") or "")
        uid = str(user.get("user_id") or "")
        channel = str(details.get("channel_id") or "")
        reason = ""
        if not lid:
            reason = "API nao confirmou a identidade da live; captura cancelada"
        elif self.live.livestream_id and lid != self.live.livestream_id:
            reason = f"bloqueada outra live: {lid} (alvo: {self.live.livestream_id})"
        elif self.live.username and not (self.follow_profile_id or self.follow_shared_id) and username.casefold() != self.live.username.casefold():
            reason = "usuario retornado pela API nao corresponde ao alvo"
        elif not uid:
            reason = "API nao confirmou o user_id da conta"
        elif self.target_user_id and uid != self.target_user_id:
            reason = "outra conta retornada pela API; captura bloqueada"
            self._identity_error = reason
        elif details.get("finished_at") or data.get("disallowed_reason") == "finished":
            reason = f"live {lid} encerrada; nenhuma outra sala sera gravada"
        elif data.get("disallowed_reason"):
            reason = f"live {lid} indisponivel: {data['disallowed_reason']}"
        elif not channel:
            reason = "API nao confirmou o canal da transmissao"
        elif self._live_confirmed and self.live.channel_id and channel != self.live.channel_id:
            reason = "canal da transmissao mudou; captura interrompida"
        if reason:
            if not self._live_ended:
                self._log(reason)
            self._live_ended = True
            self._live_confirmed = False
            return False
        if not self.live.livestream_id:
            self.live.livestream_id = lid
        self.target_user_id = self.target_user_id or uid
        self.live.user_id = uid
        self.live.username = username or self.live.username
        self.live.channel_id = channel
        self.profile_user=user
        if self.profile_callback:self.profile_callback(user)
        title=details.get('title') or details.get('headline')
        if isinstance(title,str):self.live.title=title
        self._online(True,lid)
        return True

    def _record_segment(self, ctx, index: int) -> tuple[Path | None, str]:
        self._live_ended = self._live_confirmed = False
        if self.low_resource_mode and self._viewer_request:
            from superlive_viewer import cached_live
            if cached_live(self,ctx) is False:return None,'finished'
        page = ctx.new_page()
        viewer = None
        bound_live = self.live
        seg: Path | None = None
        fh = None
        reason = "ended"
        seg_t0 = 0.0
        piece = 0

        def publish():
            nonlocal seg, fh
            if fh:
                self.status='finalizing'
                # O primeiro bloco pode sair somente no stop; contabilize-o antes de fechar o contador.
                if self.progress()['part_bytes']:self._part_finished()
                try:
                    media_eval(page,'window.__slRec.stop()')
                    blob=media_eval(page,'window.__slRec.drain()')
                    if blob:
                        self._write(fh,base64.b64decode(blob))
                    self.mime=media_eval(page,'window.__slRec.info().mimeType') or self.mime
                finally:
                    self._part_finished()
                    fh.close()
                    fh=None
            if seg and self.segment_callback:
                path, seg = seg, None
                self.segment_callback(path, {
                    "username": self.monitor_key or self.follow_username or self.live.username or f'profile/{self.target_user_id}',
                    "live_id": self.live.livestream_id, "started": seg_t0,
                    "ended":time.time(),"title":self.live.title,"profile_id":self.target_user_id,
                    "mime": self.mime, "video_size": self.video_size,
                    "normalize": self.normalize_video,
                    "seconds": max(0,time.time()-seg_t0),
                })

        def guard_live(route) -> None:
            if page.is_closed() or self.live is not bound_live:
                return
            # Bloqueia outra sala antes de buscar sua resposta, inclusive preloads.
            try:
                requested = str((route.request.post_data_json or {}).get("livestream_id") or "")
            except Exception:
                requested = ""
            if self.live.livestream_id and requested != self.live.livestream_id:
                route.abort()
                return
            try:
                response = route.fetch()
            except PWError:
                route.abort()
                return
            if page.is_closed() or self.live is not bound_live:
                if not page.is_closed():
                    route.abort()
                return
            try:
                data = response.json() if response.status == 200 else {}
            except Exception:
                data = {}
            if self._accept_live(data if isinstance(data, dict) else {}):
                if not self._live_confirmed:
                    # So aceita tracks criados depois da confirmacao desta sala.
                    page.evaluate("o => {window.__slTargetChannel=o.channelId;window.__slRec.arm(o);}", {
                        "livestreamId": self.live.livestream_id, "channelId": self.live.channel_id,
                    })
                    self._live_confirmed = True
                route.fulfill(response=response)
            else:
                # A proxima sala nao chega ao player nem reutiliza os tracks atuais.
                route.abort()

        page.route("**/api/web/livestream/retrieve*", guard_live)
        def guard_navigation(route):
            request=route.request
            if self.low_resource_mode and expendable_resource(request):
                route.abort()
                return
            if (self._live_confirmed and request.is_navigation_request() and request.frame==page.main_frame
                    and (urlsplit(request.url).netloc!=urlsplit(bound_live.url).netloc
                         or urlsplit(request.url).scheme!=urlsplit(bound_live.url).scheme
                         or urlsplit(request.url).path.rstrip('/')!=urlsplit(bound_live.url).path.rstrip('/'))):
                self._live_ended=True
                route.abort()
                self._log('navegação automática bloqueada; finalizando o alvo original')
            else:
                route.fallback()
        page.route('**/*',guard_navigation)
        try:
            if self.low_resource_mode and not self.live.url_override and urlsplit(self.live.url).hostname=='superlivechat.tv':
                from superlive_viewer import open_viewer,isolate_channel,ViewerUnavailable
                try:
                    viewer=open_viewer(self,ctx,page)
                    page=viewer.page
                    self._live_confirmed=True
                    self._log('viewer leve: um canal RTC, qualidade original, sem players recomendados')
                except Exception as exc:
                    if self._live_ended:return None,'finished'
                    if self.stop_event.is_set():return None,'stopped'
                    reason=str(exc) if isinstance(exc,ViewerUnavailable) else type(exc).__name__
                    isolated=isinstance(exc,ViewerUnavailable) and reason in ('rtc_join','enter','tracks','verify')
                    mode='um canal RTC' if isolated else 'canal RTC ainda nao correlacionado'
                    self._log(f'viewer leve indisponivel ({reason}); usando player original ({mode})')
                    page=ctx.new_page()
                    if isolated:isolate_channel(page,self,False)
                    self._live_ended=self._live_confirmed=False
                    page.route('**/api/web/livestream/retrieve*',guard_live)
                    page.route('**/*',guard_navigation)
            try:
                if not viewer:page.goto(self.live.url, wait_until="domcontentloaded", timeout=60_000)
            except PWTimeout:
                self._log("timeout ao abrir a pagina")
                return None, "novideo"
            except PWError as exc:
                if "ERR_TOO_MANY_REDIRECTS" in str(exc):
                    self._log("usuario nao encontrado (redirecionamento em loop)")
                else:
                    self._log(f"falha ao abrir a pagina: {str(exc).splitlines()[0]}")
                return None, "novideo"

            self._log(f"aberto {page.url}")

            if not self._wait_video(page, 45.0):
                if not self._live_ended:
                    self._log("sem video confirmado para o alvo (offline, privada ou geobloqueada?)")
                return None, "finished" if self._live_ended else "novideo"

            original_path = urlsplit(page.url).path.rstrip("/")
            allowed_paths = {urlsplit(self.live.url).path.rstrip("/"),
                             f"/pt/livestream/{self.live.livestream_id}"}
            if viewer:allowed_paths.add('/__streamsave_viewer')
            if original_path not in allowed_paths:
                self._log("redirecionamento para outra pagina; captura cancelada")
                return None, "finished"
            base = self._recording_base()
            seg = base.with_name(f"{base.name}.part{index:02d}.mkv")
            fh = seg.open("xb")

            info = media_eval(page,
                'window.__slRec.start(arg)',
                {"mimeTypes": MIME_TYPES, "videoBitrate": self.video_bitrate,'lowResource':self.low_resource_mode},
            )
            if not info.get("ok"):
                self._log(f"falha ao iniciar MediaRecorder: {info.get('err')}")
                return None, "norec"

            self.mime = info.get("mimeType") or ""
            self.status = "recording"
            initial = media_eval(page,'window.__slRec.info()')
            if not self.video_size and initial.get("width") and initial.get("height"):
                self.video_size = (initial["width"], initial["height"])
            if not self.started:
                self.started = time.time()
            self._log(
                f"gravando {info.get('mimeType') or 'codec original (automatico)'} "
                f"audio={'sim' if info.get('hasAudio') else 'nao'}"
            )
            self._log(f"identidade: user_id={self.target_user_id} live={self.live.livestream_id} "
                      f"canal={self.live.channel_id} video={initial.get('videoTrackId')} "
                      f"audio={initial.get('audioTrackId')}")
            if self.kill_after:
                page.evaluate(
                    """(ms) => setTimeout(() => {
                        if (window.__slRec.video) window.__slRec.video.stop();
                    }, ms)""",
                    int(self.kill_after * 1000),
                )

            last_data = last_video = time.time()
            previous_frames = None
            last_report = 0.0
            seg_t0 = time.time()
            self._part_started(seg)
            while True:
                if self.stop_event.is_set() or self._time_left() <= 0:
                    reason = "stopped"
                    break
                if viewer:viewer.refresh()
                if self._live_ended or urlsplit(page.url).path.rstrip("/") != original_path:
                    self._log("live original terminou ou o site tentou trocar de sala")
                    reason = "finished"
                    break
                if ((self.rotate and time.time()-seg_t0 >= self.rotate)
                        or (self.segment_bytes and fh.tell() >= self.segment_bytes) or self.flush_event.is_set()):
                    if not self.segment_callback:
                        reason = "rotate"
                        break
                    self.flush_event.clear()
                    publish()
                    if self.stop_event.is_set() or (self.capture_allowed and not self.capture_allowed(self.live.livestream_id)):
                        reason = 'watching'
                        break
                    piece += 1
                    base = self._recording_base()
                    seg = base.with_name(f"{base.name}.chunk{piece:04d}.mkv")
                    fh = seg.open("xb")
                    self.normalize_video = False
                    self.source_mime = ""
                    resumed = media_eval(page,'window.__slRec.start(arg)',
                        {"mimeTypes": MIME_TYPES, "videoBitrate": self.video_bitrate,'lowResource':self.low_resource_mode})
                    if not resumed.get("ok"):
                        reason = "error"
                        break
                    seg_t0 = time.time()
                    last_data = last_video = seg_t0
                    previous_frames = None
                    self.status = "recording"
                    self._part_started(seg)
                try:
                    packet = media_eval(page,"""({
                        blob: await window.__slRec.drain(),
                        info: window.__slRec.info()
                    })""")
                    blob, st = packet['blob'],packet['info']
                except PWTimeout:
                    raise
                except PWError:
                    reason = "ended"
                    break
                if blob:
                    data = base64.b64decode(blob)
                    self._write(fh,data)
                    last_data = time.time()
                video=media_eval(page,'window.__slRec.videoStats()')
                self.mime = st.get("mimeType") or self.mime
                if self.mime:
                    if self.source_mime and self.source_mime != self.mime:
                        self.normalize_video = True
                    self.source_mime = self.source_mime or self.mime
                size = (video.get("width"), video.get("height"))
                if all(size):
                    if self.video_size and size != self.video_size:
                        self.normalize_video = True
                    self.video_size = self.video_size or size
                frames = video.get("frames")
                if frames is not None and frames != previous_frames:
                    previous_frames, last_video = frames, time.time()
                if self._live_ended:
                    reason = "finished"
                    break
                if st.get("state") != "recording":
                    self._log(st.get("reason") or "track encerrou")
                    reason = "finished" if not st.get("identityValid", True) else "ended"
                    break
                if not st.get("identityValid", False):
                    self._log("player ou canal deixou de corresponder ao alvo")
                    reason = "finished"
                    break
                if (time.time() - last_data > self.stall_timeout
                        or (frames is not None and time.time() - last_video > self.stall_timeout)):
                    self._log("stream travou (sem progresso de video ou sem dados)")
                    reason = "stalled"
                    break
                # O MediaRecorder entrega um bloco por segundo.
                self.stop_event.wait(1.0)
                now = time.time()
                if now - last_report >= 10:
                    last_report = now
                    progress=self.progress()
                    self._log(f"{human_time(progress['part_seconds'])} gravados, {human_bytes(progress['part_bytes'])}")
        except PWTimeout:
            reason = "timeout"
            self._log('prazo de resposta do navegador esgotado; finalizando o prefixo capturado')
        except PWError as exc:
            reason = "error"
            self._log(f'viewer leve: operacao indisponivel ({type(exc).__name__})' if viewer
                      else f"playwright: {str(exc).splitlines()[0]}")
        except RuntimeError:
            if not viewer:raise
            reason='error'
            self._log('sessao do viewer leve indisponivel; preservando a parte antes de reconectar')
        finally:
            try:
                publish()
            except Exception as exc:
                self._log(f'ultimo bloco indisponivel; prefixo preservado ({type(exc).__name__})')
                if self.segment_callback and seg:
                    if fh:
                        fh.close()
                        fh = None
                    self.segment_callback(seg, {
                        "username": self.monitor_key or self.follow_username or self.live.username or f'profile/{self.target_user_id}',
                        "live_id": self.live.livestream_id, "started": seg_t0,
                        "ended":time.time(),"title":self.live.title,"profile_id":self.target_user_id,
                        "mime": self.mime, "video_size": self.video_size,
                        "normalize": self.normalize_video,
                        "seconds": max(0,time.time()-seg_t0),
                    })
                    seg = None
            if fh:
                try:
                    fh.close()
                except Exception:
                    pass
            self._part_finished()
            try:
                if viewer:viewer.close()
                else:page.close()
            except Exception:
                pass

        return seg, reason

    def _wait_video(self, page, timeout: float) -> bool:
        remaining = self._time_left() if self.started else timeout
        deadline = time.time() + min(timeout, max(0.0, remaining))
        while time.time() < deadline:
            if self.stop_event.is_set() or self._live_ended:
                return False
            try:
                if self._live_confirmed and page.evaluate(
                    "() => !!(window.__slRec && window.__slRec.info().hasVideo)"
                ):
                    return True
            except PWError:
                pass
            page.wait_for_timeout(400)
        return False

    # -- pos-processamento ----------------------------------------------
    def _is_h264(self) -> bool:
        mime = (self.mime or "").lower()
        return "h264" in mime or "avc" in mime

    def _finalize(self) -> Path | None:
        recording = self._recording_base()
        base, folder = recording.name, recording.parent
        target = self.fmt
        if target == "webm" and (self._is_h264() or self.normalize_video):
            # h264 nao e valido em WebM estrito (so VP8/VP9/AV1); Matroska aceita.
            target = "mkv"
            self._log("codec h264: usando container .mkv em vez de .webm")
        final = folder / f"{base}.{target}"

        inputs = self.segments
        normalized: list[Path] = []
        if self.normalize_video:
            if not self.video_size:
                self._log("resolucao desconhecida; mantendo segmentos brutos")
                return None
            width, height = (max(2, n // 2 * 2) for n in self.video_size)
            self._log(f"normalizando resolucao/codec em {width}x{height} na finalizacao")
            vf = (f"scale={width}:{height}:force_original_aspect_ratio=decrease:"
                  f"force_divisible_by=2,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1")
            for part in self.segments:
                output = part.with_name(part.stem + ".normalized.mkv")
                if not self._ffmpeg([
                    "-fflags", "+genpts", "-i", str(part), "-vf", vf,
                    "-c:v", "libx264", "-threads", "2", "-preset", "veryfast", "-crf", "20", "-fps_mode", "vfr",
                    "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2", str(output),
                ]):
                    self._log("normalizacao falhou; mantendo todos os segmentos")
                    return None
                normalized.append(output)
            inputs = normalized
            self.mime = "video/x-matroska;codecs=avc1,aac"

        if len(inputs) > 1:
            # MediaRecorder nao fecha Duration/cues; cada trecho precisa de duracao real.
            # MP4 permite ao concat converter os cabecalhos H.264 de cada reconexao.
            prepared: list[Path] = []
            for part in inputs:
                ready = part.with_name(part.stem + (".ready.mp4" if self._is_h264() else ".ready.mkv"))
                args = ["-fflags", "+genpts", "-i", str(part), "-map", "0:v:0",
                        "-map", "0:a:0?", "-c", "copy", "-strict", "-2"]
                if self._is_h264():
                    args += ["-video_track_timescale", "90000"]
                if not self._ffmpeg(args + [str(ready)]):
                    self._log("preparacao dos segmentos falhou; mantendo todos os originais")
                    return None
                prepared.append(ready)
            inputs = prepared
            merged = folder / f"{base}.merged.mkv"
            listing = folder / f".{base}.txt"
            listing.write_text(
                "".join(f"file '{p.resolve().as_posix()}'\n" for p in inputs), encoding="utf-8"
            )
            ok = self._ffmpeg(
                ["-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", "-f", "matroska", str(merged)]
            )
            listing.unlink(missing_ok=True)
            if not ok or not merged.exists():
                self._log("concat falhou; mantendo todos os segmentos")
                return None
            source = merged
        else:
            prepared = []
            source = inputs[0]

        if target == "webm" and not self._is_h264():
            if source != final:
                shutil.move(str(source), str(final))
        else:
            args = ["-fflags", "+genpts", "-i", str(source)]
            if target == "mkv":
                args += ["-c", "copy"]
            else:
                # MP4 nao aceita VP8; converte so na finalizacao, fora do navegador.
                video_args = (["-c:v", "libx264", "-preset", "veryfast", "-crf", "20"]
                              if "vp8" in self.mime.lower() else ["-c:v", "copy"])
                args += [
                    *video_args,
                    "-c:a", "aac", "-b:a", "160k",
                    "-video_track_timescale", "90000",
                    "-movflags", "+faststart",
                ]
            ok = self._ffmpeg(args + [str(final)])
            if not ok:
                if len(self.segments) > 1 or normalized:
                    self._log("ffmpeg falhou; mantendo todos os segmentos")
                    return None
                self._log("ffmpeg falhou; mantendo o segmento bruto")
                fallback = folder / f"{base}.raw.mkv"
                shutil.move(str(source), str(fallback))
                final = fallback

        for p in [*self.segments, *normalized, *prepared]:
            if p != final and p.exists():
                p.unlink(missing_ok=True)
        merged_path = folder / f"{base}.merged.mkv"
        if merged_path.exists() and merged_path != final:
            merged_path.unlink(missing_ok=True)
        return final if final.exists() else None

    def _ffmpeg(self, args: list[str]) -> bool:
        if not self.ffmpeg:
            return False
        cmd = [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", *args]
        # arquivos longos precisam de mais tempo de remux/concat (copy e rapido,
        # mas 3h de material nao cabe em 30 min de timeout)
        total_mb = sum(p.stat().st_size for p in self.segments if p.exists()) / 1e6
        timeout = min(6 * 3600, max(1800, int(total_mb * 4)))
        try:
            # Ctrl+C pertence ao Python; o FFmpeg precisa terminar de fechar o arquivo.
            process_group = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt"
                             else {"start_new_session": True})
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **process_group)
        except Exception as exc:  # noqa: BLE001
            self._log(f"ffmpeg: {exc}")
            return False
        if proc.returncode != 0:
            self._log(f"ffmpeg rc={proc.returncode} {proc.stderr.strip()[:300]}")
            return False
        return True


def target_is_id(target: str) -> bool:
    return bool(re.fullmatch(r"\d{6,}", target.strip()))
