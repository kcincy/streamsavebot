"""Leitura de identidade e fMP4 reaproveitadas do gravador StripChat fornecido."""
import base64
import hashlib
import json
import re
import tempfile
import time
import urllib.request
import urllib.error
from pathlib import Path
from functools import lru_cache
from urllib.parse import urljoin,urlsplit,urlunsplit,parse_qsl,urlencode,unquote
BASE = "https://pt.stripchat.com"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36", "Accept": "text/html,application/json", "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8"}
HEADERS['Cache-Control']='no-cache'
FLUSH_DELAY = 2.0
class IdentityError(ValueError):
    pass


class ProfileUnavailable(RuntimeError):
    def __init__(self,status='unavailable'):
        self.status=status
        super().__init__(status)


class DirectUnavailable(RuntimeError):
    """Captura HTTP indisponível; os dados já capturados são preservados."""

def http_get(url: str, timeout: float = 30) -> str:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        enc = r.headers.get_content_charset() or "utf-8"
        return raw.decode(enc, "replace")

def parse_model_info(html: str, username: str) -> dict:
    match = re.search(r'"viewCamBase"\s*:\s*', html)
    if not match:
        raise ProfileUnavailable()
    try:
        view, _ = json.JSONDecoder().raw_decode(html[match.end():])
        error=view.get('error') or {}
        if error:
            code=str(error.get('type','') if isinstance(error,dict) else '').casefold()
            raise ProfileUnavailable('blocked' if code in ('geobanned','geoblocked','banned','blocked') else 'unavailable')
        model = view["model"]
        model_id = int(model["id"])
        actual_username = model["username"]
    except (ValueError, KeyError, TypeError) as exc:
        raise ProfileUnavailable() from exc
    if not isinstance(actual_username, str) or actual_username.casefold() != username.casefold():
        raise ProfileUnavailable()
    if model_id <= 0:
        raise RuntimeError("ID do perfil invalido")
    status=str(model.get('status') or 'unknown').casefold()
    live=model.get('isLive',view.get('isCamAvailable',False)) is True and status not in ('off','offline')
    access=('offline' if not live else 'public' if status=='public' else
            'private' if status in ('private','p2p','groupshow','group','ticketshow','ticket','spy','exclusive','trueprivate') else 'restricted')
    if model.get('isBlocked') or model.get('isDeleted'):
        access='blocked' if model.get('isBlocked') else 'unavailable'
    cam=view.get('cam') or {}
    title=model.get('topic') or (cam.get('topic') if isinstance(cam,dict) else None) or view.get('topic')
    return {"username": actual_username, "id": model_id,
            "title":title if isinstance(title,str) else None,
            "stream_name": str(model.get("streamName") or model_id),
            "live": live,"status": access,"recordable":access=='public',
            "hls_urls":hls_urls(html,model_id)}


def hls_urls(html,model_id):
    urls=[]
    for name,section in (('configV3','static'),('config','data')):
        match=re.search(r'"'+name+r'"\s*:\s*',html)
        if not match:continue
        try:
            config=json.JSONDecoder().raw_decode(html[match.end():])[0][section]
            urls.extend(stream_urls(config,model_id))
        except (ValueError,KeyError,TypeError,AttributeError):continue
    return list(dict.fromkeys(urls))[:8]


def stream_urls(config,model_id):
    domains=[config.get('hlsStreamHost')]
    for key in ('features','featuresV2'):
        domains.extend(config.get(key,{}).get('hlsFallback',{}).get('fallbackDomains',[]))
    return list(dict.fromkeys(f'https://edge-hls.{domain}/hls/{model_id}/master/{model_id}_auto.m3u8'
        for domain in domains if isinstance(domain,str)
        and re.fullmatch(r'[A-Za-z0-9]+(?:[.-][A-Za-z0-9]+)*\.[A-Za-z]{2,}',domain)))[:8]


@lru_cache(maxsize=1)
def mouflon_keys():
    try:
        data=json.loads(Path(__file__).with_name('stripchat_mouflon_keys.json').read_text(encoding='utf8'))
        return {name:hashlib.sha256(value.encode()).digest() for name,value in data.items()
                if isinstance(name,str) and isinstance(value,str) and value}
    except (OSError,ValueError,AttributeError):raise DirectUnavailable('mouflon_keys_unavailable') from None


