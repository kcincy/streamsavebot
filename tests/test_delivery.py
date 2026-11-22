"""Fila, reinicio, isolamento, falhas de upload e buffer limitado, sem credenciais."""
import asyncio
from dataclasses import replace
import json
import logging
import os
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
import uuid

sys.path.insert(0,str(Path(__file__).resolve().parents[1] / "src"))
from config import Config
from delivery_service import DeliveryService,SpoolBudget,process_clip,run_media,PartTooLarge,prepare_videos,video_info
from logging_setup import SafeFormatter
from process_control import InstanceGuard
from storage import Store
from telegram.error import Forbidden, NetworkError, RetryAfter, TimedOut


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        folder=Path(self.tmp.name)
        self.config=replace(Config('123456:TEST_ONLY_NO_NETWORK_CREDENTIAL'),database=folder/'bot.sqlite3',spool=folder/'spool',api_files=folder/'api-files')
        self.store=Store(self.config.database)
        for uid in (1,2):
            self.store.user(uid,uid*10);self.store.set_language(uid,'pt')
        self.addCleanup(self.store.close)
        self.bot=SimpleNamespace(send_video=AsyncMock(),send_document=AsyncMock())
        self.pipeline=DeliveryService(self.config,self.store,self.bot)

    def clip(self,users=(1,2),started=1):
        for uid in users:self.store.add(uid,uid*10,'alvo')
        cid=uuid.uuid4().hex;folder=self.pipeline._folder(cid);folder.mkdir()
        raw=folder/'capture.mkv';raw.write_bytes(b'test')
        self.store.register_clip(cid,'alvo','123456',started,raw,{},3600)
        output=folder/'media.mp4';output.write_bytes(b'complete')
        self.store.processed(cid,[(output,'video')],{str(output):{'duration':10,'duration_seconds':10.0}})
        self.pipeline.budget.ids.add(cid)
        return cid,output

    async def test_queued_count_includes_raw_and_processing_without_double_counting_ready(self):
        self.store.add(1,10,'alvo')
        cid=uuid.uuid4().hex;folder=self.pipeline._folder(cid);folder.mkdir()
        raw=folder/'capture.mp4';raw.write_bytes(b'raw')
        self.store.register_clip(cid,'alvo','live',1,raw,{},3600)
        for status in ('raw','processing'):
            self.store.set_clip(cid,status)
            self.assertEqual(self.store.delivery_status(1,'alvo').get('pending'),1)
            self.assertEqual(self.store.statistics(1)['pending'],1)
        self.store.processed(cid,[(raw,'video')],{str(raw):{'duration_seconds':10}})
        self.assertEqual(self.store.delivery_status(1,'alvo').get('pending'),1)
        self.assertEqual(self.store.statistics(1)['pending'],1)
        self.assertNotIn('pending',self.store.delivery_status(2,'alvo'))

    async def test_um_upload_file_id_demais_destinatarios_e_limpeza(self):
        cid,path=self.clip()
        self.store.fix_identity('alvo','111')
        self.bot.send_video.return_value=SimpleNamespace(message_id=100,video=SimpleNamespace(file_id='FILE'))
        row=self.store.next_delivery();await self.pipeline.send(row)
        self.assertFalse(path.exists())
        row=self.store.next_delivery();self.assertEqual(row['file_id'],'FILE');await self.pipeline.send(row)
        self.assertEqual(self.bot.send_video.call_args.kwargs['video'],'FILE')
        self.assertIsInstance(self.bot.send_video.call_args_list[0].kwargs['video'],Path)
        self.assertEqual(self.bot.send_video.call_args_list[0].kwargs['read_timeout'],1800)
        self.assertEqual(self.bot.send_video.call_args.kwargs['read_timeout'],180)
        await self.pipeline.cleanup();self.assertFalse(path.parent.exists())
        self.assertFalse(self.pipeline.budget.ids)
        self.assertEqual(self.store.delivery_status(1,'alvo'),{'sent':1})
        self.assertNotIn('duration_seconds',self.bot.send_video.call_args.kwargs)
        for call in self.bot.send_video.call_args_list:
            self.assertEqual(call.kwargs['parse_mode'],'HTML')
            self.assertIn('<code>@alvo</code>',call.kwargs['caption'])
            self.assertIn('Plataforma: SuperLive\nID do perfil: 111',call.kwargs['caption'])
            self.assertIn('Início da gravação:',call.kwargs['caption'])
            self.assertIn('Duração: 00:00:10',call.kwargs['caption'])
            self.assertNotIn('Parte',call.kwargs['caption'])

    async def test_legendas_seguem_idioma_do_destinatario_inclusive_file_id_reutilizado(self):
        self.clip()
        self.store.set_language(1,'en')
        self.store.fix_identity('alvo','111')
        self.bot.send_video.return_value=SimpleNamespace(message_id=100,video=SimpleNamespace(file_id='FILE'))
        await self.pipeline.send(self.store.next_delivery())
        await self.pipeline.send(self.store.next_delivery())
        first,second=self.bot.send_video.call_args_list
        self.assertIn('Platform: SuperLive\nProfile ID: 111',first.kwargs['caption'])
        self.assertIn('Duration: 00:00:10',first.kwargs['caption'])
        self.assertEqual(second.kwargs['video'],'FILE')
        self.assertIn('Plataforma: SuperLive\nID do perfil: 111',second.kwargs['caption'])

    async def test_menos_de_dez_segundos_nao_envia_nem_reutiliza_e_limpa_para_todos(self):
        cid,path=self.clip()
        with self.store.db:
            self.store.db.execute('UPDATE media SET info=?,file_id=? WHERE clip_id=?',
                (json.dumps({'duration':10,'duration_seconds':9.999}),'EXISTING_FILE',cid))
        for _ in range(2):await self.pipeline.send(self.store.next_delivery())
        self.bot.send_video.assert_not_called();self.bot.send_document.assert_not_called()
        self.assertIsNone(self.store.next_delivery())
        self.assertEqual(self.store.delivery_status(1,'alvo'),{'skipped':1})
        self.assertTrue(path.exists(),'limpeza aguarda todos os destinatarios serem avaliados')
        await self.pipeline.cleanup()
        self.assertFalse(path.parent.exists());self.assertNotIn(cid,self.pipeline.budget.ids)

    async def test_fila_antiga_confere_duracao_real_sem_arredondar_antes_de_enviar(self):
        cid,path=self.clip(users=(1,))
        with self.store.db:self.store.db.execute('UPDATE media SET info=? WHERE clip_id=?',(json.dumps({'duration':10}),cid))
        with patch('delivery_service.media_duration',return_value=9.99) as probe:
            await self.pipeline.send(self.store.next_delivery())
        probe.assert_called_once_with(path)
        self.bot.send_video.assert_not_called()
        self.assertEqual(self.store.delivery_status(1,'alvo'),{'skipped':1})
        await self.pipeline.cleanup();self.assertFalse(path.parent.exists())

    async def test_duracao_desconhecida_preserva_arquivo_sem_enviar(self):
        cid,path=self.clip(users=(1,))
        with self.store.db:self.store.db.execute("UPDATE media SET info='{}' WHERE clip_id=?",(cid,))
        with patch('delivery_service.media_duration',side_effect=RuntimeError('probe_failed')):
            await self.pipeline.send(self.store.next_delivery())
        self.bot.send_video.assert_not_called()
        self.assertEqual(self.store.delivery_status(1,'alvo'),{'uncertain':1})
        await self.pipeline.cleanup();self.assertTrue(path.exists())

    async def test_frame_sem_duracao_e_ignorado_sem_virar_falha(self):
        cid,path=self.clip(users=(1,))
        with self.store.db:
            self.store.db.execute('UPDATE media SET info=? WHERE clip_id=?',
                (json.dumps({'duration':1,'duration_seconds':0.0}),cid))
        await self.pipeline.send(self.store.next_delivery())
        self.bot.send_video.assert_not_called();self.bot.send_document.assert_not_called()
        self.assertEqual(self.store.delivery_status(1,'alvo'),{'skipped':1})
        await self.pipeline.cleanup();self.assertFalse(path.parent.exists())

    async def test_limite_de_dez_segundos_com_mp4_e_ffprobe_reais(self):
        from project_runtime import media_tool
        for seconds in (9.8,10):
            with self.subTest(seconds=seconds):
                cid,path=self.clip(users=(1,))
                await asyncio.to_thread(run_media,[media_tool('ffmpeg'),'-v','error','-nostdin','-y',
                    '-f','lavfi','-i','testsrc2=size=160x120:rate=10','-t',str(seconds),
                    '-c:v','libx264','-threads','2','-preset','ultrafast',str(path)])
                outputs,info=await asyncio.to_thread(prepare_videos,[(path,'video')],self.config.part_bytes)
                final=outputs[0][0]
                self.assertAlmostEqual(info[str(final)]['duration_seconds'],seconds,places=2)
                self.assertEqual(info[str(final)]['duration'],10)
                self.store.video_metadata(f'{cid}:0',final,info[str(final)])
                self.bot.send_video.reset_mock()
                self.bot.send_video.return_value=SimpleNamespace(message_id=100,video=SimpleNamespace(file_id='FILE'))
                await self.pipeline.send(self.store.next_delivery())
                if seconds<10:self.bot.send_video.assert_not_called()
                else:self.bot.send_video.assert_called_once()
                await self.pipeline.cleanup();self.assertFalse(path.parent.exists())

    async def test_remove_readd_nao_recebe_fila_da_inscricao_anterior(self):
        self.clip()
        self.store.remove(1,'alvo');self.store.add(1,10,'alvo')
        row=self.store.next_delivery();self.assertEqual(row['user_id'],2)
        self.assertEqual(self.store.delivery_status(1,'alvo'),{'cancelled':1})

    async def test_retryafter_preserva_ordem_e_nao_bloqueia_outro_usuario(self):
        self.clip(started=1);self.clip(started=2)
        self.bot.send_video.side_effect=RetryAfter(5)
        row=self.store.next_delivery();await self.pipeline.send(row)
        self.assertEqual(self.store.delivery_status(1,'alvo')['retry'],1)
        row=self.store.next_delivery();self.assertEqual(row['user_id'],2);self.assertEqual(row['started'],1)

    async def test_bloqueio_cancela_so_os_envios_daquele_usuario(self):
        self.clip()
        self.bot.send_video.side_effect=Forbidden('blocked')
        await self.pipeline.send(self.store.next_delivery())
        self.assertEqual(self.store.recipients('alvo'),{2:20})
        self.assertEqual(self.store.next_delivery()['user_id'],2)

    async def test_timeout_ambiguo_nao_duplica_e_reinicio_preserva_estado(self):
        cid,_=self.clip()
        self.bot.send_video.side_effect=TimedOut()
        await self.pipeline.send(self.store.next_delivery())
        row=self.store.next_delivery();self.store.sending(row)
        self.store.recover()
        self.assertIsNone(self.store.next_delivery())
        self.assertEqual(self.store.delivery_status(1,'alvo'),{'uncertain':1})
        self.assertEqual(self.store.delivery_status(2,'alvo'),{'uncertain':1})
        await self.pipeline.cleanup();self.assertTrue(self.pipeline._folder(cid).exists())
        with self.store.db:self.store.db.execute('UPDATE clips SET expires=0 WHERE id=?',(cid,))
        await self.pipeline.cleanup();self.assertTrue(self.pipeline._folder(cid).exists())

    async def test_falha_de_conexao_antes_do_envio_pode_ser_retentada(self):
        import httpx
        self.clip(users=(1,))
        error=NetworkError('connection');error.__cause__=httpx.ConnectError('offline')
        self.bot.send_video.side_effect=error
        await self.pipeline.send(self.store.next_delivery())
        self.assertEqual(self.store.delivery_status(1,'alvo'),{'retry':1})
        with self.store.db:
            self.store.db.execute("UPDATE deliveries SET attempts=4,next_attempt=0 WHERE user_id=1")
        await self.pipeline.send(self.store.next_delivery())
        self.assertEqual(self.store.delivery_status(1,'alvo'),{'failed':1})

    async def test_expiracao_preserva_pendentes_sem_marcar_perda(self):
        cid,path=self.clip()
        with self.store.db:self.store.db.execute('UPDATE clips SET expires=0')
        await self.pipeline.cleanup()
        self.assertTrue(path.exists())
        self.assertEqual(self.store.delivery_status(1,'alvo'),{'pending':1})

    async def test_alerta_de_atraso_notifica_uma_vez_por_fila_e_preserva_falhas(self):
        failed,failed_path=self.clip(users=(1,))
        self.store.delivery(self.store.next_delivery(),'failed')
        _,pending_path=self.clip(users=(1,),started=2)
        pending=self.store.next_delivery()
        with self.store.db:self.store.db.execute('UPDATE clips SET expires=0')
        with patch.object(self.pipeline,'alert',new=AsyncMock(return_value=True)) as alert:
            await self.pipeline.cleanup()
            await self.pipeline.cleanup()
            alert.assert_awaited_once()
            self.store.delivery(pending,'uncertain')
            await self.pipeline.cleanup()
            self.assertFalse(self.store.has_overdue_work(1))
            self.assertTrue(failed_path.exists() and pending_path.exists())
            self.assertEqual(self.store.delivery_status(1,'alvo'),{'failed':1,'uncertain':1})
            self.store.set_clip(failed,'processing')
            await self.pipeline.cleanup()
            self.assertEqual(alert.await_count,2)

    async def test_recupera_raw_remove_orfaos_e_preserva_metadados(self):
        cid=uuid.uuid4().hex;folder=self.pipeline._folder(cid);folder.mkdir()
        raw=folder/'capture.mkv';raw.write_bytes(b'raw')
        (folder/'media.mp4').write_bytes(b'incomplete')
        self.store.register_clip(cid,'alvo','123456',1,raw,{},3600)
        orphan=uuid.uuid4().hex;self.pipeline._folder(orphan).mkdir()
        with patch.object(self.pipeline,'_process_loop',new=AsyncMock()),patch.object(self.pipeline,'_send_loop',new=AsyncMock()):
            await self.pipeline.start()
            await self.pipeline.close()
        self.assertTrue(raw.exists());self.assertFalse((folder/'media.mp4').exists())
        self.assertFalse(self.pipeline._folder(orphan).exists())

    async def test_preserva_mpeg_ts_sem_metadados_apos_reinicio(self):
        cid=uuid.uuid4().hex;folder=self.pipeline._folder(cid);folder.mkdir()
        raw=folder/'capture.ts';raw.write_bytes(b'recoverable')
        with patch.object(self.pipeline,'_process_loop',new=AsyncMock()),patch.object(self.pipeline,'_send_loop',new=AsyncMock()):
            await self.pipeline.start()
            await self.pipeline.close()
        self.assertEqual(raw.read_bytes(),b'recoverable')
        self.assertIn(cid,self.pipeline.budget.ids)

    async def test_limpeza_recusa_caminhos_fora_do_spool(self):
        with self.assertRaises(ValueError):self.pipeline._delete('../outside')

    async def test_arquivo_em_uso_tem_limpeza_reagendada_sem_perder_estado(self):
        cid,path=self.clip(users=())
        with patch('delivery_service.shutil.rmtree',side_effect=PermissionError):
            await self.pipeline.cleanup()
        self.assertTrue(path.exists());self.assertIn(cid,self.pipeline.pending_deletes)
        self.pipeline.pending_deletes[cid]=0
        await self.pipeline.cleanup()
        self.assertFalse(path.exists());self.assertNotIn(cid,self.pipeline.budget.ids)

    async def test_cache_auxiliar_da_api_e_removido_sem_tocar_banco(self):
        cache=self.config.api_files;cache.mkdir()
        (cache/'video.mp4').write_bytes(b'uploaded')
        await self.pipeline.cleanup()
        self.assertFalse((cache/'video.mp4').exists())
        self.assertTrue(self.config.database.exists())

    async def test_cache_e_origem_preservados_ate_confirmacao_do_upload(self):
        cid,path=self.clip(users=(1,))
        cache=self.config.api_files;cache.mkdir()
        cached=cache/'sending.mp4';cached.write_bytes(b'not_confirmed')
        await self.pipeline.cleanup()
        self.assertTrue(cached.exists());self.assertTrue(path.exists())
        self.bot.send_video.return_value=SimpleNamespace(message_id=100,video=SimpleNamespace(file_id='FILE'))
        await self.pipeline.send(self.store.next_delivery())
        await self.pipeline.cleanup()
        self.assertFalse(cached.exists());self.assertFalse(path.parent.exists())


