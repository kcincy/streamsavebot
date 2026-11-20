"""SDK real: paineis atualizaveis privados, vinculados ao dono e recuperaveis apos reinicio."""
import json
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from telegram import Update
from telegram.error import BadRequest
from telegram.request import HTTPXRequest
from telegram_bot import build_application
from test_telegram import RegistryFixture


class SummaryCommandsTests(RegistryFixture):
    async def test_estatisticas_limite_atualizacao_dono_e_conversa_privada(self):
        sent=[];edits=[];answers=[];unchanged=False
        async def request(request,url,method,request_data=None,**kwargs):
            endpoint=url.rsplit('/',1)[-1];data=request_data.parameters if request_data else {}
            if endpoint=='getMe':result={'id':123456,'is_bot':True,'first_name':'Test','username':'test_bot'}
            elif endpoint=='answerCallbackQuery':answers.append(data);result=True
            elif endpoint in ('sendMessage','editMessageText'):
                if unchanged and endpoint=='editMessageText':raise BadRequest('Message is not modified')
                (sent if endpoint=='sendMessage' else edits).append(data)
                result={'message_id':1,'date':1,'chat':{'id':data['chat_id'],'type':'private'},'text':data['text']}
            else:raise AssertionError(endpoint)
            return 200,json.dumps({'ok':True,'result':result}).encode()
        app=build_application(self.config,self.registry)
        async def command(text,user=1,kind='private'):
            message={'message_id':1,'date':1,'chat':{'id':user*10,'type':kind},
                     'from':{'id':user,'is_bot':False,'first_name':'Test'},'text':text,
                     'entities':[{'type':'bot_command','offset':0,'length':len(text.split()[0])}]}
            await app.process_update(Update.de_json({'update_id':1,'message':message},app.bot))
        async def callback(data,user=1,kind='private'):
            query={'id':'query','chat_instance':'test','from':{'id':user,'is_bot':False,'first_name':'Test'},
                   'data':data,'message':{'message_id':1,'date':1,'chat':{'id':user*10,'type':kind},'text':'panel'}}
            await app.process_update(Update.de_json({'update_id':2,'callback_query':query},app.bot))
        with patch.object(HTTPXRequest,'do_request',request):
            await app.initialize()
            try:
                await command('/stats')
                self.assertIn('<b>Tempo gravado:</b> 00:00:00',sent[-1]['text'])
                self.assertIn('0,00 GB',sent[-1]['text'])
                self.assertEqual(sent[-1]['reply_markup']['inline_keyboard'][0][0]['callback_data'],'stats:1')
                before=len(sent);await command('/limits');self.assertEqual(len(sent),before)
                await callback('limits:1');self.assertTrue(answers[-1]['show_alert'])
                await callback('limite:1');self.assertTrue(answers[-1]['show_alert'])
                await callback('stats:1');self.assertIn('Suas estatísticas',edits[-1]['text'])
                before=len(edits);await callback('limits:1',2)
                self.assertEqual(len(edits),before);self.assertTrue(answers[-1]['show_alert'])
                await callback('stats:1',1,'group')
                self.assertEqual(len(edits),before);self.assertTrue(answers[-1]['show_alert'])
                before=len(sent);await command('/stats',1,'group');self.assertEqual(len(sent),before)
                unchanged=True;await callback('limits:1');self.assertEqual(len(sent),before)
                await command('/stats extra');self.assertIn('Envie apenas /stats',sent[-1]['text'])
            finally:await app.shutdown()
