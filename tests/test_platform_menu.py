"""Escolha privada por plataforma: botões expirados, homônimos e controles individuais."""
import json
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from telegram import Update
from telegram.request import HTTPXRequest
from telegram_bot import build_application
from test_telegram import RegistryFixture,BlockingRecorder,until


class PlatformMenuTests(RegistryFixture):
    async def test_escolha_de_plataforma_privada_e_controles_de_homonimos(self):
        sent=[];answers=[];edits=[]
        async def request(request,url,method,request_data=None,**kwargs):
            endpoint=url.rsplit('/',1)[-1];data=request_data.parameters if request_data else {}
            if endpoint=='getMe':result={'id':123456,'is_bot':True,'first_name':'Test','username':'test_bot'}
            elif endpoint=='answerCallbackQuery':answers.append(data);result=True
            elif endpoint in ('sendMessage','editMessageText'):
                if endpoint=='sendMessage':sent.append(data)
                else:edits.append(data)
                result={'message_id':1,'date':1,'chat':{'id':data['chat_id'],'type':'private'},'text':data['text']}
            else:raise AssertionError(endpoint)
            return 200,json.dumps({'ok':True,'result':result}).encode()
        app=build_application(self.config,self.registry);counter=0
        async def message(text,user=1,chat=10):
            nonlocal counter
            counter+=1
            update={'update_id':counter,'message':{'message_id':counter,'date':1,
                'from':{'id':user,'is_bot':False,'first_name':'Test'},'chat':{'id':chat,'type':'private'},'text':text,
                'entities':[{'type':'bot_command','offset':0,'length':len(text.split()[0])}] if text.startswith('/') else []}}
            await app.process_update(Update.de_json(update,app.bot))
        async def callback(data,user=1,chat=10,kind='private'):
            nonlocal counter
            counter+=1
            update={'update_id':counter,'callback_query':{'id':'choice','chat_instance':'test',
                'from':{'id':user,'is_bot':False,'first_name':'Test'},'data':data,
                'message':{'message_id':1,'date':1,'chat':{'id':chat,'type':kind},'text':'menu'}}}
            await app.process_update(Update.de_json(update,app.bot))
        with patch('monitor_service.StripchatRecorder',BlockingRecorder),patch.object(HTTPXRequest,'do_request',request),patch('traffic_control.TrafficGuard.allowed',return_value=True):
            await app.initialize()
            try:
                await message('/add')
                buttons=sent[-1]['reply_markup']['inline_keyboard'][0]
                self.assertEqual([[b['text'] for b in row] for row in sent[-1]['reply_markup']['inline_keyboard']],
                    [['🔵 SuperLive','🔴 StripChat'],['🟢 Kick','🟣 TikTok']])
                choice=buttons[1]['callback_data']
                await message('alvo')
                self.assertIn('Escolha primeiro',sent[-1]['text']);self.assertFalse(self.registry.monitors)
                await callback(choice,user=2,chat=20);self.assertTrue(answers[-1]['show_alert'])
                await callback(choice,chat=-100,kind='group');self.assertTrue(answers[-1]['show_alert'])
                await callback(choice)
                await message('https://superlivechat.tv/pt/alvo')
                self.assertIn('não corresponde',sent[-1]['text'])
                await message('@Alvo')
                self.assertEqual(self.store.subscriptions(1),['stripchat:alvo'])
                await message('/add')
                fresh=sent[-1]['reply_markup']['inline_keyboard'][0][0]['callback_data']
                await callback(choice);self.assertTrue(answers[-1]['show_alert'])
                await callback(fresh);await message('alvo')
                await until(lambda:BlockingRecorder.active==2)
                await message('/monitor')
                self.assertIn('alvo · SuperLive',sent[-1]['text']);self.assertIn('alvo · StripChat',sent[-1]['text'])
                await message('/remove');await message('alvo')
                self.assertIn('várias plataformas',sent[-1]['text'])
                generation=self.store.subscription(1,'alvo')['generation']
                await callback('pause:'+generation)
                self.assertFalse(self.store.subscription(1,'alvo')['enabled'])
                self.assertTrue(self.store.subscription(1,'stripchat:alvo')['enabled'])
                self.registry.add(2,20,'alvo','stripchat')
                generation=self.store.subscription(1,'stripchat:alvo')['generation']
                await callback('send:'+generation)
                self.assertEqual(self.store.recipients('stripchat:alvo','123456'),{1:10,2:20})
                self.assertEqual(self.store.subscription(1,'stripchat:alvo')['stopped_live_id'],'')
                await message('/remove');await message('https://pt.stripchat.com/alvo')
                self.assertEqual(self.store.subscriptions(1),['alvo'])
                self.assertEqual(self.store.subscriptions(2),['stripchat:alvo'])
                for language in ('en','pt'):
                    self.store.set_language(1,language)
                    for platform in ('superlive','stripchat','kick','tiktok'):
                        await message('/add')
                        buttons=sent[-1]['reply_markup']['inline_keyboard']
                        choice=next(b['callback_data'] for row in buttons for b in row
                                    if b['callback_data'].endswith(':'+platform))
                        await callback(choice)
                        prompt=edits[-1]['text']
                        self.assertNotIn('@username',prompt)
                        if platform=='superlive':
                            self.assertIn('sharing ID' if language=='en' else 'ID de compartilhamento',prompt)
                            self.assertIn('⚠️ Live stream IDs are not accepted.' if language=='en' else '⚠️ IDs de lives não são aceitos.',prompt)
                            self.assertNotIn('•',prompt)
                        else:self.assertNotIn('shared:',prompt)
            finally:await app.shutdown()
