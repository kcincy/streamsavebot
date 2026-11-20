"""Corridas reais de tarefas: leitura antecipada, ordem, recuperação e navegação."""
import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from response_queue import ResponseQueue,MAX_BODY


class QueueTests(unittest.IsolatedAsyncioTestCase):
    def response(self,name,body):
        return SimpleNamespace(url='https://cdn.invalid/'+name+'?token=SECRET',body=body,
                               headers={},status=200,request=SimpleNamespace(headers={'range':'bytes=0-100'}))

    async def test_leituras_comecam_sem_esperar_identidade_ou_corpo_anterior_e_aplicam_em_ordem(self):
        release=asyncio.Event();started=[];accepted=[]
        async def slow():
            started.append('init');await release.wait();return b'init'
        async def fast():started.append('segment');return b'segment'
        async def apply(url,body):accepted.append(body)
        page=SimpleNamespace(main_frame=object())
        queue=ResponseQueue(page,apply,lambda:True)
        queue.submit(self.response('init.mp4',slow));queue.submit(self.response('segment.mp4',fast))
        await asyncio.sleep(.03)
        self.assertEqual(started,['init','segment']);self.assertFalse(accepted)
        release.set();await asyncio.sleep(.01)
        self.assertFalse(accepted)
        await queue.drain()
        self.assertEqual(accepted,[b'init',b'segment'])
        self.assertFalse(queue.tasks)

    async def test_falha_de_corpo_tenta_mesma_url_uma_vez_com_range_e_libera_resposta(self):
        recovered=SimpleNamespace(headers={},status=206,body=AsyncMock(return_value=b'recovered'),dispose=AsyncMock())
        page=SimpleNamespace(main_frame=object(),request=SimpleNamespace(get=AsyncMock(return_value=recovered)))
        accepted=AsyncMock();queue=ResponseQueue(page,accepted,lambda:True);queue.ready.set()
        response=self.response('segment.mp4',AsyncMock(side_effect=RuntimeError('No data found')));response.status=206
        queue.submit(response);await queue.drain()
        self.assertEqual(accepted.call_args.args[1],b'recovered')
        self.assertEqual(page.request.get.call_args.args[0],response.url)
        self.assertEqual(page.request.get.call_args.kwargs['headers'],{'range':'bytes=0-100'})
        recovered.dispose.assert_awaited_once()
        response.headers={'content-range':'bytes 0-100/200'}
        queue=ResponseQueue(page,AsyncMock(),lambda:True);queue.ready.set()
        with self.assertLogs('response_queue',level='WARNING'):
            queue.submit(response);await queue.drain()
        self.assertTrue(queue.error)

    async def test_navegacao_durante_leitura_nao_recupera_ou_grava_outro_fluxo_e_log_nao_vaza_url(self):
        release=asyncio.Event()
        async def fail():await release.wait();raise RuntimeError('No data SECRET')
        frame=object();page=SimpleNamespace(main_frame=frame,request=SimpleNamespace(get=AsyncMock()))
        accepted=AsyncMock();queue=ResponseQueue(page,accepted,lambda:True);queue.ready.set()
        queue.submit(self.response('segment.mp4',fail));await asyncio.sleep(.01)
        queue.navigation(frame);release.set()
        with self.assertLogs('response_queue',level='WARNING') as output:await queue.drain()
        self.assertTrue(queue.error);accepted.assert_not_awaited();page.request.get.assert_not_awaited()
        self.assertNotIn('SECRET',' '.join(output.output));self.assertNotIn('cdn.invalid',' '.join(output.output))

    async def test_limites_de_fila_e_tamanho_nao_acumulam_corpos_sem_limite(self):
        event=asyncio.Event()
        async def delayed():await event.wait();return b'data'
        queue=ResponseQueue(SimpleNamespace(main_frame=object()),AsyncMock(),lambda:True)
        for _ in range(13):queue.submit(self.response('segment.mp4',delayed))
        self.assertEqual(len(queue.tasks),12);self.assertTrue(queue.error)
        event.set();await queue.drain()
        response=self.response('oversized.mp4',AsyncMock())
        response.headers={'content-length':str(MAX_BODY+1)}
        queue=ResponseQueue(SimpleNamespace(),AsyncMock(),lambda:True)
        with self.assertLogs('response_queue',level='WARNING'):
            queue.submit(response);await queue.drain()
        response.body.assert_not_awaited();self.assertTrue(queue.error)
