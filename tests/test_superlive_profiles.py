"""Anonymous SuperLive accounts: URL/ID parsing, pinned identity, names and aliases."""
from pathlib import Path
import sys,tempfile,unittest
import json,threading
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from unittest.mock import Mock,patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from platforms import profile_key,profile_url
from recorder import Live,StreamRecorder,parse_record_target,sync_playwright,open_context
from storage import Store
from test_telegram import RegistryFixture,BlockingRecorder,until
from telegram_bot import Commands

USER={'user_id':'30314654','shared_id':'89633559','name':'🐰 B O N N I E 🐰','username':None,'livestream_id':'155732788'}

class ProfileTests(unittest.TestCase):
    def test_profile_urls_and_bare_ids_are_distinct_from_live_urls(self):
        for value in ('30314654','@30314654','id:30314654','user_id:30314654','profile/30314654',
                      'https://superlivechat.tv/pt/profile/30314654','https://superlivechat.tv/profile/30314654'):
            self.assertEqual(profile_key(value),'profile/30314654',value)
            self.assertEqual(profile_key(profile_key(value)),'profile/30314654')
        self.assertEqual(profile_key('shared:89633559'),'shared/89633559')
        self.assertEqual(profile_key('superlive:shared_id:89633559'),'shared/89633559')
        self.assertEqual(profile_url('profile/30314654'),'https://superlivechat.tv/pt/profile/30314654')
        self.assertEqual(parse_record_target('30314654').livestream_id,'30314654')
        profile=parse_record_target('https://superlivechat.tv/pt/profile/30314654')
        self.assertEqual(profile.profile_id,'30314654');self.assertFalse(profile.livestream_id)
        for bad in ('https://superlivechat.tv/pt/livestream/30314654','https://evil.invalid/profile/30314654',
                    'https://superlivechat.tv/pt/profile/30314654/other','id:../../123','shared:bad'):
            with self.assertRaises(ValueError,msg=bad):profile_key(bad)

    def test_no_username_and_name_changes_keep_same_account_and_live_guards(self):
        rec=StreamRecorder(Live('','','',0,'',profile_id='30314654'),Path('.'))
        candidate=rec._profile_live({'user':USER})
        self.assertTrue(rec.following);self.assertFalse(rec.follow_username)
        self.assertEqual(candidate.livestream_id,'155732788');self.assertEqual(rec.target_user_id,'30314654')
        rec.live=candidate
        data={'user':dict(USER,username='newname',name='New name'),'stream_details':{'livestream_id':'155732788','channel_id':'target'}}
        self.assertTrue(rec._accept_live(data))
        self.assertFalse(rec._accept_live(dict(data,user=dict(USER,user_id='29574218'))))
        with self.assertRaisesRegex(ValueError,'outra conta'):rec._profile_live({'user':dict(USER,user_id='29574218')})

    def test_shared_id_requires_exact_match_not_first_search_result(self):
        rec=StreamRecorder(Live('','','',0,'',shared_id='89633559'),Path('.'))
        self.assertIsNotNone(rec._profile_live({'user':USER}))
        with self.assertRaisesRegex(ValueError,'ID compartilhado'):rec._profile_live({'user':dict(USER,shared_id='999')})

    def test_name_metadata_persists_and_update_does_not_write_unchanged_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'bot.sqlite3';store=Store(path)
            store.add(1,10,'profile/30314654');store.update_superlive_profile('profile/30314654',USER)
            trace=[];store.db.set_trace_callback(trace.append);store.update_superlive_profile('profile/30314654',USER)
            self.assertFalse(any(s.startswith('UPDATE') for s in trace))
            store.update_superlive_profile('profile/30314654',{'user_id':'30314654'})
            self.assertEqual(store.profile_name('profile/30314654'),USER['name'])
            self.assertEqual(store.superlive_alias('shared/89633559'),'profile/30314654')
            with self.assertRaises(ValueError):store.update_superlive_profile('profile/30314654',dict(USER,user_id='123'))
            store.close();store=Store(path)
            try:self.assertEqual(store.profile_label('profile/30314654'),'🐰 B O N N I E 🐰 · SuperLive')
            finally:store.close()

    def test_real_http_bootstrap_resolves_shared_search_and_caches_the_correct_profile(self):
        state={'profiles':[],'search':[],'wrong':False}
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def send(self,value,kind='application/json'):
                data=(value if isinstance(value,str) else json.dumps(value)).encode()
                self.send_response(200);self.send_header('Content-Type',kind);self.send_header('Content-Length',str(len(data)))
                self.end_headers();self.wfile.write(data)
            def do_GET(self):
                self.send("<script>fetch('/api/web/users/profile',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({user_id:location.pathname.split('/').pop(),client_params:{test:true}})})</script>",'text/html')
            def do_POST(self):
                data=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                if self.path.endswith('/users/search'):
                    state['search'].append(data)
                    self.send({'items':[dict(USER,user_id='999',shared_id='999'),USER]})
                else:
                    state['profiles'].append(data)
                    self.send({'user':dict(USER,user_id='999') if state['wrong'] else USER})
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            with patch('recorder.BASE',f'http://127.0.0.1:{server.server_port}'),sync_playwright() as pw:
                browser,context=open_context(pw,False)
                try:
                    rec=StreamRecorder(Live('','','',0,'',shared_id='89633559'),Path('.'),stop_event=threading.Event())
                    self.assertIsNotNone(rec._resolve_profile(context));self.assertIsNotNone(rec._resolve_profile(context))
                    self.assertEqual(len(state['search']),1)
                    self.assertTrue(all(p['user_id']=='30314654' for p in state['profiles']))
                    self.assertEqual(len(context.pages),0)
                    state['wrong']=True
                    with self.assertRaisesRegex(ValueError,'outra conta'):rec._resolve_profile(context)
                finally:context.close();browser.close()
        finally:server.shutdown();server.server_close();thread.join()

