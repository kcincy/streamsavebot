"""A single original-quality viewer, without the site's recommendation players."""
import re
import time
from playwright.sync_api import Error as PWError
from pathlib import Path
from urllib.parse import urlsplit
from recorder import media_eval

FACTORY_JS=r"""(() => {
 window.__slJoins=[];
 window.__slBootstrapOnly=true;
 window.__slFactory=(sdk,...options)=>{
   const client=sdk.createClient(...options);
   if(typeof client.join==='function' && typeof client.subscribe==='function'){
     const join=client.join.bind(client);
     client.join=async(...args)=>{
       window.__slJoins.push(args);
       if(!window.__slBootstrapOnly && args[1]===window.__slTargetChannel)return await join(...args);
       return args[3];
     };
   }
   return client;
 };
})();"""

VIEWER_JS=r"""async p=>{
 AgoraRTC.setLogLevel(3);
 const S=window.__slViewer={host:{},error:'',needsRenew:false};
 const c=S.client=AgoraRTC.createClient({mode:'live',role:'audience',codec:'h264'});
 await c.setRemoteDefaultVideoStreamType(0);
 const stop=reason=>{S.error=reason;window.__slRec.reason=reason;window.__slRec.stop();};
 c.on('connection-state-change',(state,old,reason)=>{
   if(state==='DISCONNECTED')stop('RTC disconnected: '+(reason||''));
 });
 c.on('token-privilege-will-expire',()=>S.needsRenew=true);
 c.on('user-left',user=>{if(String(user.uid)==='1')stop('publisher left');});
 c.on('user-unpublished',(user,kind)=>{
   if(String(user.uid)==='1' && kind==='video')stop('publisher video ended');
 });
 c.on('user-published',async(user,kind)=>{
   if(String(user.uid)!=='1' || !['video','audio'].includes(kind))return;
   try{
     await c.setStreamFallbackOption(user.uid,0);
     await c.subscribe(user,kind);
     S.host[kind]=user[kind+'Track'].getMediaStreamTrack();
   }catch(e){stop('RTC subscription failed');}
 });
 await c.join(...p.join);
 return true;
}"""


def rewrite_factories(code):
    # The frontend's module names change; wrap the SDK factory before its first join.
    pattern=r'(?<![\w.$])([A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*)\.createClient\('
    return re.sub(pattern,lambda match:'window.__slFactory('+match[1]+',',code)


class ViewerUnavailable(RuntimeError):
    """Controlled reason codes are safe to log without SDK tokens or signed URLs."""


def isolate_channel(page,recorder,bootstrap_only):
    page.add_init_script(FACTORY_JS+f'\nwindow.__slBootstrapOnly={str(bootstrap_only).lower()};')
    def script(route):
        response=route.fetch()
        try:route.fulfill(response=response,body=rewrite_factories(response.text()))
        finally:response.dispose()
    def room(route):
        try:lid=str((route.request.post_data_json or {}).get('livestream_id') or '')
        except (ValueError,AttributeError):lid=''
        if lid!=recorder.live.livestream_id:route.abort()
        else:route.fallback()
    page.route('**/_nuxt/*.js',script)
    page.route('**/api/web/livestream/**',room)


def cached_live(recorder,context):
    """Reuse the normal API request while a public profile is temporarily private."""
    request=recorder._viewer_request
    response=None
    try:
        response=context.request.post('https://api.superlivechat.tv/api/web/livestream/retrieve',
            headers=request['headers'],data={**request['data'],'livestream_id':recorder.live.livestream_id},timeout=15000)
        data=response.json() if response.status==200 else None
        if (isinstance(data,dict) and isinstance(data.get('stream_details'),dict)
                and isinstance(data.get('user'),dict) and data['stream_details'].get('livestream_id')
                and data['user'].get('user_id')):
            return recorder._accept_live(data)
    except (PWError,ValueError):pass
    finally:
        if response:response.dispose()
    return None


