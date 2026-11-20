"""The lightweight viewer keeps identity, HIGH quality, and session cleanup explicit."""
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
import threading
from unittest.mock import Mock,patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from superlive_viewer import LightViewer,FACTORY_JS,VIEWER_JS,rewrite_factories,open_viewer,cached_live,ViewerUnavailable,isolate_channel
from recorder import sync_playwright,open_context,Live,StreamRecorder


class ViewerTests(unittest.TestCase):
    def viewer(self,data=None):
        rec=SimpleNamespace(_accept_live=Mock(return_value=True))
        context=Mock();page=Mock();page.evaluate.return_value=False
        viewer=LightViewer(rec,context,Mock());viewer.page=page
        viewer.join=['app','channel','token',42];viewer.token='token'
        viewer.api=Mock(return_value=data or {'stream_details':{'agora_id':42,'agora_channel_token':'token','channel_id':'channel'}})
        return viewer

    def test_refresh_does_not_renew_unchanged_token_but_rejects_other_viewer(self):
        viewer=self.viewer();viewer.refresh(force=True)
        self.assertEqual(viewer.page.evaluate.call_count,1)
        viewer.api.return_value['stream_details']['agora_id']=99
        with self.assertRaisesRegex(RuntimeError,'identity changed'):viewer.refresh(force=True)

    def test_live_end_stops_before_token_update(self):
        viewer=self.viewer();viewer.recorder._accept_live.return_value=False
        viewer.refresh(force=True)
        self.assertIn('__slRec.stop()',viewer.page.evaluate.call_args.args[0])

    def test_expiring_token_is_renewed_from_verified_response(self):
        viewer=self.viewer();viewer.page.evaluate.return_value=True
        with patch('superlive_viewer.media_eval') as evaluate:
            viewer.refresh(force=True)
        self.assertIn('renewToken',evaluate.call_args.args[1])
        self.assertEqual(evaluate.call_args.args[2],'token')

    def test_new_channel_cannot_reuse_the_old_viewer_tracks(self):
        viewer=self.viewer();viewer.api.return_value['stream_details']['channel_id']='another'
        with self.assertRaisesRegex(RuntimeError,'channel changed'):viewer.refresh(force=True)

    def test_failed_bootstrap_always_closes_its_session(self):
        with patch.object(LightViewer,'open',side_effect=RuntimeError('unavailable')),patch.object(LightViewer,'close') as close:
            with self.assertRaises(RuntimeError):open_viewer(Mock(),Mock(),Mock())
        close.assert_called_once()

    def test_cleanup_error_does_not_hide_the_original_failure_stage(self):
        with patch.object(LightViewer,'open',side_effect=RuntimeError('SDK secret omitted')),patch.object(LightViewer,'close',side_effect=RuntimeError('cleanup failed')):
            with self.assertRaisesRegex(ViewerUnavailable,'^bootstrap$'):open_viewer(Mock(),Mock(),Mock())

    def test_non_target_room_actions_are_blocked_in_bootstrap_and_fallback(self):
        page=Mock();rec=SimpleNamespace(live=SimpleNamespace(livestream_id='123'))
        isolate_channel(page,rec,False)
        room=page.route.call_args_list[-1].args[1]
        route=Mock();route.request.post_data_json={'livestream_id':'999'};room(route);route.abort.assert_called_once()
        route=Mock();route.request.post_data_json={'livestream_id':'123'};room(route);route.fallback.assert_called_once()

    def test_guests_do_not_disable_the_verified_hosts_lightweight_bootstrap(self):
        rec=SimpleNamespace(live=SimpleNamespace(livestream_id='123',channel_id='target',url='https://superlivechat.tv/pt/livestream/123'),
            stop_event=threading.Event(),_live_ended=False,_accept_live=Mock(return_value=True))
        page=Mock();context=Mock();viewer=LightViewer(rec,context,page)
        route=Mock();route.request.post_data_json={'livestream_id':'123'}
        route.request.url='https://api.superlivechat.tv/api/web/livestream/retrieve'
        route.request.all_headers.return_value={}
        response=route.fetch.return_value;response.status=200
        response.json.return_value={'user':{'user_id':'host'},'stream_details':{'type':1,'guests':[{'user_id':'guest'}],'is_group_stream':True}}
        def navigate(*args,**kwargs):
            handler=next(call.args[1] for call in page.route.call_args_list if call.args[0]=='**/api/web/livestream/retrieve*')
            handler(route)
        page.goto.side_effect=navigate;page.evaluate.return_value=[['app','target','token',42]]
        viewer.prepare()
        route.abort.assert_not_called();route.fulfill.assert_called_once()
        self.assertEqual(viewer.join,['app','target','token',42])
        rec._accept_live.assert_called_once_with(response.json.return_value)
        context.new_page.assert_not_called()

    def test_unmapped_sdk_does_not_disable_capture_by_guessing_the_channel(self):
        for code,isolated in [('bootstrap_no_target_join',False),('bootstrap',False),('unsupported_stream_type',False),('tracks',True)]:
            rec=StreamRecorder(Live('tester','123','',0,''),Path('.'),stop_event=threading.Event())
            page=Mock();page.url=rec.live.url;context=Mock();context.new_page.return_value=page
            with patch('superlive_viewer.open_viewer',side_effect=ViewerUnavailable(code)),patch('superlive_viewer.isolate_channel') as isolate,patch.object(rec,'_wait_video',return_value=False):
                self.assertEqual(rec._record_segment(context,1),(None,'novideo'))
            self.assertEqual(isolate.called,isolated)

    def test_api_errors_dispose_response(self):
        viewer=LightViewer(Mock(),Mock(),Mock());viewer.api_base='https://api.superlivechat.tv'
        viewer.request={'headers':{},'data':{}};response=viewer.context.request.post.return_value
        response.status=503
        with self.assertRaises(RuntimeError):viewer.api('retrieve')
        response.dispose.assert_called_once()

    def test_private_gate_uses_current_live_id_and_always_checks_identity(self):
        recorder=SimpleNamespace(_viewer_request={'data':{'livestream_id':'old','client_params':{}},'headers':{}},
            live=SimpleNamespace(livestream_id='new'),_accept_live=Mock(return_value=False))
        context=Mock();response=context.request.post.return_value;response.status=200
        response.json.return_value={'stream_details':{'livestream_id':'new'},'user':{'user_id':'123'}}
        self.assertFalse(cached_live(recorder,context))
        recorder._accept_live.assert_called_once_with(response.json.return_value)
        self.assertEqual(context.request.post.call_args.kwargs['data']['livestream_id'],'new')
        response.dispose.assert_called_once();context.new_page.assert_not_called()
        response.status=503;recorder._accept_live.reset_mock()
        self.assertIsNone(cached_live(recorder,context));recorder._accept_live.assert_not_called()
        response.status=200;response.json.return_value={'user':{},'stream_details':{}}
        self.assertIsNone(cached_live(recorder,context));recorder._accept_live.assert_not_called()

    def test_real_browser_factory_does_not_join_recommendations_and_viewer_never_requests_low(self):
        with sync_playwright() as pw:
            browser,context=open_context(pw,False)
            try:
                page=context.new_page();page.goto('about:blank')
                page.evaluate(FACTORY_JS)
                code=rewrite_factories('sdk.createClient({}); ns.sdk.createClient({});')
                self.assertEqual(code,'window.__slFactory(sdk,{}); window.__slFactory(ns.sdk,{});')
                value=page.evaluate("""async()=>{
                  window.__slTargetChannel='target';let actualJoins=0;
                  const sdk={createClient:()=>({join:()=>actualJoins++,subscribe:()=>{}})};
                  const c=window.__slFactory(sdk,{});
                  const uid=await c.join('app','target','token',42);
                  const other=await Promise.race([c.join('app','other','token',42),new Promise(r=>setTimeout(()=>r('blocked'),10))]);
                  return {uid,other,actualJoins};
                }""")
                self.assertEqual(value,dict(uid=42,other=42,actualJoins=0))
                value=page.evaluate("""async()=>{
                  let connected=[];window.__slBootstrapOnly=false;window.__slTargetChannel='target';
                  const sdk={createClient:()=>({join:async(...args)=>{connected.push(args[1]);return args[3]},subscribe:()=>{}})};
                  const c=window.__slFactory(sdk,{});
                  await c.join('app','recommendation','token',42);
                  await c.join('app','target','token',42);
                  return connected;
                }""")
                self.assertEqual(value,['target'])
                page.evaluate("""()=>{
                  window.__calls=[];window.__handlers={};
                  window.AgoraRTC={setLogLevel:()=>{},createClient:()=>({
                    on:(event,handler)=>__handlers[event]=handler,
                    setRemoteDefaultVideoStreamType:type=>__calls.push(['quality',type]),
                    setStreamFallbackOption:(uid,type)=>__calls.push(['fallback',uid,type]),
                    subscribe:(user,kind)=>__calls.push(['subscribe',user.uid,kind]),
                    join:()=>{}
                  })};
                }""")
                page.evaluate(VIEWER_JS,dict(join=['app','target','token',42]))
                page.evaluate("""async()=>{
                  const user=uid=>({uid,videoTrack:{getMediaStreamTrack:()=>({id:'host'})}});
                  await __handlers['user-published'](user(999),'video');
                  await __handlers['user-published'](user(1),'video');
                }""")
                self.assertEqual(page.evaluate('()=>__calls'),[['quality',0],['fallback',1,0],['subscribe',1,'video']])
            finally:context.close();browser.close()
