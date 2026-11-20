"""Estados oficiais do perfil: offline, privada, bloqueio e retorno à sala pública."""
import asyncio
import json
from pathlib import Path
import sys
import tempfile
import threading
from unittest.mock import AsyncMock,patch
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from recorder import Live
from stripchat_capture import parse_model_info,ProfileUnavailable,IdentityError
from stripchat_recorder import StripchatRecorder,Session


def html(status='off',live=False,username='alvo',**flags):
    return json.dumps({'viewCamBase':{'model':dict(id=111,username=username,status=status,isLive=live,**flags)}})


class AccessTests(unittest.IsolatedAsyncioTestCase):
    async def test_redirecionamento_automatico_bloqueia_captura_sem_encerrar_monitor_permanentemente(self):
        from types import SimpleNamespace
        frame=object();session=Session(SimpleNamespace(low_resource_mode=True),parse_model_info(html('public',True),'alvo'))
        session.confirmed=True;session.page=SimpleNamespace(main_frame=frame)
        session.responses=SimpleNamespace(error=None)
        route=SimpleNamespace(request=SimpleNamespace(resource_type='document',is_navigation_request=lambda:True,frame=frame,
                              url='https://pt.stripchat.com/outro'),abort=AsyncMock(),fallback=AsyncMock())
        await session.guard_navigation(route)
        route.abort.assert_awaited_once();route.fallback.assert_not_awaited()
        self.assertIsInstance(session.responses.error,ProfileUnavailable)

    async def test_privada_enquanto_aguarda_buffer_nao_grava_o_segmento_ao_liberar_vaga(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp);owner=StripchatRecorder(Live('alvo','','',0,''),folder,part_limit=500000000)
            entered=threading.Event();release=threading.Event();outputs=[]
            def base():entered.set();release.wait(3);return folder/'capture'
            owner._recording_base=base;owner.segment_callback=lambda path,meta:outputs.append(path.read_bytes())
            session=Session(owner,parse_model_info(html('public',True),'alvo'))
            session.confirmed=True
            session.page=SimpleNamespace(url='https://pt.stripchat.com/alvo',is_closed=lambda:False)
            await session.accept('https://cdn/x_init_111.mp4',b'\0\0\0\x10ftypavc1')
            pending=asyncio.create_task(session.accept('https://cdn/x_1_source_111.mp4',b'\0\0\0\x10moofPRIVATE'))
            while not entered.is_set():await asyncio.sleep(.01)
            session.public=False;owner.access('private',True);release.set()
            await pending;await session.publish()
            self.assertEqual(outputs,[b''])

    async def test_somente_public_explicitamente_confirmado_pode_gravar(self):
        for status,live,access in (('off',False,'offline'),('off',True,'offline'),
                                   ('private',True,'private'),('p2p',True,'private'),
                                   ('groupShow',True,'private'),('unknown',True,'restricted'),('public',True,'public')):
            info=parse_model_info(html(status,live),'alvo')
            self.assertEqual(info['status'],access)
            self.assertEqual(info['recordable'],access=='public')
        self.assertFalse(parse_model_info(html('public',True,isBlocked=True),'alvo')['recordable'])
        with self.assertRaises(ProfileUnavailable):parse_model_info(html(username='outro'),'alvo')
        with self.assertRaises(ProfileUnavailable) as error:
            parse_model_info(json.dumps({'viewCamBase':{'model':None,'error':{'type':'geoBanned'}}}),'alvo')
        self.assertEqual(error.exception.status,'blocked')

    async def test_offline_e_privada_continuam_monitorando_sem_abrir_navegador(self):
        with tempfile.TemporaryDirectory() as tmp:
            owner=StripchatRecorder(Live('alvo','','',0,''),Path(tmp),part_limit=500000000)
            owner.identity_callback=lambda uid:None
            owner.live_id_callback=lambda token,online:'session'
            owner.capture_allowed=lambda live_id:True
            states=iter([parse_model_info(html(),'alvo'),parse_model_info(html('private',True),'alvo'),
                         ProfileUnavailable('blocked'),parse_model_info(html('public',True),'alvo')])
            def profile(*args):
                value=next(states)
                if isinstance(value,Exception):raise value
                return value
            async def pause(seconds):await asyncio.sleep(0)
            async def capture():owner.stop_event.set()
            with patch('stripchat_recorder.model_info',side_effect=profile),patch('stripchat_recorder.DirectSession') as session:
                session.return_value.run=AsyncMock(side_effect=capture)
                owner.pause=pause
                await owner.monitor()
                session.assert_called_once()
            self.assertFalse(owner._identity_error)
            self.assertEqual(owner.progress()['access'],'public')

    async def test_transicao_privada_descarta_buffer_e_impede_fallback_e_novos_blocos(self):
        with tempfile.TemporaryDirectory() as tmp:
            owner=StripchatRecorder(Live('alvo','','',0,''),Path(tmp),part_limit=500000000)
            info=parse_model_info(html('public',True),'alvo')
            session=Session(owner,info)
            from types import SimpleNamespace
            session.writer=SimpleNamespace(buf={1:b'unconfirmed'})
            with patch('stripchat_recorder.model_info',return_value=parse_model_info(html('private',True),'alvo')):
                self.assertFalse(await session.refresh_access())
                self.assertEqual(owner.progress()['access'],'private')
                self.assertFalse(session.writer.buf)
                with self.assertRaises(ProfileUnavailable):await session.start_recorder()
            self.assertFalse(session.public)

    async def test_id_real_diferente_continua_bloqueado(self):
        with tempfile.TemporaryDirectory() as tmp:
            owner=StripchatRecorder(Live('alvo','','',0,'',user_id='222'),Path(tmp))
            with patch('stripchat_recorder.model_info',return_value=parse_model_info(html(),'alvo')):
                with self.assertRaises(IdentityError):await owner.monitor()
