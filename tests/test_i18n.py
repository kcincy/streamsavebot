"""First-use language selection through the real SDK, without contacting Telegram."""
import ast
import json
from pathlib import Path
import string
import sys
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from telegram import Update
from telegram.request import HTTPXRequest
from telegram_bot import build_application
from i18n import PT
from storage import Store
from test_telegram import RegistryFixture


class LanguageTests(RegistryFixture):
    async def test_english_first_portuguese_persisted_owner_controls_and_all_commands(self):
        calls=[]
        async def request(request,url,method,request_data=None,**kwargs):
            name=url.rsplit('/',1)[-1];data=request_data.parameters if request_data else {}
            calls.append((name,data))
            if name=='getMe':result={'id':123456,'is_bot':True,'first_name':'Test','username':'test_bot'}
            elif name in ('sendMessage','editMessageText'):
                result={'message_id':1,'date':1,'chat':{'id':420,'type':'private'},'text':data['text']}
            elif name=='editMessageReplyMarkup':
                result={'message_id':1,'date':1,'chat':{'id':420,'type':'private'},'text':'old'}
            else:result=True
            return 200,json.dumps({'ok':True,'result':result}).encode()
        app=build_application(self.config,self.registry)
        async def command(text,uid=42):
            message={'message_id':1,'date':1,'from':{'id':uid,'is_bot':False,'first_name':'User'},
                'chat':{'id':uid*10,'type':'private'},'text':text}
            if text.startswith('/'):message['entities']=[{'type':'bot_command','offset':0,'length':len(text.split()[0])}]
            await app.process_update(Update.de_json({'update_id':1,'message':message},app.bot))
        async def choice(data,uid=42):
            await app.process_update(Update.de_json({'update_id':2,'callback_query':{
                'id':'choice','chat_instance':'test','data':data,
                'from':{'id':uid,'is_bot':False,'first_name':'User'},
                'message':{'message_id':1,'date':1,'chat':{'id':420,'type':'private'},'text':'start'}}},app.bot))
        def sent():return [data for name,data in calls if name=='sendMessage']
        with patch.object(HTTPXRequest,'do_request',request):
            await app.initialize()
            try:
                await command('/start')
                self.assertIn('Welcome!',sent()[-1]['text']);self.assertNotIn('Weekly allowance',sent()[-1]['text'])
                self.assertNotIn('Profile limit',sent()[-1]['text']);self.assertNotIn('shared:',sent()[-1]['text'])
                self.assertIn('automatically',sent()[-1]['text'])
                for name in ('SuperLive','StripChat','Kick','TikTok','/add','/monitor','/stop','/remove','/stats','/start'):
                    self.assertIn(name,sent()[-1]['text'])
                buttons=sent()[-1]['reply_markup']['inline_keyboard'][0]
                self.assertEqual([b['callback_data'] for b in buttons],['language:42:en','language:42:pt'])
                self.assertEqual([b['text'] for b in buttons],['🇺🇸 Continue in English','🇧🇷 Continuar em Português'])
                await choice('language:42:pt',43)
                self.assertEqual(self.store.language(42),'en')
                self.assertTrue(calls[-1][1]['show_alert'])
                await choice('language:42:pt')
                self.assertEqual(self.store.language(42),'pt')
                self.assertIn('Bem-vindo!',sent()[-1]['text'])
                self.assertIn('automaticamente',sent()[-1]['text'])
                self.assertNotIn('Limite',sent()[-1]['text']);self.assertNotIn('shared:',sent()[-1]['text'])
                self.assertIsNone(sent()[-1].get('reply_markup'))
                menu=[d for n,d in calls if n=='setMyCommands'][-1]
                self.assertEqual(menu['scope'],{'type':'chat','chat_id':420})
                self.assertEqual(menu['commands'][-1]['command'],'stats')
                self.assertIn('estat\u00edsticas',menu['commands'][-1]['description'])
                for cmd,expected in [('/start','Bem-vindo!'),('/monitor','Monitoramento'),('/stats','Suas estat\u00edsticas'),
                    ('/add','Em qual plataforma'),('/remove','Qual perfil'),
                    ('/stop','De qual perfil'),('/add https://bad.invalid/a','link de perfil')]:
                    await command(cmd);self.assertIn(expected,sent()[-1]['text'],cmd)
                    if cmd in ('/stop','/remove'):
                        self.assertNotIn('@username',sent()[-1]['text'])
                        self.assertIn('ID exibido no /monitor',sent()[-1]['text'])
                before=len(sent());await command('/estatisticas');self.assertEqual(len(sent()),before)
                reopened=Store(self.config.database)
                try:self.assertEqual(reopened.language(42),'pt')
                finally:reopened.close()
                await choice('language:42:en')
                self.assertIn('Welcome!',sent()[-1]['text']);self.assertEqual(self.store.language(42),'en')
                await command('/stats');self.assertIn('Your statistics',sent()[-1]['text'])
                await command('/add https://bad.invalid/a');self.assertIn('valid profile URL',sent()[-1]['text'])
                await command('/start',44);await choice('language:44:en',44)
                self.assertEqual(self.store.db.execute('SELECT language FROM users WHERE user_id=44').fetchone()[0],'en')
            finally:await app.shutdown()

    def test_translation_catalog_covers_literal_messages_and_format_parameters(self):
        root=Path(__file__).resolve().parents[1]/'src'
        source_strings=set()
        for path in root.glob('*.py'):
            if path.name=='i18n.py':continue
            tree=ast.parse(path.read_text(encoding='utf-8-sig'))
            source_strings.update(node.value for node in ast.walk(tree)
                                  if isinstance(node,ast.Constant) and isinstance(node.value,str))
            for node in ast.walk(tree):
                if not isinstance(node,ast.Call):continue
                name=node.func.id if isinstance(node.func,ast.Name) else node.func.attr if isinstance(node.func,ast.Attribute) else ''
                index=1 if name=='tr' or (name=='t' and isinstance(node.func,ast.Attribute)) else 0
                if name in ('tr','t','UserError') and len(node.args)>index:
                    argument=node.args[index]
                    if isinstance(argument,ast.Constant) and isinstance(argument.value,str):
                        self.assertIn(argument.value,PT,f'{path.name}:{node.lineno}')
        self.assertEqual(set(PT)-source_strings,set())
        formatter=string.Formatter()
        def fields(text):return {field for _,field,_,_ in formatter.parse(text) if field}
        for english,portuguese in PT.items():self.assertEqual(fields(english),fields(portuguese),english)
