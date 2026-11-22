"""Discover public TikTok lives and their advertised video streams."""
from dataclasses import dataclass
import json
import re
from urllib.parse import urlsplit

from streamlink.exceptions import PluginError
from streamlink.plugins.tiktok import TikTok
from streamlink.stream.http import HTTPStream

from stripchat_capture import IdentityError


USER_ROOM = 'https://www.tiktok.com/api-live/user/room'
ROOM_INFO = 'https://webcast.tiktok.com/webcast/room/info/'
MAX_RESPONSE = 8 * 1024 * 1024


@dataclass(frozen=True)
class TikTokLive:
    owner_id: str
    stream_id: str
    title: str
    streams: dict


def object_at(value, *keys):
    for key in keys:
        value = value.get(key) if isinstance(value, dict) else None
    return value if isinstance(value, dict) else {}


def json_object(value):
    if isinstance(value, (str, bytes)):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return object_at(value)


def identifier(value):
    if isinstance(value, (str, int)):
        value = str(value)
        if value.isascii() and value.isdecimal() and value.lstrip('0'):
            return value
    return ''


def page_data(page, script_id):
    match = re.search(rf'''<script\b[^>]*\sid\s*=\s*["']{script_id}["'][^>]*>(.*?)</script\s*>''',
                      page.decode('utf-8', errors='replace'), re.S)
    return json_object(match[1] if match else '')


def fetch(session, url, referer, *, params=None):
    headers = {'Referer': referer, 'Cache-Control': 'no-cache'}
    try:
        with session.http.get(url, params=params, headers=headers, allow_redirects=False, stream=True, timeout=8) as response:
            if response.status_code != 200:
                return b''
            body = bytearray()
            for chunk in response.iter_content(chunk_size=65536):
                if len(body) + len(chunk) > MAX_RESPONSE:
                    return b''
                body.extend(chunk)
            return bytes(body)
    except (PluginError, OSError):
        return b''


def available_streams(session, configurations, flv_urls=None, flv_params=None):
    streams = {}

    def add(quality, url, params, default_codec):
        quality = str(quality).lower()
        quality = {'origion': 'origin', 'hd1': 'hd', 'sd1': 'sd', 'sd2': 'ld', 'full_hd1': 'uhd'}.get(quality, quality)
        if quality not in TikTok.QUALITY_WEIGHTS or quality == 'ao':
            return
        if not isinstance(url, str):
            return
        try:
            parsed = urlsplit(url)
            if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
                return
        except ValueError:
            return
        sdk = json_object(params)
        codec = str(sdk.get('VCodec') or sdk.get('v_codec') or default_codec).lower()
        codec = {'avc': 'h264', 'hevc': 'h265'}.get(codec, codec)
        if codec in ('h264', 'h265'):
            streams.setdefault(f'{codec}_{quality}', HTTPStream(session, url))

    for config, codec in configurations:
        data = object_at(json_object(config.get('stream_data')), 'data')
        for quality, value in data.items():
            main = object_at(value, 'main')
            add(quality, main.get('flv'), main.get('sdk_params'), codec)
    for key, url in object_at(flv_urls).items():
        add(key, url, object_at(flv_params).get(key), 'h264')
    return streams


def profile_owner(user, username):
    owner_id = identifier(user.get('id'))
    if owner_id and user.get('uniqueId'):
        if str(user['uniqueId']).casefold() != username:
            raise IdentityError('profile_name_changed')
        return owner_id
    return ''