class ProcessingTests(unittest.TestCase):
    def test_frame_unico_sem_tempo_nao_confunde_duracao_desconhecida_com_video_curto(self):
        probe=dict(format={},streams=[dict(codec_type='video',width=360,height=640,nb_read_packets='1'),
                                      dict(codec_type='audio')])
        with patch('delivery_service.media_duration',side_effect=RuntimeError('unknown_media_duration')) as duration:
            self.assertEqual(video_info(Path('single.mp4'),probe)['duration_seconds'],0.0)
            duration.assert_not_called()
            probe['streams'][0]['nb_read_packets']='2'
            with self.assertRaisesRegex(RuntimeError,'unknown_media_duration'):
                video_info(Path('unknown.mp4'),probe)

    def test_hevc_mp4_is_copied_with_identical_video_packets_and_original_resolution(self):
        from project_runtime import media_tool
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp);raw=folder/'capture.mp4'
            run_media([media_tool('ffmpeg'),'-v','error','-y','-f','lavfi','-i','testsrc2=size=360x640:rate=10',
                '-f','lavfi','-i','sine=frequency=440:sample_rate=48000','-t','2','-pix_fmt','yuv420p10le',
                '-c:v','libx265','-preset','ultrafast','-x265-params','log-level=error:pools=none:frame-threads=1',
                '-c:a','aac',str(raw)])
            def hashes(path):
                probe=json.loads(run_media([media_tool('ffprobe'),'-v','error','-show_packets',
                    '-select_streams','v:0','-show_entries','packet=data_hash','-show_data_hash','sha256','-of','json',str(path)]))
                return [p['data_hash'] for p in probe['packets']]
            before=hashes(raw)
            with patch('delivery_service.run_media',wraps=run_media) as runner:
                outputs=process_clip({'raw_path':str(raw),'metadata':'{}'},500_000_000)
                self.assertTrue(any('-c:v' in c.args[0] and c.args[0][c.args[0].index('-c:v')+1]=='copy' for c in runner.call_args_list))
                self.assertFalse(any('libx264' in c.args[0] for c in runner.call_args_list))
            outputs,info=prepare_videos(outputs,500_000_000);path=outputs[0][0]
            self.assertEqual(hashes(path),before)
            streams=json.loads(run_media([media_tool('ffprobe'),'-v','error','-show_streams','-of','json',str(path)]))['streams']
            video=next(s for s in streams if s['codec_type']=='video')
            self.assertEqual((video['codec_name'],video['codec_tag_string'],video['width'],video['height']),('hevc','hvc1',360,640))
            self.assertEqual(video['pix_fmt'],'yuv420p10le')

    def test_pipe_limita_bytes_antes_de_escrever_e_descarta_saida_incompleta(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'part.bin'
            with self.assertRaises(PartTooLarge):
                run_media([sys.executable,'-c',"import sys;sys.stdout.buffer.write(b'x'*200000)"],sink=(path,70000))
            self.assertFalse(path.exists())
            run_media([sys.executable,'-c',"import sys;sys.stdout.buffer.write(b'x'*50000)"],sink=(path,70000))
            self.assertEqual(path.stat().st_size,50000)
    def test_divide_acima_do_limite_com_ffmpeg_real(self):
        import subprocess
        from project_runtime import media_tool
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp);raw=folder/'capture.mkv'
            subprocess.run([media_tool('ffmpeg'),'-v','error','-y','-f','lavfi','-i',
                'testsrc2=size=320x240:rate=30:duration=6','-f','lavfi','-i','sine=duration=6',
                '-c:v','libx264','-threads','2','-preset','ultrafast','-g','30','-c:a','libopus',
                '-shortest',str(raw)],check=True,capture_output=True)
            row={'raw_path':str(raw),'metadata':json.dumps({'mime':'video/x-matroska;codecs=avc1,opus'}),
                 'username':'alvo','live_id':'123456'}
            outputs=process_clip(row,180000)
            self.assertGreater(len(outputs),1)
            self.assertTrue(raw.exists(),'bruto precisa existir ate confirmar a transacao SQLite')
            duration=0
            for path,kind in outputs:
                self.assertEqual(kind,'video');self.assertLessEqual(path.stat().st_size,180000)
                value=json.loads(subprocess.check_output([media_tool('ffprobe'),'-v','error','-show_format','-of','json',str(path)]))
                duration+=float(value['format']['duration'])
                subprocess.run([media_tool('ffmpeg'),'-v','error','-xerror','-i',str(path),'-f','null','-'],check=True,capture_output=True)
            self.assertGreater(duration,5.5)

    def test_processo_e_interrompido_se_orcamento_for_excedido(self):
        with self.assertRaisesRegex(RuntimeError,'spool_limit'):
            run_media([sys.executable,'-c','print(1)'],quota=lambda:False)