class LightViewer:
    def __init__(self,recorder,context,bootstrap):
        self.recorder,self.context=recorder,context
        self.page=None;self.bootstrap=bootstrap;self.request=None;self.join=None;self.next_poll=0
        self.api_base=None;self.token=None;self.block_exit=lambda route:route.abort()
        self.phase='bootstrap';self.unsupported=False

    def api(self,action):
        response=self.context.request.post(self.api_base+'/api/web/livestream/'+action,
            headers=self.request['headers'],data=self.request['data'],timeout=15000)
        try:
            if response.status!=200:raise RuntimeError('viewer API unavailable')
            data=response.json()
            if not isinstance(data,dict):raise RuntimeError('invalid viewer response')
            return data
        finally:response.dispose()

    def refresh(self,force=False):
        if not force and time.monotonic()<self.next_poll:return
        data=self.api('retrieve')
        if not self.recorder._accept_live(data):
            self.page.evaluate("()=>{window.__slRec.reason='live ended or identity changed';window.__slRec.stop();return true;}")
            return
        details=data['stream_details']
        if details.get('channel_id')!=self.join[1]:raise RuntimeError('viewer channel changed')
        if str(details.get('agora_id'))!=str(self.join[3]):raise RuntimeError('viewer identity changed')
        token=details.get('agora_channel_token')
        if not token:raise RuntimeError('viewer token unavailable')
        renew=self.page.evaluate('()=>window.__slViewer.needsRenew')
        if token!=self.token or renew:
            media_eval(self.page,'(async()=>{await window.__slViewer.client.renewToken(arg);window.__slViewer.needsRenew=false;return true;})()',token)
            self.token=token
        self.next_poll=time.monotonic()+10

    def prepare(self):
        page=self.bootstrap
        isolate_channel(page,self.recorder,True)
        self.context.route('**/api/web/livestream/exit*',self.block_exit)
        def retrieve(route):
            request=route.request
            value=request.post_data_json or {}
            if str(value.get('livestream_id'))!=self.recorder.live.livestream_id:
                route.abort();return
            parsed=urlsplit(request.url)
            if parsed.scheme!='https' or parsed.hostname!='api.superlivechat.tv':
                route.abort();return
            response=route.fetch()
            try:
                data=response.json() if response.status==200 else {}
                self.request=dict(data=value,headers={k:v for k,v in request.all_headers().items()
                    if k.lower() not in ('host','content-length','cookie')})
                self.recorder._viewer_request=self.request
                if not self.recorder._accept_live(data):route.abort();return
                details=data['stream_details']
                # Guests do not change selection: only publisher 1 in the verified host channel is recorded.
                if details.get('type')!=1:
                    self.unsupported=True
                    route.abort();return
                self.api_base='https://'+parsed.hostname
                page.evaluate('channel=>window.__slTargetChannel=channel',self.recorder.live.channel_id)
                route.fulfill(response=response)
            finally:response.dispose()
        page.route('**/api/web/livestream/retrieve*',retrieve)
        page.goto(self.recorder.live.url,wait_until='domcontentloaded',timeout=30000)
        deadline=time.monotonic()+20
        while (time.monotonic()<deadline and not self.recorder.stop_event.is_set()
                and not self.recorder._live_ended and not self.unsupported):
            joins=page.evaluate('()=>window.__slJoins||[]')
            self.join=next((args[:4] for args in joins if len(args)>=4
                and args[1]==self.recorder.live.channel_id),None)
            if self.request and self.join:break
            page.wait_for_timeout(200)
        if self.recorder.stop_event.is_set():raise InterruptedError()
        if self.unsupported:raise ViewerUnavailable('unsupported_stream_type')
        if not self.request or not self.join:raise ViewerUnavailable('bootstrap_no_target_join')
        return self

    def open(self):
        self.prepare()
        page=self.bootstrap
        self.phase='rtc_join'
        self.page=self.context.new_page()
        self.page.route('https://superlivechat.tv/__streamsave_viewer',lambda route:route.fulfill(
            content_type='text/html',body='<html><body></body></html>'))
        self.page.goto('https://superlivechat.tv/__streamsave_viewer',wait_until='domcontentloaded')
        self.page.add_script_tag(content=(Path(__file__).parent/'vendor/agora-rtc-sdk-ng-4.24.10.js').read_text(encoding='utf8'))
        media_eval(self.page,'('+VIEWER_JS+')(arg)',dict(join=self.join),timeout=20000)
        self.token=self.join[2]
        self.phase='enter'
        entered=self.api('enter')
        if entered.get('error') or entered.get('disallowed_reason'):raise RuntimeError('viewer entry refused')
        self.phase='tracks'
        self.page.wait_for_function('()=>window.__slViewer.host.video&&window.__slViewer.host.audio',timeout=20000)
        self.phase='verify'
        self.refresh(force=True)
        if self.recorder._live_ended:raise RuntimeError('live ended during viewer startup')
        if self.page.evaluate("()=>!!window.__slViewer.error || window.__slViewer.client.connectionState!=='CONNECTED'"):
            raise RuntimeError('viewer disconnected during startup')
        self.page.evaluate("""p=>{
          const root=document.createElement('div');root.id='teleport-video-'+p.livestreamId;
          const video=document.createElement('video');video.dataset.pipChannel=p.channelId;
          root.append(video);document.body.append(root);
          video.srcObject=new MediaStream([__slViewer.host.video,__slViewer.host.audio]);
          window.__slRec.arm(p);
        }""",dict(livestreamId=self.recorder.live.livestream_id,channelId=self.recorder.live.channel_id))
        page.close();self.bootstrap=None
        self.phase='ready'
        return self

    def close(self):
        try:
            if self.page and not self.page.is_closed():
                try:media_eval(self.page,'(async()=>{await window.__slViewer?.client.leave();return true;})()')
                finally:self.page.close()
        finally:
            if self.bootstrap and not self.bootstrap.is_closed():self.bootstrap.close()
            self.context.unroute('**/api/web/livestream/exit*',self.block_exit)
            if self.request and self.api_base:
                try:self.api('exit')
                except Exception:pass


def open_viewer(recorder,context,bootstrap):
    viewer=LightViewer(recorder,context,bootstrap)
    try:return viewer.open()
    except Exception as exc:
        try:viewer.close()
        except Exception:pass
        if isinstance(exc,(ViewerUnavailable,InterruptedError)):raise
        raise ViewerUnavailable(viewer.phase) from exc