def decode_mouflon(text,url):
    text='\n'.join(line for line in text.splitlines() if not line.startswith('#EXT-X-MOUFLON-ADVERT'))
    if '#EXT-X-MOUFLON' not in text:return text,None
    keys=mouflon_keys()
    options=re.findall(r'^#EXT-X-MOUFLON:PSCH:(v[12]):([^:\r\n]+)',text,re.M)
    query=dict(parse_qsl(urlsplit(url).query))
    options.append((query.get('psch'),query.get('pkey')))
    selected=next(((version,name) for version,name in options if version in ('v1','v2') and name in keys),None)
    if not selected:raise DirectUnavailable('mouflon_key_missing')
    version,name=selected;key=keys[name];pending=None;lines=[]
    def decode(value):
        value=unquote(value)
        raw=base64.b64decode(value+'='*(-len(value)%4),validate=True)
        return bytes(value^key[index%len(key)] for index,value in enumerate(raw)).decode('utf8')
    try:
        for line in text.splitlines():
            if line.startswith(('#EXT-X-MOUFLON:PSCH:','#EXT-X-MOUFLON-ADVERT')):continue
            if line.startswith(('#EXT-X-MOUFLON:URI:','#EXT-X-MOUFLON:FILE:')):
                if pending is not None:raise ValueError()
                value=line.split(':',2)[2]
                if version=='v2' and line.startswith('#EXT-X-MOUFLON:URI:'):
                    prefix,encrypted,suffix=value.rsplit('_',2)
                    pending=prefix+'_'+decode(encrypted[::-1])+'_'+suffix
                elif version=='v1' and line.startswith('#EXT-X-MOUFLON:FILE:'):pending=decode(value)
                else:raise ValueError()
                continue
            if line.startswith('#EXT-X-MOUFLON'):raise ValueError()
            if pending is not None and line and not line.startswith('#'):
                if urlsplit(line).path.rsplit('/',1)[-1]!='media.mp4':raise ValueError()
                line=pending if urlsplit(pending).scheme else line.replace('media.mp4',pending,1)
                pending=None
            lines.append(line)
        if pending is not None:raise ValueError()
    except (ValueError,UnicodeError):raise DirectUnavailable('invalid_mouflon') from None
    return '\n'.join(lines),selected


def hls_playlist(text,url):
    """Lê segmentos completos e resolve os nomes MOUFLON, sem decodificar vídeo."""
    if not text.lstrip().startswith('#EXTM3U'):
        raise DirectUnavailable('invalid_playlist')
    text,selected=decode_mouflon(text,url)
    if any(tag in text for tag in ('#EXT-X-BYTERANGE','#EXT-X-GAP',
                                    '#EXT-X-ENDLIST','#EXT-X-MEDIA:','#EXT-X-SKIP')):
        raise DirectUnavailable('unsupported_playlist')
    for line in text.splitlines():
        if line.startswith(('#EXT-X-KEY:','#EXT-X-SESSION-KEY:')) and not re.search(r'(?:[:,])METHOD=NONE(?:,|$)',line):
            raise DirectUnavailable('encrypted_playlist')
    if '#EXT-X-STREAM-INF:' in text:
        variant=urlsplit(urljoin(url,_pick_variant(text,'source').splitlines()[-1].strip()))
        query=dict(parse_qsl(variant.query))
        query.update(playlistType='standard',preferredVideoCodec='h264')
        if selected:query.update(psch=selected[0],pkey=selected[1])
        return urlunsplit(variant._replace(query=urlencode(query))),[],1
    sequence=discontinuity=0;init=None;duration=None;segments=[];delay=1
    try:
        for line in map(str.strip,text.splitlines()):
            if line.startswith('#EXT-X-MEDIA-SEQUENCE:'):sequence=int(line.split(':',1)[1])
            elif line.startswith('#EXT-X-DISCONTINUITY-SEQUENCE:'):discontinuity=int(line.split(':',1)[1])
            elif line=='#EXT-X-DISCONTINUITY':discontinuity+=1
            elif line.startswith('#EXT-X-TARGETDURATION:'):delay=max(.5,min(2,float(line.split(':',1)[1])/2))
            elif line.startswith('#EXT-X-MAP:'):
                if 'BYTERANGE=' in line:raise DirectUnavailable('unsupported_playlist')
                init=urljoin(url,re.search(r'URI="([^"]+)"',line)[1])
            elif line.startswith('#EXTINF:'):duration=float(line.split(':',1)[1].split(',')[0])
            elif line and not line.startswith('#'):
                if duration is None or duration<=0 or sequence<0:raise ValueError()
                segments.append((sequence,discontinuity,init,urljoin(url,line)))
                sequence+=1;duration=None
    except (ValueError,TypeError):raise DirectUnavailable('invalid_playlist') from None
    if not segments:raise DirectUnavailable('empty_playlist')
    return None,segments,delay

def model_info(username: str, timeout: float = 30) -> dict:
    """Le somente o modelo da pagina solicitada, sem usar recomendacoes como fallback."""
    try:
        return parse_model_info(http_get(f"{BASE}/{username}", timeout=timeout), username)
    except urllib.error.HTTPError as exc:
        if exc.code in (403,404,410,451):
            raise ProfileUnavailable('blocked' if exc.code in (403,451) else 'unavailable') from None
        raise