class SafetyTests(unittest.TestCase):
    def test_buffer_wait_is_cancelled_by_membership_without_reserving_another_slot(self):
        budget=SpoolBudget(1);stop=threading.Event();wanted=threading.Event();wanted.set()
        budget.acquire('first',stop);entered=threading.Event();errors=[]
        def allowed():
            self.assertFalse(budget.condition._is_owned())
            entered.set();return wanted.is_set()
        def wait():
            try:budget.acquire('second',stop,allowed)
            except Exception as exc:errors.append(exc)
        worker=threading.Thread(target=wait);worker.start()
        self.assertTrue(entered.wait(1));wanted.clear();worker.join(timeout=2)
        self.assertFalse(worker.is_alive());self.assertEqual(budget.ids,{'first'})
        self.assertIsInstance(errors[0],InterruptedError)

    @unittest.skipUnless(os.name=='nt','Job Object exige Windows')
    def test_crash_do_principal_elimina_processos_filhos(self):
        import ctypes
        import subprocess
        code="""import json,os,subprocess,sys,time
from pathlib import Path
sys.path.insert(0,str(Path.cwd()/"src"))
from process_control import install_process_job
install_process_job()
p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
print(json.dumps({'leader':os.getpid(),'child':p.pid}),flush=True)
time.sleep(60)
"""
        proc=subprocess.Popen([sys.executable,'-B','-c',code],cwd=Path(__file__).resolve().parents[1],
                              stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        data=json.loads(proc.stdout.readline())
        k=ctypes.WinDLL('kernel32',use_last_error=True)
        k.OpenProcess.restype=ctypes.c_void_p
        k.OpenProcess.argtypes=[ctypes.c_uint32,ctypes.c_bool,ctypes.c_uint32]
        k.WaitForSingleObject.argtypes=[ctypes.c_void_p,ctypes.c_uint32]
        k.CloseHandle.argtypes=[ctypes.c_void_p]
        handle=k.OpenProcess(0x100000,False,data['child'])
        self.assertTrue(handle)
        try:
            os.kill(data['leader'],15)
            proc.wait(timeout=10)
            self.assertEqual(k.WaitForSingleObject(handle,5000),0,'filho ficou orfao')
        finally:
            k.CloseHandle(handle)
            if proc.poll() is None:proc.kill();proc.wait()
            proc.stdout.close();proc.stderr.close()

    def test_instancia_unica_inclusive_outra_copia_do_projeto(self):
        with tempfile.TemporaryDirectory() as a,tempfile.TemporaryDirectory() as b:
            with InstanceGuard('test-token',Path(a)):
                with self.assertRaises(RuntimeError):
                    with InstanceGuard('test-token',Path(a)):pass
                if sys.platform=='win32':
                    with self.assertRaises(RuntimeError):
                        with InstanceGuard('test-token',Path(b)):pass
            with InstanceGuard('test-token',Path(a)):pass

    def test_buffer_nao_cresce_e_espera_pode_ser_interrompida(self):
        budget=SpoolBudget(1);stop=threading.Event()
        budget.acquire('first',stop);stop.set()
        with self.assertRaises(InterruptedError):budget.acquire('second',stop)
        self.assertEqual(budget.ids,{'first'});budget.release('first')

    def test_log_remove_segredos_e_token_de_urls(self):
        formatter=SafeFormatter(['PRIVATE_HASH'])
        record=logging.LogRecord('test',40,'',1,'https://a/bot123456:ABCDEFGHIJKLMNOP PRIVATE_HASH',(),None)
        text=formatter.format(record)
        self.assertNotIn('PRIVATE_HASH',text);self.assertNotIn('ABCDEFGHIJKLMNOP',text)
