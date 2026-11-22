"""MVP Telegram sem credenciais: motor falso bloqueante e requisicoes Bot API locais falsas."""
import asyncio
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import uuid
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import monitor_service as service
import recorder as engine
if importlib.util.find_spec("telegram") is None:
    raise unittest.SkipTest("Instale requirements.txt para testar Telegram.")
from telegram import Update
from telegram.error import NetworkError
from telegram.request import HTTPXRequest
from telegram_bot import Commands, Config, build_application, configure_command_menu, on_error
from dataclasses import replace
from storage import Store
from delivery_service import DeliveryService

TOKEN = "123456:TEST_ONLY_NO_NETWORK_CREDENTIAL"


class BlockingRecorder:
    instances = []
    active = 0
    peak = 0
    lock = threading.Lock()

    def __init__(self, live, outdir, *, stop_event, **kwargs):
        self.live, self.outdir, self.stop_event = live, outdir, stop_event
        self.status, self.outputs = "starting", []
        self._identity_error = ""
        self.flush_event = threading.Event()
        self.sample = dict(online=True,live_id='123456',part_id=uuid.uuid4().hex,
                           live_bytes=12345678,part_bytes=3456789,recorded_seconds=3723,part_seconds=123)
        self.started = threading.Event()
        self.instances.append(self)

    def progress(self):
        return dict(self.sample,recording=self.status=='recording')

    def request_part(self):
        self.flush_event.set()

    def run(self):
        with self.lock:
            type(self).active += 1
            type(self).peak = max(type(self).peak, type(self).active)
        self.status = "recording"
        self.started.set()
        self.stop_event.wait()
        self.status = "finalizing"
        time.sleep(.05)
        self.outputs.append(self.outdir / f"{self.live.username}.mp4")
        with self.lock:
            type(self).active -= 1
        self.status = "stopped"
        return engine.Result(self.live, self.outputs[0], 1, 100, True, paths=tuple(self.outputs))


async def until(predicate):
    async def wait():
        while not predicate():
            await asyncio.sleep(.005)
    await asyncio.wait_for(wait(), 3)


