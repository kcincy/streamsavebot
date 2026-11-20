"""Identidade de plataforma/perfil; chaves antigas continuam sendo SuperLive."""
import re
from urllib.parse import urlsplit
from recorder import parse_record_target
from i18n import UserError

PLATFORMS = {'superlive':'SuperLive','stripchat':'StripChat','kick':'Kick','tiktok':'TikTok'}


def split_profile(key):
    platform,separator,username=key.partition(':')
    return (platform,username) if separator and platform in PLATFORMS else ('superlive',key)


def profile_label(key,display_name=''):
    platform,username=split_profile(key)
    if platform=='superlive':username=display_name or (f'ID {username.split("/",1)[1]}' if username.startswith(('profile/','shared/')) else username)
    return f'{username} · {PLATFORMS[platform]}'


def profile_url(key):
    platform,username=split_profile(key)
    if platform=='superlive' and username.startswith('profile/'):
        return 'https://superlivechat.tv/pt/'+username
    return {'superlive':f'https://superlivechat.tv/pt/{username}',
            'stripchat':f'https://stripchat.com/{username}',
            'kick':f'https://kick.com/{username}',
            'tiktok':f'https://www.tiktok.com/@{username}/live'}[platform]


def profile_key(target,platform=None):
    raw=target.strip()
    if raw.startswith(tuple(key+':' for key in PLATFORMS)):
        selected,raw=raw.split(':',1)
        if platform and platform!=selected:
            raise UserError('The profile does not match the selected platform.')
        platform=selected
    if raw.startswith(('http://','https://')):
        parsed=urlsplit(raw)
        host=(parsed.hostname or '').lower()
        if host=='stripchat.com' or host.endswith('.stripchat.com'):detected='stripchat'
        elif host in ('kick.com','www.kick.com'):detected='kick'
        elif host in ('tiktok.com','www.tiktok.com'):detected='tiktok'
        elif host in ('superlivechat.tv','www.superlivechat.tv'):detected='superlive'
        else:raise UserError('Use a username or a valid profile URL for the selected platform.')
        if platform and platform!=detected:
            raise UserError('The URL does not match the selected platform.')
        platform=detected
        try:valid_port=parsed.port in (None,443 if parsed.scheme=='https' else 80)
        except ValueError:valid_port=False
        if parsed.username or parsed.password or not valid_port:
            raise UserError('Use a username or a valid profile URL for the selected platform.')
        if platform=='tiktok':match=re.fullmatch(r'/@([^/]+)(?:/live)?/?',parsed.path)
        elif platform in ('stripchat','kick'):match=re.fullmatch(r'/([^/]+)/?',parsed.path)
        else:match=re.fullmatch(r'/(?:[a-z]{2}/)?((?:profile/)?[^/]+)/?',parsed.path)
        if not match:raise UserError('Use a profile, not a live ID or a recorded video URL.')
        raw=match[1]
    platform=platform or 'superlive'
    if platform not in PLATFORMS:
        raise UserError('Unsupported platform.')
    if platform in ('stripchat','kick','tiktok'):
        username=raw.lstrip('@').casefold()
        patterns={'stripchat':r'[a-z0-9_.-]{2,60}','kick':r'[a-z0-9_-]{1,60}',
                  'tiktok':r'[a-z0-9_][a-z0-9_.]{0,22}[a-z0-9_]'}
        if not re.fullmatch(patterns[platform],username) or username in ('.','..'):
            raise UserError('Invalid username for {platform}.',platform=PLATFORMS[platform])
        return platform+':'+username
    identifier=re.fullmatch(r'(?:(profile/|id:|user_id:|shared/|shared:|shared_id:))?([1-9]\d{0,18})',raw.lstrip('@').casefold())
    if identifier:
        return ('shared/' if (identifier[1] or '').startswith('shared') else 'profile/')+identifier[2]
    try:live=parse_record_target(raw)
    except ValueError:raise UserError('Invalid username for {platform}.',platform='SuperLive') from None
    if live.livestream_id:
        raise UserError('Use a profile, not a live ID or a recorded video URL.')
    if len(live.username)>64:
        raise UserError('Invalid username for {platform}.',platform='SuperLive')
    return live.username.casefold()
