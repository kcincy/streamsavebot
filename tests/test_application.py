"""Entrada unica com polling SDK real e respostas Bot API sem credenciais."""
import asyncio
from dataclasses import replace
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1] / "src"))
import application as main
from config import Config
from storage import Store
from telegram.request import HTTPXRequest
from test_telegram import BlockingRecorder,TOKEN


class ApplicationTests(unittest.IsolatedAsyncioTestCase):
    async def test_uma_entrada_remove_webhook_processa_comandos_e_fecha_servicos(self):
        stop=asyncio.Event();events=[];sent=[]
        updates=[]
        for i,text in enumerate(('/start','/add'),1):
            updates.append({'update_id':i,'message':{'message_id':i,'date':1,
                'from':{'id':10,'is_bot':False,'first_name':'Test'},'chat':{'id':900,'type':'private'},
                'text':text,'entities':[{'type':'bot_command','offset':0,'length':len(text.split()[0])}] if text.startswith('/') else []}})
        class API:
            process=None
            def __init__(self,config):pass
            async def start(self):events.append('api_start')
            async def close(self):events.append('api_close')
        async def request(request,url,method,request_data=None,**kwargs):
            endpoint=url.rsplit('/',1)[-1]
            if endpoint=='getMe':result={'id':123456,'is_bot':True,'first_name':'Test','username':'local_test_bot'}
            elif endpoint=='setMyCommands':
                self.assertEqual([c['command'] for c in request_data.parameters['commands']],
                                 ['start','add','remove','stop','monitor','stats'])
                self.assertIn(request_data.parameters['scope'],[{'type':'all_private_chats'},{'type':'chat','chat_id':990}])
                events.append('set_commands');result=True
            elif endpoint=='getWebhookInfo':result={'url':'https://previous.invalid/hook','has_custom_certificate':False,'pending_update_count':4}
            elif endpoint=='deleteWebhook':
                self.assertFalse(request_data.parameters.get('drop_pending_updates',False))
                events.append('delete_webhook');result=True
            elif endpoint=='getUpdates':
                await asyncio.sleep(.01);result=updates[:];updates.clear()
            elif endpoint=='sendMessage':
                data=request_data.parameters;sent.append(data['text'])
                if 'reply_markup' in data and 'Which platform' in data['text']:
                    choice=data['reply_markup']['inline_keyboard'][0][0]['callback_data']
                    updates.append({'update_id':3,'callback_query':{'id':'choice','chat_instance':'test',
                        'from':{'id':10,'is_bot':False,'first_name':'Test'},'data':choice,
                        'message':{'message_id':2,'date':1,'chat':{'id':900,'type':'private'},'text':'platform'}}})
                    for i,text in enumerate(('alvo','/monitor','/remove','alvo'),4):
                        updates.append({'update_id':i,'message':{'message_id':i,'date':1,
                            'from':{'id':10,'is_bot':False,'first_name':'Test'},'chat':{'id':900,'type':'private'},
                            'text':text,'entities':[{'type':'bot_command','offset':0,'length':len(text)}] if text.startswith('/') else []}})
                result={'message_id':len(sent),'date':1,'chat':{'id':900,'type':'private'},'text':data['text']}
                if 'was removed' in data['text']:stop.set()
            elif endpoint=='answerCallbackQuery':result=True
            elif endpoint=='editMessageText':
                data=request_data.parameters
                result={'message_id':2,'date':1,'chat':{'id':900,'type':'private'},'text':data['text']}
            else:raise AssertionError(endpoint)
            return 200,json.dumps({'ok':True,'result':result}).encode()
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);config=replace(Config(TOKEN),database=root/'bot.sqlite3',spool=root/'spool',api_files=root/'files')
            seed=Store(config.database);seed.user(99,990);seed.set_language(99,'pt');seed.close()
            with patch('application.LocalAPI',API),patch('monitor_service.StreamRecorder',BlockingRecorder),patch.object(HTTPXRequest,'do_request',request):
                await asyncio.wait_for(main.run(config,stop),10)
            self.assertEqual(events[0],'api_start');self.assertEqual(events[-1],'api_close')
            self.assertIn('delete_webhook',events);self.assertEqual(len(sent),6)
            self.assertEqual(events.count('set_commands'),2)
            self.assertIn('Which platform',sent[1]);self.assertIn('Which profile',sent[4])
            self.assertIn('<b>alvo · SuperLive</b>\n',sent[3])
            store=Store(config.database)
            try:self.assertEqual(store.subscriptions(10),[])
            finally:store.close()