def public_live(session, url, username, expected_owner):
    response = json_object(fetch(session, USER_ROOM, url,
        params={'aid': 1988, 'sourceType': 54, 'uniqueId': username}))
    data = object_at(response, 'data') if response.get('statusCode') in (0, '0') else {}
    user, room = object_at(data, 'user'), object_at(data, 'liveRoom')
    api_owner = profile_owner(user or object_at(room, 'owner'), username)
    api_owner = api_owner or identifier(room.get('ownerUserId'))
    if api_owner and expected_owner and api_owner != expected_owner:
        raise IdentityError('profile_identity_changed')
    primary = None
    if room.get('status') in (2, '2'):
        if room.get('liveSubOnly') in (1, '1', True):
            return None, 'restricted'
        source = available_streams(session, [(object_at(room, section, 'pull_data'), codec)
            for section, codec in (('streamData', 'h264'), ('hevcStreamData', 'h265'))])
        primary = TikTokLive(api_owner, identifier(room.get('streamId')), str(room.get('title') or ''), source)
        if primary.owner_id and primary.stream_id and source:
            return primary, 'public'

    page = fetch(session, url.removesuffix('/live'), url)
    profile = object_at(page_data(page, '__UNIVERSAL_DATA_FOR_REHYDRATION__'),
                        '__DEFAULT_SCOPE__', 'webapp.user-detail', 'userInfo', 'user')
    owner = profile_owner(profile, username)
    room_id = identifier(profile.get('roomId')) if owner else ''
    live_status = None
    if not room_id or primary is None or not primary.streams:
        page = fetch(session, url, url)
        live_info = object_at(page_data(page, '(?:SIGI_STATE|sigi-persisted-data)'), 'LiveRoom', 'liveRoomUserInfo')
        live_user = object_at(live_info, 'user')
        live_room = identifier(live_user.get('roomId'))
        live_owner = profile_owner(live_user, username)
        if live_room and live_owner:
            owner, room_id = live_owner, live_room
            live_status = object_at(live_info, 'liveRoom').get('status')
    if owner and expected_owner and owner != expected_owner:
        raise IdentityError('profile_identity_changed')
    if not room_id and api_owner and (not owner or owner == api_owner) and room.get('status') in (2, '2', 4, '4'):
        room_id = identifier(user.get('roomId'))
        owner = api_owner

    if room_id:
        response = json_object(fetch(session, ROOM_INFO, url, params={'aid': 1988, 'room_id': room_id}))
        detail = object_at(response, 'data') if response.get('status_code') in (0, '0') else {}
        detail_id = identifier(detail.get('id_str') or detail.get('id'))
        detail_owner = identifier(detail.get('owner_user_id_str') or detail.get('owner_user_id'))
        detail_owner = detail_owner or identifier(object_at(detail, 'owner').get('id_str') or object_at(detail, 'owner').get('id'))
        if detail_id == room_id and detail_owner == owner:
            if detail.get('status') in (2, '2'):
                filters = object_at(detail, 'stream_url_filtered_info')
                if filters.get('is_gated_room') or filters.get('is_paid_event'):
                    return None, 'restricted'
                stream_url = object_at(detail, 'stream_url')
                streams = available_streams(session, [(object_at(stream_url, 'live_core_sdk_data', 'pull_data'), 'h264')],
                                         stream_url.get('flv_pull_url'), stream_url.get('flv_pull_url_params'))
                stream_id = identifier(detail.get('stream_id_str') or detail.get('stream_id'))
                if not stream_id and primary and api_owner == owner and identifier(user.get('roomId')) == room_id:
                    stream_id = primary.stream_id
                live = TikTokLive(owner, stream_id, str(detail.get('title') or ''), streams)
                return live, 'public' if stream_id and streams else 'live_no_stream'
            if detail.get('status') in (4, '4') and live_status in (2, '2'):
                return TikTokLive(owner, '', str(detail.get('title') or ''), {}), 'live_no_stream'
            if detail.get('status') in (4, '4') and (primary is None or
                    (api_owner == owner and identifier(user.get('roomId')) == room_id)):
                return None, 'offline'
    elif owner and api_owner == owner and room.get('status') in (4, '4') and profile.get('roomId') in ('', '0', 0):
        return None, 'offline'

    if primary and primary.owner_id:
        return primary, 'live_no_stream'
    return None, 'unavailable'