class SegmentWriter:
    """Grava init + segmentos fMP4 em ordem, tolerando pequenas reordenacoes."""

    def __init__(self, path: Path, limit=500_000_000, write=None):
        self.path = path
        self.fh = open(path, "xb")
        self.limit=limit
        self.write=write or self.fh.write
        self.buf: dict[tuple[int, int], tuple[float, bytes]] = {}
        self.written: dict[tuple[int, int], tuple[int, int]] = {}
        self.last_key: tuple[int, int] = (-1, -1)
        self.needs_reorder = False
        self.init_size = 0
        self.bytes_written = 0
        self.init_written = False

    def write_init(self, data: bytes) -> None:
        if self.init_written:
            return
        if self.bytes_written+len(data)>self.limit:
            raise RuntimeError('init_exceeds_part_limit')
        self.write(data)
        self.fh.flush()
        self.init_written = True
        self.init_size = len(data)
        self.bytes_written += len(data)

    def add(self, key: tuple[int, int], data: bytes) -> None:
        if key in self.written or key in self.buf:
            return
        pending=sum(len(item[1]) for item in self.buf.values())
        if pending+len(data)>16*1024*1024:
            raise RuntimeError('segment_reorder_buffer_full')
        if self.bytes_written+pending+len(data)>self.limit:
            raise RuntimeError('segment_exceeds_part_limit')
        self.buf[key] = (time.monotonic(), data)
        self.flush()

    def flush(self, force: bool = False) -> None:
        if not self.init_written and not force:
            return
        now = time.monotonic()
        while self.buf:
            key = min(self.buf)
            ts, data = self.buf[key]
            if not force and now - ts < FLUSH_DELAY:
                break
            del self.buf[key]
            if key in self.written:
                continue
            offset = self.fh.tell()
            self.write(data)
            self.bytes_written += len(data)
            self.written[key] = (offset, len(data))
            self.needs_reorder |= key < self.last_key or key[1] == -1
            self.last_key = max(self.last_key, key)
        self.fh.flush()

    @property
    def segments(self) -> int:
        return len(self.written)

    def close(self) -> None:
        if not self.fh.closed:
            try:
                self.flush(force=True)
            finally:
                self.fh.close()
        if not self.needs_reorder:
            return

        # O indice em disco permite ordenar atrasos de qualquer duracao sem perder partes.
        ordered_path = None
        try:
            with self.path.open("rb") as source, tempfile.NamedTemporaryFile(
                dir=self.path.parent, prefix=self.path.stem + ".", suffix=".sorting",
                delete=False,
            ) as ordered:
                ordered_path = Path(ordered.name)
                ordered.write(source.read(self.init_size))
                for key in sorted(self.written):
                    if key[1] != -1 and (key[0], -1) in self.written:
                        continue
                    offset, size = self.written[key]
                    source.seek(offset)
                    data = source.read(size)
                    if len(data) != size:
                        raise OSError("arquivo bruto truncado durante a ordenacao")
                    ordered.write(data)
            ordered_path.replace(self.path)
            self.needs_reorder = False
            self.bytes_written = self.path.stat().st_size
        finally:
            if ordered_path is not None:
                ordered_path.unlink(missing_ok=True)

def _pick_variant(playlist: str, quality: str) -> str:
    lines = playlist.splitlines()
    blocks: list[tuple[dict, list[str]]] = []
    header: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("#EXT-X-STREAM-INF"):
            attrs = dict(re.findall(r'([A-Z0-9\-]+)=("[^"]*"|[^,]*)', line))
            attrs = {k: v.strip('"') for k, v in attrs.items()}
            block = [line]
            i += 1
            while i < len(lines) and (not lines[i].strip() or lines[i].startswith("#")):
                block.append(lines[i])
                i += 1
            if i < len(lines):
                block.append(lines[i])
                i += 1
            blocks.append((attrs, block))
            continue
        header.append(line)
        i += 1

    if not blocks:
        return playlist

    def dims(attrs):
        res = attrs.get("RESOLUTION", "")
        m = re.match(r"(\d+)x(\d+)", res)
        if not m:
            return 0, 0
        return int(m.group(1)), int(m.group(2))

    def bitrate(attrs):
        try:
            return int(attrs.get("BANDWIDTH", "0"))
        except ValueError:
            return 0

    if quality in ("source", "max", "best", ""):
        chosen = max(blocks, key=lambda b: bitrate(b[0]))
    else:
        target = int(quality)
        scored = []
        for attrs, block in blocks:
            w, h = dims(attrs)
            size = min(w, h)
            scored.append((size, bitrate(attrs), attrs, block))
        under = [s for s in scored if s[0] and s[0] <= target]
        chosen = max(under, key=lambda s: (s[0], s[1])) if under else min(
            [s for s in scored if s[0]] or scored, key=lambda s: s[0])

    header = [ln for ln in header if not ln.startswith("#EXT-X-MOUFLON-ADVERT")]
    return "\n".join(header + chosen[-1]) + "\n"
