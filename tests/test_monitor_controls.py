"""Owner-bound send/remove controls, Unicode names, free slots and unlimited time."""
from dataclasses import replace
import json
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from telegram import Update
from telegram.request import HTTPXRequest
from telegram_bot import Commands,build_application,BOT_COMMANDS
from test_telegram import RegistryFixture,until


class MonitorControlsTests(RegistryFixture):
    async def profile(self):
        self.registry.add(1,10,'alvo');self.registry.add(2,20,'alvo')
        self.store.fix_identity('alvo','111')
        self.store.update_superlive_profile('alvo',dict(user_id=111,username='alvo',name='𝒜𝓃𝒶 💖 <name>'))
        monitor=self.registry.monitors['alvo'];await until(monitor.recorder.started.is_set)
        monitor.recorder.flush_event.clear()
        return monitor

    async def test_send_and_remove_are_owner_bound_and_send_keeps_both_recordings(self):
        monitor=await self.profile();answers=[];edits=[]
        generation=self.store.subscription(1,'alvo')['generation']
        async def request(request,url,method,request_data=None,**kwargs):
            endpoint=url.rsplit('/',1)[-1];data=request_data.parameters if request_data else {}
            if endpoint=='getMe':result={'id':123456,'is_bot':True,'first_name':'Test','username':'test_bot'}
            elif endpoint=='answerCallbackQuery':answers.append(data);result=True
            elif endpoint=='editMessageText':
                edits.append(data);result={'message_id':1,'date':1,'chat':{'id':data['chat_id'],'type':'private'},'text':data['text']}
            else:raise AssertionError(endpoint)
            return 200,json.dumps({'ok':True,'result':result}).encode()
        app=build_application(self.config,self.registry)
        async def click(action,uid=1):
            query=dict(id='query',chat_instance='test',data=action+':'+generation,
                **{'from':{'id':uid,'is_bot':False,'first_name':'Test'}},
                message={'message_id':1,'date':1,'chat':{'id':uid*10,'type':'private'},'text':'monitor'})
            await app.process_update(Update.de_json({'update_id':1,'callback_query':query},app.bot))
        with patch.object(HTTPXRequest,'do_request',request):
            await app.initialize()
            try:
                await click('remove',2)
                self.assertTrue(answers[-1]['show_alert']);self.assertIsNotNone(self.store.subscription(1,'alvo'))
                before=dict(self.store.subscription(1,'alvo'))
                await click('send')
                self.assertEqual(dict(self.store.subscription(1,'alvo')),before)
                self.assertTrue(monitor.recorder.flush_event.is_set())
                self.assertFalse(monitor.recorder.stop_event.is_set())
                self.assertEqual(self.store.recipients('alvo','123456'),{1:10,2:20})
                self.assertIn(' (111)',edits[-1]['text']);self.assertIn('&lt;name&gt;',edits[-1]['text'])
                buttons=edits[-1]['reply_markup']['inline_keyboard']
                self.assertEqual([[b['callback_data'].split(':')[0] for b in row] for row in buttons],
                    [['pause','remove'],['refresh']])
                await click('remove')
                self.assertIsNone(self.store.subscription(1,'alvo'))
                self.assertIsNotNone(self.store.subscription(2,'alvo'))
                self.assertFalse(monitor.recorder.stop_event.is_set())
                self.assertEqual(edits[-1]['text'].count('Slot vazio'),3)
                self.registry.add(1,10,'alvo');new=self.store.subscription(1,'alvo')['generation']
                self.assertNotEqual(new,generation)
                await click('remove')
                self.assertTrue(answers[-1]['show_alert']);self.assertEqual(self.store.subscription(1,'alvo')['generation'],new)
            finally:await app.shutdown()

    async def test_styled_profile_is_removed_by_number_parentheses_or_copied_label(self):
        for target in ('111','(111)','𝒜𝓃𝒶 💖 <name> (111) · SuperLive'):
            await self.profile()
            page=Commands(self.registry).monitor_pages(1,10)[0][0]
            self.assertIn('𝒜𝓃𝒶 💖 &lt;name&gt; (111)',page)
            self.registry.remove(1,10,target)
            self.assertIsNone(self.store.subscription(1,'alvo'))
            self.assertIsNotNone(self.store.subscription(2,'alvo'))

    async def test_slots_count_paused_profiles_and_do_not_apply_to_superuser(self):
        commands=Commands(self.registry)
        self.assertEqual(commands.monitor_pages(1,10)[0][0].count('Slot vazio'),3)
        self.registry.add(1,10,'one');self.registry.set_enabled(1,10,'one',False)
        self.assertEqual(commands.monitor_pages(1,10)[0][0].count('Slot vazio'),2)
        self.registry.add(1,10,'two');self.registry.add(1,10,'three')
        self.assertNotIn('Slot vazio',commands.monitor_pages(1,10)[0][0])
        with self.assertRaises(ValueError):self.registry.add(1,10,'four')
        self.registry.config=replace(self.config,superuser_id=9)
        self.assertNotIn('Slot vazio',commands.monitor_pages(9,90)[0][0])

    async def test_historical_usage_never_blocks_capture_or_send_and_limits_command_is_removed(self):
        monitor=await self.profile()
        self.store.charge_quota(1,'2026-11-19',99999999)
        self.assertEqual(self.store.recipients('alvo','123456'),{1:10,2:20})
        monitor.recorder.sample['recorded_seconds']=999999999
        self.registry.send_part(1,10,'111')
        self.assertTrue(self.store.subscription(1,'alvo')['enabled'])
        self.assertEqual(self.store.subscription(1,'alvo')['stopped_live_id'],'')
        self.assertNotIn('limits',[name for name,label in BOT_COMMANDS])
        self.assertFalse(hasattr(self.registry,'quota'))