class ProfileRegistryTests(RegistryFixture):
    async def test_aliases_share_one_capture_and_controls_keep_generation_and_ownership(self):
        key,added=self.registry.add(1,10,'https://superlivechat.tv/pt/profile/30314654')
        self.assertTrue(added);self.assertEqual(key,'profile/30314654')
        self.store.update_superlive_profile(key,USER)
        generation=self.store.subscription(1,key)['generation']
        self.assertFalse(self.registry.add(1,10,'id:30314654')[1])
        self.assertFalse(self.registry.add(1,10,'shared:89633559')[1])
        await until(lambda:BlockingRecorder.active==1)
        pages=Commands(self.registry).monitor_pages(1,10)
        self.assertIn('<b>🐰 B O N N I E 🐰 (30314654) · SuperLive</b>',pages[0][0])
        self.assertNotIn('profile/30314654',pages[0][0])
        self.registry.set_enabled(1,10,'id:30314654',False)
        self.assertEqual(self.store.subscription(1,key)['generation'],generation)
        self.assertFalse(self.store.subscription(1,key)['enabled'])
        self.registry.set_enabled(1,10,'shared:89633559',True)
        self.registry.remove(1,10,'https://superlivechat.tv/pt/profile/30314654')
        self.assertFalse(self.store.subscriptions(1))

    async def test_id_alias_does_not_add_a_second_monitor_for_existing_named_profile(self):
        self.registry.add(1,10,'alvo')
        self.store.update_superlive_profile('alvo',dict(USER,username='alvo'))
        self.assertEqual(self.registry.add(1,10,'30314654'),('alvo',False))
        self.assertEqual(self.store.subscriptions(1),['alvo'])

    async def test_old_duplicate_aliases_preserve_the_owners_existing_subscription(self):
        for uid,key in ((2,'oldname'),(1,'myname')):
            self.store.add(uid,uid*10,key);self.store.update_superlive_profile(key,dict(USER,username='myname'))
        generation=self.store.subscription(1,'myname')['generation']
        self.assertEqual(self.registry.add(1,10,'id:30314654'),('myname',False))
        self.assertEqual(self.store.subscription(1,'myname')['generation'],generation)
        self.registry.set_enabled(1,10,'superlive:myname',False)
        self.assertTrue(self.store.subscription(2,'oldname')['enabled'])