class RegistryFixture(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        BlockingRecorder.instances=[];BlockingRecorder.active=BlockingRecorder.peak=0
        self.patcher=patch('monitor_service.StreamRecorder',BlockingRecorder)
        self.patcher.start();self.addCleanup(self.patcher.stop)
        folder=Path(self.tmp.name)
        self.config=replace(Config(TOKEN),database=folder/'bot.sqlite3',spool=folder/'spool',api_files=folder/'api-files')
        self.store=Store(self.config.database)
        for uid in (1,2,3,9,10,20):
            self.store.user(uid,uid*10);self.store.set_language(uid,'pt')
        self.delivery=DeliveryService(self.config,self.store,None)
        self.registry=service.MonitorRegistry(self.config,self.store,self.delivery)
        await self.registry.start()

    async def asyncTearDown(self):
        await self.registry.close()
        self.store.close()


class RegistryTests(RegistryFixture):
    async def test_tiktok_failure_does_not_delay_other_profiles(self):
        with patch('monitor_service.StreamlinkRecorder',BlockingRecorder):
            self.registry.add(1,10,'tiktok:first');self.registry.add(1,10,'tiktok:second')
            first=self.registry.monitors['tiktok:first'].recorder
            second=self.registry.monitors['tiktok:second'].recorder
            for _ in range(5):first.capture_failure()
            self.assertGreater(first.retry_delay(15),15)
            self.assertEqual(second.retry_delay(15),15)
            first.capture_recovered()
            self.assertEqual(first.retry_delay(15),15)

    async def test_cem_monitores_nao_bloqueiam_processamento_no_executor_padrao(self):
        self.registry.config=replace(self.config,superuser_id=9)
        for index in range(100):
            self.registry.add(9,90,f'load{index:03d}')
        await until(lambda:BlockingRecorder.active==100)
        result=await asyncio.wait_for(asyncio.to_thread(lambda:'media_worker_available'),1)
        self.assertEqual(result,'media_worker_available')
        self.assertEqual(BlockingRecorder.peak,100)

    async def test_novas_plataformas_usam_streamlink_e_pausados_contam_no_limite_conjunto(self):
        with patch('monitor_service.StreamlinkRecorder',BlockingRecorder) as factory:
            self.registry.add(1,10,'https://kick.com/tester')
            self.registry.add(1,10,'https://www.tiktok.com/@tester/live')
            self.registry.add(1,10,'tester')
            self.assertEqual(set(self.registry.monitors),{'kick:tester','tiktok:tester','tester'})
            self.assertEqual(factory.instances[-2].live.username,'tester')
            self.registry.set_enabled(1,10,'kick:tester',False)
            with self.assertRaisesRegex(ValueError,'Profile limit of 3'):
                self.registry.add(1,10,'stripchat:another')
            self.assertEqual(self.store.subscriptions(1),['kick:tester','tester','tiktok:tester'])
            self.registry.set_enabled(1,10,'kick:tester',True)

    async def test_pausa_individual_persistente_e_retomada_sem_duplicar(self):
        self.registry.add(1,10,'streamer');self.registry.add(2,20,'streamer')
        monitor=self.registry.monitors['streamer']
        await until(monitor.recorder.started.is_set)
        self.registry.set_enabled(1,10,'streamer',False)
        self.assertFalse(monitor.recorder.stop_event.is_set())
        self.assertIn('pausado',self.registry.details(1,10)[0]['text'])
        self.assertEqual(self.store.subscriptions(1),['streamer'])
        self.registry.set_enabled(2,20,'streamer',False)
        await monitor.task
        self.assertEqual(self.registry.monitors,{})
        await self.registry.close()
        self.registry=service.MonitorRegistry(self.config,self.store,self.delivery)
        await self.registry.start()
        self.assertEqual(self.registry.monitors,{})
        self.registry.set_enabled(1,10,'streamer',True)
        await until(lambda:BlockingRecorder.instances[-1].started.is_set())
        self.assertEqual(self.store.recipients('streamer'),{1:10})
        self.assertEqual(BlockingRecorder.peak,1)

    async def test_stop_individual_sinaliza_corte_e_preserva_proxima_live(self):
        self.registry.add(1,10,'streamer');self.registry.add(2,20,'streamer')
        rec=self.registry.monitors['streamer'].recorder
        await until(rec.started.is_set)
        with self.assertRaises(ValueError):self.registry.stop(3,30,'streamer')
        self.assertFalse(rec.flush_event.is_set())
        self.registry.stop(1,10,'streamer')
        self.assertTrue(rec.flush_event.is_set());self.assertFalse(rec.stop_event.is_set())
        self.assertEqual(self.store.recipients('streamer','123456'),{2:20})
        self.assertEqual(self.store.recipients('streamer','123456',rec.sample['part_id']),{1:10,2:20})
        self.assertEqual(self.store.recipients('streamer','234567'),{1:10,2:20})
        self.assertIn('parada nesta live',self.registry.details(1,10)[0]['text'])
        with self.assertRaises(ValueError):self.registry.stop(1,10,'streamer')
        self.registry.set_enabled(1,10,'streamer',True)
        self.assertEqual(self.store.recipients('streamer','123456'),{1:10,2:20})

    async def test_progresso_e_formatacao_do_tempo_usam_amostra_do_gravador(self):
        self.registry.add(1,10,'streamer')
        rec=self.registry.monitors['streamer'].recorder
        await until(rec.started.is_set)
        text=self.registry.details(1,10)[0]['text']
        self.assertIn('📦 3,46 MB / 500 MB',text)
        self.assertIn('⏱ 02:03',text)
        self.assertNotIn('12,35 MB',text)
        rec.sample['part_bytes']=4_000_000
        self.assertIn('4,00 MB / 500 MB',self.registry.details(1,10)[0]['text'])
        rec.sample.update(part_bytes=0,part_seconds=99999)
        row=self.registry.details(1,10)[0]
        self.assertIn('Aguardando dados da captura',row['text'])
        self.assertNotIn('⏱',row['text']);self.assertFalse(row['recording'])
        self.assertTrue(row['can_stop'])
        self.assertEqual(rec.sample['live_bytes'],12345678)
        for seconds,expected in ((59,'00:59'),(61,'01:01'),(3600,'01:00:00'),(3661,'01:01:01')):
            self.assertEqual(service.MonitorRegistry.elapsed(seconds),expected)
    async def test_compartilhamento_isolamento_persistencia_e_reinicio(self):
        self.registry.add(1,10,'@Streamer')
        self.registry.add(2,20,'streamer')
        self.assertEqual(len(self.registry.monitors),1)
        self.assertFalse(self.registry.add(1,10,'STREAMER')[1])
        await until(lambda:BlockingRecorder.instances[0].started.is_set())
        monitor=self.registry.monitors['streamer']
        self.registry.remove(1,10,'streamer')
        self.assertFalse(monitor.recorder.stop_event.is_set())
        self.assertEqual(self.registry.list(1,10),[])
        await self.registry.close()
        self.registry=service.MonitorRegistry(self.config,self.store,self.delivery)
        await self.registry.start()
        self.assertEqual(self.registry.recipients('streamer'),{2:20})
        await until(lambda:len(BlockingRecorder.instances)==2 and BlockingRecorder.instances[-1].started.is_set())
        self.assertEqual(BlockingRecorder.peak,1)

    async def test_remove_alheio_e_entradas_invalidas(self):
        for target in ('','../foo','foo bar','https://superlivechat.tv/pt/livestream/123456','https://example.org/pt/foo','https://superlivechat.tv/'):
            with self.assertRaises(ValueError):self.registry.add(1,10,target)
        self.assertEqual(self.registry.monitors,{})
        self.registry.add(1,10,'streamer')
        with self.assertRaises(ValueError):self.registry.remove(2,20,'streamer')
        self.assertFalse(self.registry.monitors['streamer'].recorder.stop_event.is_set())

    async def test_limite_de_streamers_nao_cria_inscricao_que_nao_pode_monitorar(self):
        for i in range(self.config.max_monitors_per_user):self.registry.add(1,10,f'alvo{i}')
        with self.assertRaisesRegex(ValueError,'Profile limit'):self.registry.add(1,10,'extra')
        self.assertNotIn('extra',self.store.subscriptions(1))
        self.assertFalse(self.registry.add(1,10,'ALVO0')[1])
        self.assertTrue(self.registry.add(2,20,'alvo0')[1])
        self.registry.add(2,20,'outro1');self.registry.add(2,20,'outro2')
        with self.assertRaisesRegex(ValueError,'Paused profiles'):self.registry.add(2,20,'outro3')
        with self.assertRaises(ValueError):self.registry.add(1,10,'outro1')
        await until(lambda:BlockingRecorder.active==5)
        self.assertEqual(len(self.registry.monitors),5)
        self.store.block(1)
        with self.assertRaises(ValueError):self.registry.add(1,10,'extra')
        self.store.user(1,10)
        await self.registry.close()
        self.registry=service.MonitorRegistry(self.config,self.store,self.delivery)
        await self.registry.start()
        await until(lambda:BlockingRecorder.active==5)
        self.assertEqual(len(self.store.active_subscriptions(1)),3)
        self.assertEqual(len(self.store.active_subscriptions(2)),3)
        self.assertEqual(len(self.registry.monitors),5)

    async def test_pausados_contam_no_cadastro_e_retomada_nao_precisa_de_nova_vaga(self):
        for name in ('alvo0','alvo1','alvo2'):
            self.registry.add(1,10,name)
        self.registry.add(2,20,'alvo0')
        await until(lambda:BlockingRecorder.active==3)
        shared=self.registry.monitors['alvo0']
        self.registry.stop(1,10,'alvo1')
        with self.assertRaises(ValueError):self.registry.add(1,10,'extra')
        self.registry.set_enabled(1,10,'alvo0',False)
        with self.assertRaises(ValueError):self.registry.add(1,10,'extra')
        self.registry.set_enabled(1,10,'alvo0',True)
        self.assertEqual(self.store.subscription(1,'alvo0')['enabled'],1)
        self.assertFalse(self.registry.add(1,10,'alvo0')[1])
        self.registry.set_enabled(1,10,'alvo1',True)
        self.assertFalse(self.store.subscription(1,'alvo1')['stopped_live_id'])
        self.registry.set_enabled(1,10,'alvo0',True)
        self.assertIs(self.registry.monitors['alvo0'],shared)
        self.assertFalse(shared.recorder.stop_event.is_set())
        self.assertEqual(len(self.store.active_subscriptions(1)),3)
        self.assertEqual(self.store.active_subscriptions(2),['alvo0'])

    async def test_reinscricao_aguarda_worker_anterior(self):
        self.registry.add(1,10,'streamer')
        old=self.registry.monitors['streamer']
        await until(old.recorder.started.is_set)
        self.registry.remove(1,10,'streamer')
        self.registry.add(2,20,'streamer')
        self.assertIs(self.registry.monitors['streamer'],old)
        await until(lambda:self.registry.monitors['streamer'] is not old)
        await until(lambda:BlockingRecorder.instances[-1].started.is_set())
        self.assertEqual(BlockingRecorder.peak,1)

    async def test_loop_nao_bloqueia_e_shutdown_aguarda(self):
        self.registry.add(1,10,'streamer')
        monitor=self.registry.monitors['streamer']
        await until(monitor.recorder.started.is_set)
        for _ in range(5):
            await asyncio.sleep(.001)
            self.assertIn('🔴 Gravando',self.registry.list(1,10)[0][1])
        await self.registry.close()
        self.assertTrue(monitor.result.ok)
        self.assertEqual(BlockingRecorder.active,0)
        self.assertEqual(self.store.subscriptions(1),['streamer'])


class HandlerTests(RegistryFixture):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        guard=patch('traffic_control.TrafficGuard.allowed',return_value=True)
        guard.start();self.addCleanup(guard.stop)
    async def test_dialogos_isolados_validacao_troca_cancelamento_e_acoes_originais(self):
        sent=[]
        async def request(request,url,method,request_data=None,**kwargs):
            endpoint=url.rsplit('/',1)[-1]
            if endpoint=='getMe':
                result={'id':123456,'is_bot':True,'first_name':'Test','username':'test_bot'}
            elif endpoint=='sendMessage':
                data=request_data.parameters;sent.append(data)
                result={'message_id':len(sent),'date':1,'chat':{'id':data['chat_id'],'type':'private'},'text':data['text']}
            elif endpoint=='answerCallbackQuery':result=True
            elif endpoint=='editMessageText':
                data=request_data.parameters
                result={'message_id':1,'date':1,'chat':{'id':data['chat_id'],'type':'private'},'text':data['text']}
            else:raise AssertionError(endpoint)
            return 200,json.dumps({'ok':True,'result':result}).encode()
        app=build_application(self.config,self.registry)
        counter=0
        async def message(text,user=1,chat=10,kind='private'):
            nonlocal counter
            counter+=1
            data={'update_id':counter,'message':{'message_id':counter,'date':1,
                  'from':{'id':user,'is_bot':False,'first_name':'Test'},
                  'chat':{'id':chat,'type':kind},'text':text,
                  'entities':[{'type':'bot_command','offset':0,'length':len(text.split()[0])}] if text.startswith('/') else []}}
            await app.process_update(Update.de_json(data,app.bot))
            if text=='/add' and kind=='private':
                nonce=app.user_data[user]['platform_choice']
                callback={'update_id':counter+10000,'callback_query':{'id':'choice','chat_instance':'test',
                    'from':{'id':user,'is_bot':False,'first_name':'Test'},'data':f'platform:{nonce}:superlive',
                    'message':{'message_id':counter,'date':1,'chat':{'id':chat,'type':'private'},'text':'platform'}}}
                await app.process_update(Update.de_json(callback,app.bot))
        with patch.object(HTTPXRequest,'do_request',request):
            await app.initialize()
            try:
                await message('sem comando')
                self.assertFalse(sent)
                await message('/add');await message('/add',2,20)
                self.assertEqual([r['chat_id'] for r in sent],[10,20])
                self.assertTrue(all('Em qual plataforma' in r['text'] for r in sent))
                self.assertFalse(self.registry.monitors)
                before=len(sent)
                await message('/remove',chat=-100,kind='group')
                await message('alvo',chat=-100,kind='group')
                await message('alvo',chat=999)
                self.assertEqual(len(sent),before)
                await message('https://example.org/perfil')
                self.assertIn('link de perfil',sent[-1]['text'])
                self.assertFalse(self.store.subscriptions(1))
                await message('https://superlivechat.tv/pt/Alvo',2,20)
                self.assertEqual(sent[-1]['chat_id'],20)
                await message(' @ALVO ')
                self.assertTrue(sent[-1]['text'].startswith('✅ '))
                self.assertNotIn('Status',sent[-1]['text'])
                self.assertEqual(self.store.subscriptions(1),['alvo'])
                self.assertEqual(self.store.subscriptions(2),['alvo'])
                self.assertEqual(len(self.registry.monitors),1)
                monitor=self.registry.monitors['alvo'];await until(monitor.recorder.started.is_set)
                before=len(sent);await message('outro')
                self.assertEqual(len(sent),before)
                for name in ('outro1','outro2'):
                    await message('/add');await message(name)
                await message('/add');await message('extra')
                self.assertIn('Limite de 3',sent[-1]['text'])
                self.assertNotIn('extra',self.store.subscriptions(1))
                await message('/monitor')
                before=len(sent);await message('extra')
                self.assertEqual(len(sent),before)
                await message('/remove')
                await message('/start')
                before=len(sent);await message('alvo')
                self.assertEqual(len(sent),before)
                self.assertIn('alvo',self.store.subscriptions(1))
                await message('/remove')
                await message('/stop')
                self.assertIn('enviar a parte atual e continuar gravando',sent[-1]['text'])
                await message('alheio')
                self.assertIn('não está na sua lista',sent[-1]['text'])
                self.assertFalse(monitor.recorder.flush_event.is_set())
                await message('alvo')
                self.assertIn('A gravação e o monitoramento continuam.',sent[-1]['text'])
                self.assertTrue(monitor.recorder.flush_event.is_set())
                self.assertEqual(self.store.recipients('alvo','123456'),{1:10,2:20})
                self.assertEqual(self.store.subscription(1,'alvo')['stopped_live_id'],'')
                self.assertTrue(self.store.subscription(1,'alvo')['enabled'])
                self.assertFalse(monitor.recorder.stop_event.is_set())
                await message('/remove');await message('alheio')
                self.assertIn('não está na sua lista',sent[-1]['text'])
                await message('alvo')
                self.assertEqual(sent[-1]['text'],'✅ Sua inscrição em alvo · SuperLive foi removida.')
                self.assertNotIn('alvo',self.store.subscriptions(1))
                self.assertEqual(self.store.subscriptions(2),['alvo'])
                self.assertFalse(monitor.recorder.stop_event.is_set())
            finally:await app.shutdown()

    async def test_painel_segue_modelo_e_conta_apenas_inscricoes_do_usuario(self):
        for name in ('luckzinha_x','sassyadri','vampira'):
            self.registry.add(1,10,name)
        self.registry.add(2,20,'alheio')
        await until(lambda:all(m.recorder.started.is_set() for m in self.registry.monitors.values()))
        self.registry.monitors['luckzinha_x'].recorder.sample.update(part_bytes=2_550_000,part_seconds=13)
        for name in ('sassyadri','vampira'):
            rec=self.registry.monitors[name].recorder
            rec.sample['online']=False
            rec.status='offline'
        with patch('telegram_bot.datetime') as clock:
            clock.now.return_value.strftime.return_value='20:09'
            pages=Commands(self.registry).monitor_pages(1,10)
        self.assertEqual(len(pages),1)
        text,markup,_=pages[0]
        self.assertEqual(text,
            '📡 <b>Monitoramento</b>\n\n'
            '🟢 Ao vivo: 1\n⚫ Offline: 2\n👁 Monitorando: 3/3\n🎥 Gravações ativas: 1\n\n'
            '🟢 <b>luckzinha_x · SuperLive</b>\n└ 🔴 Gravando · Envio por partes\n└ ⏱ 00:13 | 📦 2,55 MB / 500 MB\n\n'
            '⚫ <b>sassyadri · SuperLive</b>\n└ 👁 Offline · Aguardando nova live\n\n'
            '⚫ <b>vampira · SuperLive</b>\n└ 👁 Offline · Aguardando nova live\n\n'
            '🕒 Atualizado às 20:09')
        self.assertNotIn('━',text)
        self.assertEqual([[b.text for b in row] for row in markup.inline_keyboard],[
            ['⏸️ Pausar luckzinha_x · SuperLive','🗑 Remover'],
            ['⏸️ Pausar sassyadri · SuperLive','🗑 Remover'],
            ['⏸️ Pausar vampira · SuperLive','🗑 Remover'],['🔄 Atualizar status']])
        self.assertEqual(markup.inline_keyboard[0][1].callback_data,
                         'remove:'+self.store.subscription(1,'luckzinha_x')['generation'])

    async def test_painel_vazio_e_lista_grande_paginada_com_pausas(self):
        commands=Commands(self.registry)
        text,markup,_=commands.monitor_pages(1,10)[0]
        self.assertIn('👁 Monitorando: 0/3\n🎥 Gravações ativas: 0',text)
        self.assertEqual(text.count('Slot vazio'),3)
        self.assertEqual(markup.inline_keyboard[0][0].callback_data,'refresh:0')
        generations=set()
        for i in range(120):
            name=f'perfil_{i:03d}_'+48*'x'
            self.store.add(1,10,name)
            self.store.set_enabled(1,name,False)
            generations.add(self.store.subscription(1,name)['generation'])
        pages=commands.monitor_pages(1,10)
        self.assertGreater(len(pages),1)
        self.assertEqual(set.union(*(p[2] for p in pages)),generations)
        for index,(text,markup,owned) in enumerate(pages):
            self.assertLessEqual(len(text.encode('utf-16-le'))//2,4096)
            self.assertIn('👁 Monitorando: 0/120\n🎥 Gravações ativas: 0',text)
            self.assertIn('❔ Sem status: 120',text)
            self.assertIn('└ ⏸️ Monitoramento pausado',text)
            self.assertIn(f'Página {index+1}/{len(pages)}',text)
            self.assertEqual(markup.inline_keyboard[-1][0].callback_data,f'refresh:{index}')
            self.assertEqual({row[0].callback_data for row in markup.inline_keyboard[:-1]},
                             {'resume:'+g for g in owned})
            self.assertTrue(all([b.callback_data.split(':')[0] for b in row]==['resume','remove']
                                for row in markup.inline_keyboard[:-1]))
        self.assertFalse(self.registry.monitors)

    async def test_botoes_privados_isolados_e_comando_stop(self):
        sent=[];edits=[];answers=[]
        async def request(request,url,method,request_data=None,**kwargs):
            endpoint=url.rsplit('/',1)[-1]
            data=request_data.parameters if request_data else {}
            if endpoint=='getMe':result={'id':123456,'is_bot':True,'first_name':'Test','username':'test_bot'}
            elif endpoint=='answerCallbackQuery':
                answers.append(data);result=True
            elif endpoint in ('sendMessage','editMessageText'):
                (sent if endpoint=='sendMessage' else edits).append(data)
                result={'message_id':1,'date':1,'chat':{'id':data['chat_id'],'type':'private'},'text':data['text']}
            else:raise AssertionError(endpoint)
            return 200,json.dumps({'ok':True,'result':result}).encode()
        app=build_application(self.config,self.registry)
        self.registry.add(1,10,'streamer');self.registry.add(2,20,'streamer')
        monitor=self.registry.monitors['streamer'];await until(monitor.recorder.started.is_set)
        generation=self.store.subscription(1,'streamer')['generation']
        async def callback(data,user=1,chat=10,kind='private'):
            update={'update_id':1,'callback_query':{'id':'q1','chat_instance':'test','from':{'id':user,'is_bot':False,'first_name':'User'},
                'data':data,'message':{'message_id':1,'date':1,'chat':{'id':chat,'type':kind},'text':'monitor'}}}
            await app.process_update(Update.de_json(update,app.bot))
        with patch.object(HTTPXRequest,'do_request',request):
            await app.initialize()
            try:
                await callback('pause:'+generation,user=2,chat=20)
                self.assertEqual(self.store.subscription(1,'streamer')['enabled'],1)
                self.assertTrue(answers[-1]['show_alert'])
                await callback('pause:'+generation,chat=-100,kind='group')
                self.assertEqual(self.store.subscription(1,'streamer')['enabled'],1)
                await callback('pause:'+generation)
                self.assertIn('pausado',edits[-1]['text'])
                self.assertIn('👁 Monitorando: 0/3\n🎥 Gravações ativas: 0',edits[-1]['text'])
                self.assertFalse(monitor.recorder.stop_event.is_set())
                await callback('resume:'+generation)
                self.assertEqual(self.store.subscription(1,'streamer')['enabled'],1)
                await callback('stop:'+generation)
                self.assertNotIn('parada nesta live',edits[-1]['text'])
                self.assertIn('👁 Monitorando: 1/3\n🎥 Gravações ativas: 1',edits[-1]['text'])
                self.assertIn('📦',edits[-1]['text'])
                self.assertEqual(self.store.subscription(1,'streamer')['stopped_live_id'],'')
                self.assertTrue(monitor.recorder.flush_event.is_set())
                await callback('refresh:0')
                self.assertIn('Atualizar',str(edits[-1]['reply_markup']))
                self.registry.remove(1,10,'streamer')
                self.registry.add(1,10,'streamer')
                await callback('pause:'+generation)
                self.assertTrue(answers[-1]['show_alert'])
                self.assertEqual(self.store.subscription(1,'streamer')['enabled'],1)
            finally:await app.shutdown()

    async def test_dispatch_real_de_quatro_comandos_e_filtro_de_chat(self):
        sent = []
        async def request(request, url, method, request_data=None, **kwargs):
            endpoint = url.rsplit("/", 1)[-1]
            if endpoint == "getMe":
                result = {"id": 123456, "is_bot": True, "first_name": "Test", "username": "local_test_bot"}
            elif endpoint == "sendMessage":
                params = request_data.parameters
                sent.append((params["chat_id"], params["text"]))
                result = {"message_id": len(sent), "date": 1,
                          "chat": {"id": params["chat_id"], "type": "private"}, "text": params["text"]}
            else:
                raise AssertionError(endpoint)
            return 200, json.dumps({"ok": True, "result": result}).encode()

        app = build_application(Config(TOKEN), self.registry)
        async def command(text, user_id=10, chat_id=900, chat_type="private"):
            data = {"update_id": len(sent) + 1, "message": {"message_id": 1, "date": 1,
                    "from": {"id": user_id, "is_bot": False, "first_name": "User"},
                    "chat": {"id": chat_id, "type": chat_type}, "text": text,
                    "entities": [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]}}
            await app.process_update(Update.de_json(data, app.bot))

        with patch.object(HTTPXRequest, "do_request", request):
            await app.initialize()
            try:
                await command("/start")
                self.assertIn("/monitor", sent[-1][1])
                await command("/add")
                self.assertIn("Em qual plataforma", sent[-1][1])
                await command("/add @Streamer")
                await command("/add streamer", 20, 800)
                await command("/add outro", 20, 800)
                await command("/add STREAMER")
                self.assertIn('já está na sua lista',sent[-1][1])
                await command("/monitor")
                self.assertEqual(sent[-1][0], 900)
                self.assertIn('<b>streamer · SuperLive</b>\n',sent[-1][1])
                self.assertNotIn("outro", sent[-1][1])
                before = len(sent)
                await command("/add proibido", 30, -100, "group")
                self.assertEqual(len(sent), before)
                self.assertNotIn("proibido", self.registry.monitors)
                await command("/remove outro")
                self.assertIn("sua lista", sent[-1][1])
                await command("/remove streamer")
                self.assertIn("removida", sent[-1][1])
                self.assertFalse(self.registry.monitors["streamer"].recorder.stop_event.is_set())
                await command("/monitor")
                self.assertIn("Slot vazio", sent[-1][1])
                self.assertEqual(len(BlockingRecorder.instances), 2)
            finally:
                await self.registry.close()
                await app.shutdown()

    async def test_logs_de_erro_nao_incluem_url_com_token(self):
        with self.assertLogs("telegram_bot", level="ERROR") as captured:
            await on_error(None, SimpleNamespace(error=NetworkError("https://api.telegram.org/bot" + TOKEN)))
        self.assertNotIn(TOKEN, "\n".join(captured.output))
        self.assertIn("NetworkError", "\n".join(captured.output))


class MenuTests(unittest.IsolatedAsyncioTestCase):
    async def test_menu_tem_todos_os_comandos_e_descricoes_sem_parametros(self):
        bot=SimpleNamespace(set_my_commands=AsyncMock(return_value=True))
        await configure_command_menu(bot)
        commands=bot.set_my_commands.call_args.args[0]
        self.assertEqual([name for name,_ in commands],['start','add','remove','stop','monitor','stats'])
        self.assertTrue(all(1<=len(description)<=256 for _,description in commands))
        self.assertEqual(bot.set_my_commands.call_args.kwargs['scope'].type,'all_private_chats')

    async def test_falha_no_menu_nao_interrompe_bot_nem_expoe_token(self):
        bot=SimpleNamespace(set_my_commands=AsyncMock(side_effect=NetworkError(TOKEN)))
        with self.assertLogs('telegram_bot',level='WARNING') as captured:
            await configure_command_menu(bot)
        self.assertIn('NetworkError',' '.join(captured.output))
        self.assertNotIn(TOKEN,' '.join(captured.output))


class ConfigTests(unittest.TestCase):
    def test_configuracoes_separadas_das_credenciais_e_prioridade_do_ambiente(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            (root/'session.json').write_text('{}',encoding='utf-8')
            (root/'settings.env').write_text(
                'MAX_MONITORS_PER_USER=2\nTELEGRAM_API_MODE=local\n'
                'TELEGRAM_API_URL=http://127.0.0.1:8099\nSPOOL_MAX_MB=3000\n'
                'TEMP_RETENTION_SECONDS=120\nSUPERLIVE_STATE=session.json\n'
                'TELEGRAM_BOT_TOKEN=SHOULD_NOT_BE_USED\n',encoding='utf-8')
            with patch('config.ROOT',root),patch.dict('os.environ',{'TELEGRAM_BOT_TOKEN':TOKEN},clear=True):
                config=Config.from_env()
                self.assertEqual(config.max_monitors_per_user,2)
                self.assertEqual(config.token,TOKEN)
                self.assertEqual(config.api_url,'http://127.0.0.1:8099')
                self.assertEqual(config.spool_bytes,3000*1024*1024)
                self.assertEqual(config.retention_seconds,120)
                self.assertEqual(config.state,str(root/'session.json'))
                with patch.dict('os.environ',{'MAX_MONITORS_PER_USER':'3'}):
                    self.assertEqual(Config.from_env().max_monitors_per_user,3)
                with patch.dict('os.environ',{},clear=True):
                    with self.assertRaisesRegex(ValueError,'TELEGRAM_BOT_TOKEN'):Config.from_env()

    def test_api_local_publica_limites_e_credenciais(self):
        for mode in ('local','public'):
            with patch.dict('os.environ',{'TELEGRAM_BOT_TOKEN':TOKEN,'TELEGRAM_API_MODE':mode,
                    'PART_MAX_MB':'500' if mode=='local' else '45'},clear=True):
                config=Config.from_env()
            bot=build_application(config).bot
            self.assertEqual(bot.local_mode,mode=='local')
            self.assertNotIn(TOKEN,repr(config))
            self.assertEqual(config.segment_seconds,0)
            self.assertEqual(config.max_monitors_per_user,3)
            self.assertEqual(config.part_bytes,500_000_000 if mode=='local' else 45_000_000)
        for values in ({'TELEGRAM_API_URL':'http://secret@localhost:8081'},
                       {'MAX_MONITORS_PER_USER':'0'},{'MAX_MONITORS_PER_USER':'abc'},
                       {'MAX_MONITORS_PER_USER':'33'},{'SPOOL_MAX_MB':'96'},
                       {'PART_MAX_MB':'16'},{'PART_MAX_MB':'501'},
                       {'TELEGRAM_API_MODE':'public','PART_MAX_MB':'46'}):
            with patch.dict('os.environ',{'TELEGRAM_BOT_TOKEN':TOKEN,**values},clear=True):
                with self.assertRaises(ValueError) as error:Config.from_env()
                self.assertNotIn(TOKEN,str(error.exception))

    def test_spool_comporta_cem_capturas_sem_alterar_qualidade(self):
        with patch.dict('os.environ',{'TELEGRAM_BOT_TOKEN':TOKEN,'TELEGRAM_API_MODE':'local',
                'PART_MAX_MB':'128','SPOOL_MAX_MB':'16384'},clear=True):
            config=Config.from_env()
        self.assertEqual(config.part_bytes,128_000_000)
        self.assertGreaterEqual(config.spool_slots,100)
        self.assertEqual(config.spool_slots,122)


class ConflictTests(unittest.IsolatedAsyncioTestCase):
    async def test_conflict_encerra_polling_em_vez_de_repetir(self):
        from telegram.error import Conflict
        for message in ('polling terminated','getUpdates cannot run while webhook is active'):
            stop=asyncio.Event()
            app=SimpleNamespace(bot_data={'stop_event':stop})
            with self.assertLogs('telegram_bot',level='CRITICAL') as output:
                await on_error(None,SimpleNamespace(error=Conflict(message),application=app))
            self.assertTrue(stop.is_set())
            self.assertIn('Conflict',app.bot_data['fatal_error'])
            if 'webhook' in message:self.assertIn('webhook',' '.join(output.output))
