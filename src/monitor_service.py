"""Uma captura por conta, compartilhada entre as inscricoes persistidas."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import logging
import threading
import re
from zoneinfo import ZoneInfo

from recorder import Live, StreamRecorder, sync_playwright, open_context, PWError
from platforms import profile_key,split_profile
from stripchat_recorder import StripchatRecorder
from streamlink_recorder import StreamlinkRecorder
from browser_pool import BrowserPool
from resource_control import CaptureGate,PlatformBackoff
from i18n import tr,decimal,UserError

log = logging.getLogger(__name__)


def streamer_username(target):
    return profile_key(target)


@dataclass
class Monitor:
    recorder: StreamRecorder
    task: asyncio.Task | None = None
    result: object = None


class MonitorRegistry:
    def __init__(self, config, store, delivery):
        self.config, self.store, self.delivery = config, store, delivery
        self.monitors = {}
        self.closed = False
        self.loop = None
        self.zone=ZoneInfo('America/Sao_Paulo')
        self.resource_task=None
        self.resource_stop=asyncio.Event()
        self.browsers=BrowserPool()
        self.gate=CaptureGate(config,store,delivery)
        self.delivery.can_process=lambda:bool(self.gate.sample and not self.gate.blocked and not self.gate.reason
            and not self.gate.starting and self.gate.sample['cpu']<.45 and self.gate.sample['memory']<.7)
        self.backoff=PlatformBackoff()
        # Long-lived captures must not occupy asyncio's pool used by processing and uploads.
        self.capture_workers=ThreadPoolExecutor(max_workers=128,thread_name_prefix='capture')

    def is_superuser(self,user_id):return user_id==self.config.superuser_id

    def membership_changed(self,user_id,profile,leaving=False):
        monitor=self.monitors.get(profile)
        progress=monitor.recorder.progress() if monitor else {}
        if progress.get('recording') and progress.get('part_id'):
            if leaving:self.store.leave_capture(user_id,profile,progress['part_id'])
            monitor.recorder.request_part()

    def user(self, user_id, chat_id):
        self.store.user(user_id, chat_id)
        self.reconcile()

    def recipients(self, username):
        return self.store.recipients(username.casefold())

    def _check_limit(self, user_id, username):
        if self.is_superuser(user_id):return
        active = self.store.subscriptions(user_id)
        limit = self.config.max_monitors_per_user
        if username not in active and len(active) >= limit:
            log.info('limite individual atingido user_id=%s ativos=%s limite=%s',user_id,len(active),limit)
            raise UserError('Profile limit of {limit} reached. Paused profiles also count. Remove a profile before adding another.',limit=limit)

    def add(self, user_id, chat_id, target, platform=None):
        if self.closed:
            raise UserError('The bot is shutting down. Please try again shortly.')
        username = self.store.superlive_alias(profile_key(target,platform),owner_id=user_id)
        if username.startswith('shared/'):
            raise UserError('Use /add to resolve the ID shown in SuperLive.')
        if self.store.subscription(user_id,username) is None:
            self._check_limit(user_id,username)
        added = self.store.add(user_id, chat_id, username)
        self.reconcile()
        if added:self.membership_changed(user_id,username)
        log.info('inscricao adicionada user_id=%s chat_id=%s streamer=%s nova=%s', user_id,chat_id,username,added)
        return username, added

    async def resolve_add_target(self,target,platform=None):
        key=profile_key(target,platform)
        if not key.startswith(('profile/','shared/')):return self.store.superlive_alias(key),None
        endpoint=await self.browsers.endpoint() if self.config.low_resource_mode else None
        cancelled=threading.Event()
        def lookup():
            kind,number=key.split('/',1)
            live=Live('','','',0,'',**({'profile_id':number} if kind=='profile' else {'shared_id':number}))
            recorder=StreamRecorder(live,self.config.spool,stop_event=cancelled)
            with self.gate.checking(cancelled),sync_playwright() as pw:
                browser,context=open_context(pw,False,self.config.state,endpoint=endpoint)
                try:recorder._resolve_profile(context);return recorder.profile_user
                finally:context.close();browser.close()
        try:user=await asyncio.wait_for(asyncio.to_thread(lookup),45)
        except (PWError,RuntimeError,ValueError,TimeoutError,InterruptedError):
            raise UserError('Could not resolve this SuperLive profile. Check the link or ID and try again.') from None
        finally:cancelled.set()
        return self.store.superlive_alias('profile/'+str(user['user_id']),user),user

    def remove(self, user_id, chat_id, target):
        username = self._subscription(user_id,target)['username']
        if not self.store.remove(user_id, username):
            raise UserError('This profile is not on your list.')
        self.membership_changed(user_id,username,True)
        self.reconcile()
        log.info('inscricao removida user_id=%s chat_id=%s streamer=%s',user_id,chat_id,username)
        return username

    def _subscription(self,user_id,target):
        target=target.strip()
        if not target.startswith(('http://','https://')):
            identifier=re.search(r'\(([1-9]\d{0,18})\)(?:\s*·\s*SuperLive)?$',target)
            if identifier:target=identifier[1]
        if not target.strip().startswith(('http://','https://','stripchat:','superlive:','kick:','tiktok:')):
            canonical=self.store.superlive_alias(profile_key(target),owner_id=user_id)
            matches=[key for key in self.store.subscriptions(user_id) if split_profile(key)[1]==target.strip().lstrip('@').casefold() or key==canonical]
            if len(matches)>1:
                raise UserError('This name exists on multiple platforms. Send the profile URL or platform:username.')
            username=matches[0] if matches else canonical
        else:username=self.store.superlive_alias(profile_key(target),owner_id=user_id)
        row = self.store.subscription(user_id,username)
        if row is None:
            raise UserError('This profile is not on your list.')
        return row

    def set_enabled(self,user_id,chat_id,target,enabled):
        row = self._subscription(user_id,target)
        username = row['username']
        self.store.set_enabled(user_id,username,enabled)
        if bool(row['enabled'])!=enabled or (enabled and row['stopped_live_id']):
            self.membership_changed(user_id,username,not enabled)
        self.reconcile()
        log.info('monitoramento alterado user_id=%s chat_id=%s streamer=%s ativado=%s',user_id,chat_id,username,enabled)
        return username

    def send_part(self,user_id,chat_id,target):
        row=self._subscription(user_id,target);username=row['username']
        monitor=self.monitors.get(username)
        progress=monitor.recorder.progress() if monitor else {}
        if not row['enabled']:raise UserError('Monitoring is paused. Use Resume in /monitor.')
        if not progress.get('recording') or not progress.get('part_bytes') or not any(
                member['user_id']==user_id for member in self.store.capture_users(
                    username,progress.get('part_id',''),progress.get('live_id'))):
            raise UserError('There is no current recording part to send.')
        monitor.recorder.request_part()
        log.info('parte solicitada user_id=%s streamer=%s; captura continua',user_id,username)
        return username

    def stop(self,user_id,chat_id,target):
        row = self._subscription(user_id,target)
        username = row['username']
        monitor = self.monitors.get(username)
        if not row['enabled']:
            raise UserError('Monitoring is paused. Use Resume in /monitor.')
        progress = monitor.recorder.progress() if monitor else {}
        if not progress.get('online') or not progress.get('live_id'):
            raise UserError('There is no confirmed live to stop.')
        if row['stopped_live_id'] == progress['live_id']:
            raise UserError('Your recording of this live has already stopped; the next live will be recorded automatically.')
        part_id=progress['part_id']
        members=self.store.capture_snapshot(part_id,username)
        if members is not None and not any(m['user_id']==user_id and m['generation']==row['generation']
                                          and m.get('active',True) for m in members):
            part_id=''
        self.store.stop_live(user_id,username,progress['live_id'],part_id)
        monitor.recorder.request_part()
        log.info('stop individual user_id=%s chat_id=%s streamer=%s live=%s parte=%s',
                 user_id,chat_id,username,progress['live_id'],progress['part_id'])
        return username,bool(part_id)

    @staticmethod
    def elapsed(seconds):
        seconds=max(0,int(seconds))
        clock=f'{seconds//60%60:02d}:{seconds%60:02d}'
        return f'{seconds//3600:02d}:{clock}' if seconds>=3600 else clock

    def details(self,user_id,chat_id):
        self.user(user_id,chat_id)
        language=self.store.language(user_id)
        def t(message,**values):return tr(language,message,**values)
        rows = []
        for username in self.store.subscriptions(user_id):
            sub = self.store.subscription(user_id,username)
            monitor = self.monitors.get(username)
            progress = monitor.recorder.progress() if monitor else {}
            member=any(r['user_id']==user_id for r in self.store.capture_users(
                username,progress.get('part_id',''),progress.get('live_id')))
            online = progress.get('online')
            recording=bool(sub['enabled'] and member and progress.get('recording')
                           and progress.get('part_bytes'))
            stopped = bool(progress.get('live_id') and sub['stopped_live_id']==progress['live_id'])
            state = self.status(username,language)
            if not sub['enabled']:
                state = t('⏸️ Monitoring paused')
            elif stopped:
                state = t('⏹️ Recording stopped for this live · Waiting for the next live')
                recording=False
            elif monitor and monitor.recorder.status=='waiting_resources':
                reason={'cpu':t('CPU usage'),'memory':t('memory usage'),'disk':t('free disk space'),
                        'metrics':t('resource metrics'),'starting':t('resource check'),
                        'recovery':t('resource recovery')}.get(self.gate.reason,t('capture capacity'))
                state=t('⏳ Waiting for resources · Position {position} · {reason}',
                        position=self.gate.position(username),reason=reason)
            elif progress.get('access') in ('private','restricted','blocked','unavailable'):
                state={
                    'private':t('🔒 Private live · Waiting for a public stream'),
                    'restricted':t('🔒 Restricted room · Waiting for a public stream'),
                    'blocked':t('🚫 Profile blocked/geoblocked · Monitoring continues'),
                    'unavailable':t('❔ Profile unavailable · Retrying'),
                }[progress['access']]
                recording=False
            elif recording:
                state = t('🔴 Recording · Sending in parts')
            elif progress.get('recording') and not member:
                state=t('👁 Waiting for the next part for your subscription')
            elif progress.get('recording') and not progress.get('part_bytes'):
                state=t('👁 Live · Waiting for recording data')
            elif online is False:
                state = t('👁 Offline · Waiting for a new live')
            elif online is True and (not monitor or monitor.recorder.status in ('offline','watching')):
                state = t('👁 Live · Waiting for capture')
            else:
                state = '👁 '+state[:1].upper()+state[1:]
            icon={True:'🟢',False:'⚫',None:'❔'}.get(online,'❔')
            label=self.store.profile_label(username,with_id=True)
            text=f'{icon} {label}\n└ {state}'
            if recording:
                megabytes=decimal(language,progress['part_bytes']/1e6)
                limit=decimal(language,self.config.part_bytes/1e6,0)
                text += f"\n└ ⏱ {self.elapsed(progress['part_seconds'])} | 📦 {megabytes} MB / {limit} MB"
            sent = self.store.delivery_status(user_id,username)
            pending = sum(sent.get(k,0) for k in ('pending','retry','sending'))
            failures = sum(sent.get(k,0) for k in ('failed','expired','uncertain'))
            if pending:
                text += '\n└ '+t('📤 {count} queued part(s)',count=pending)
            if failures:
                text += '\n└ '+t('⚠️ {count} failed/unconfirmed delivery(s)',count=failures)
            rows.append(dict(username=username,generation=sub['generation'],enabled=bool(sub['enabled']),
                             label=label,platform=split_profile(username)[0],
                             stopped=stopped,can_send=bool(recording),can_stop=bool(sub['enabled'] and not stopped and progress.get('online')
                                                          and member
                                                          and progress.get('access') not in ('private','restricted','blocked','unavailable')),
                             online=online,recording=recording,text=text))
        return rows

    def list(self, user_id, chat_id):
        return [(r['username'],r['text']) for r in self.details(user_id,chat_id)]

    def status(self, username,language='en'):
        monitor = self.monitors.get(username)
        if not monitor:
            return tr(language,'Waiting for monitoring')
        if monitor.recorder.stop_event.is_set() and not monitor.task.done():
            return tr(language,'Waiting for finalization before resuming')
        return tr(language,{
            'queued':'Waiting for capture resources', 'starting':'Starting',
            'checking':'Checking profile', 'offline':'Offline; waiting to return',
            'connecting':'Opening source stream', 'waiting_data':'Waiting for source video data',
            'recording':'Recording and sending in parts', 'reconnecting':'Reconnecting',
            'waiting_space':'Waiting for space in the delivery queue', 'finalizing':'Finalizing part',
            'waiting_resources':'Waiting for capture resources',
            'watching':'Waiting for the next live; monitoring enabled',
            'stopped':'Stopped', 'error':'Unable to access the profile. Check the username or profile URL.',
        }.get(monitor.recorder.status,'Status unavailable'))

    async def start(self):
        self.loop = asyncio.get_running_loop()
        self.delivery.blocked_callback = self.reconcile
        self.reconcile()
        self.resource_task=asyncio.create_task(self._resource_loop(),name='capture-resources')
        log.info('monitoramentos restaurados quantidade=%s',len(self.monitors))

    async def _resource_loop(self):
        while not self.resource_stop.is_set():
            self.gate.poll()
            for key,(started,owners) in list(self.gate.active.items()):
                monitor=self.monitors.get(key)
                if monitor:self.gate.active[key]=(started,set(self.store.recipients(key,monitor.recorder.live.livestream_id)))
            for key in list(self.gate.starting):
                monitor=self.monitors.get(key)
                if monitor and monitor.recorder.progress().get('part_bytes'):self.gate.starting.discard(key)
            for key in self.gate.revoked:
                monitor=self.monitors.get(key)
                if monitor:
                    monitor.recorder.request_part()
                    if not monitor.recorder.progress().get('recording'):monitor.recorder.stop_event.set()
            for key in list(self.gate.order):
                monitor=self.monitors.get(key)
                if not monitor or monitor.recorder.progress().get('online') is False:self.gate.cancel(key)
            self.gate.save_health(self.config.spool.parent/'health.json')
            try:await asyncio.wait_for(self.resource_stop.wait(),5)
            except asyncio.TimeoutError:pass

    def reconcile(self):
        if self.closed:
            return
        self.loop = self.loop or asyncio.get_running_loop()
        usernames = set(self.store.streamers())
        for username,monitor in list(self.monitors.items()):
            if username not in usernames:
                self.gate.cancel(username)
                monitor.recorder.stop_event.set()
                if monitor.task.done():
                    self.monitors.pop(username,None)
        for username in usernames:
            if username not in self.monitors:
                self._start(username)

    def _start(self, username):
        platform,handle=split_profile(username)
        factory=({'stripchat':StripchatRecorder,'superlive':StreamRecorder}.get(platform,StreamlinkRecorder))
        extra={'platform':platform} if platform in ('kick','tiktok') else {}
        uid=self.store.platform_id(username)
        profile_id=(handle.split('/',1)[1] if handle.startswith('profile/') else uid) if platform=='superlive' else ''
        rec = factory(Live('' if handle.startswith('profile/') and platform=='superlive' else handle,'','',0,'',
            user_id=uid or profile_id,profile_id=profile_id),
            self.config.spool, state={'stripchat':self.config.stripchat_state,'superlive':self.config.state}.get(platform), reconnect=-1,
            rotate=self.config.segment_seconds, segment_bytes=max(1,self.config.part_bytes-16*1024*1024),
            part_limit=self.config.part_bytes,low_resource_mode=self.config.low_resource_mode,
            stop_event=threading.Event(), segment_callback=self.delivery.segment_ready,**extra)
        if self.config.low_resource_mode and platform in ('superlive','kick'):
            def endpoint():
                return asyncio.run_coroutine_threadsafe(self.browsers.endpoint(),self.loop).result(timeout=40)
            rec.browser_endpoint_provider=endpoint
        def base():
            rec.status = 'waiting_space'
            allowed=(lambda:rec.capture_allowed(rec.live.livestream_id)) if platform in ('kick','tiktok') else None
            path=self.delivery.begin_part(rec.stop_event,allowed)
            future=asyncio.run_coroutine_threadsafe(self._begin_capture(username,path.parent.name),self.loop)
            future.result(timeout=30)
            return path
        rec._recording_base = base
        def identity(uid):
            future = asyncio.run_coroutine_threadsafe(self._identity(username,uid),self.loop)
            future.result(timeout=30)
        rec.identity_callback = identity
        rec.monitor_key=username
        rec.check_guard=lambda:self.gate.checking(rec.stop_event)
        rec.retry_delay=lambda delay:self.backoff.delay(platform,delay)
        rec.capture_failure=lambda:self.backoff.failed(platform)
        rec.capture_recovered=lambda:self.backoff.recovered(platform)
        def acquire():
            return asyncio.run_coroutine_threadsafe(self._admit(username),self.loop).result(timeout=30)
        def release():
            async def finish():self.gate.release(username)
            asyncio.run_coroutine_threadsafe(finish(),self.loop).result(timeout=30)
        rec.capture_acquire=acquire
        rec.capture_release=release
        if platform=='superlive':
            def profile(user):
                future=asyncio.run_coroutine_threadsafe(self._profile(username,user),self.loop)
                future.result(timeout=30)
            rec.profile_callback=profile
        def live_id(token,online):
            future=asyncio.run_coroutine_threadsafe(self._observed_live(username,token,online),self.loop)
            return future.result(timeout=30)
        rec.live_id_callback=live_id
        def allowed(live_id):
            future = asyncio.run_coroutine_threadsafe(self._allowed(username,live_id),self.loop)
            return future.result(timeout=30)
        rec.capture_allowed = allowed
        rec.status = 'queued'
        monitor = Monitor(rec)
        self.monitors[username] = monitor
        monitor.task = asyncio.create_task(self._run(username,monitor),name=f'monitor:{username}')

    async def _identity(self, username, uid):
        self.store.fix_identity(username,uid)

    async def _profile(self,username,user):
        self.store.update_superlive_profile(username,user)

    async def _observed_live(self, username, token, online):
        return self.store.observed_live(username,token,online)

    async def _allowed(self,username,live_id):
        interested=bool(self.store.recipients(username,live_id))
        if not interested:self.gate.cancel(username)
        return username not in self.gate.revoked and interested

    async def _admit(self,username):
        monitor=self.monitors.get(username)
        if not monitor or monitor.recorder.stop_event.is_set():return False
        users=self.store.recipients(username,monitor.recorder.live.livestream_id)
        if not users:
            self.gate.cancel(username)
            return False
        return self.gate.request(username,users)

    async def _begin_capture(self,username,part_id):
        monitor=self.monitors.get(username)
        live_id=monitor.recorder.progress().get('live_id') if monitor else None
        self.store.begin_capture(username,part_id,live_id)

    async def _run(self, username, monitor):
        try:
            while not monitor.recorder.stop_event.is_set():
                monitor.result = await self.loop.run_in_executor(self.capture_workers,monitor.recorder.run)
                if monitor.recorder.stop_event.is_set():
                    break
                if monitor.recorder._identity_error or monitor.result.error.startswith('ValueError:'):
                    break
                monitor.recorder.status = 'reconnecting'
                log.warning('motor sera retomado streamer=%s erro=%s', username,monitor.result.error)
                for _ in range(30):
                    if monitor.recorder.stop_event.is_set():
                        break
                    await asyncio.sleep(1)
        except Exception as exc:
            monitor.recorder.status = 'error'
            log.error('monitor falhou streamer=%s erro=%s',username,type(exc).__name__)
        finally:
            self.gate.release(username)
            log.info('monitor encerrado streamer=%s',username)
            if self.closed or username not in self.store.streamers():
                self.monitors.pop(username,None)
            elif monitor.recorder.stop_event.is_set():
                self._start(username)

    async def close(self):
        self.closed = True
        self.resource_stop.set()
        tasks = []
        for monitor in self.monitors.values():
            monitor.recorder.stop_event.set()
            tasks.append(monitor.task)
        if tasks:
            await asyncio.gather(*tasks)
        if self.resource_task:await asyncio.gather(self.resource_task,return_exceptions=True)
        self.capture_workers.shutdown(wait=True)
        await self.browsers.close()
        self.monitors.clear()
