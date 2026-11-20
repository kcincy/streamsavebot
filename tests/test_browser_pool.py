"""Chromium real: uma instancia, clientes de threads diferentes e cookies isolados."""
import asyncio
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from browser_pool import BrowserPool
from recorder import sync_playwright,expendable_resource


class BrowserPoolTests(unittest.IsolatedAsyncioTestCase):
    async def test_clientes_isolados_e_fechar_um_nao_interrompe_outro(self):
        pool=BrowserPool();ready=[threading.Event(),threading.Event()];closed=threading.Event()
        try:
            one,two=await asyncio.gather(pool.endpoint(),pool.endpoint())
            self.assertEqual(one,two)
            def client(index):
                with sync_playwright() as pw:
                    browser=pw.chromium.connect_over_cdp(one)
                    context=browser.new_context()
                    try:
                        context.add_cookies([{'name':'owner','value':str(index),'url':'https://example.org'}])
                        page=context.new_page();page.set_content(f'<p>{index}</p>')
                        ready[index].set();self.assertTrue(ready[1-index].wait(10))
                        if index==1:
                            self.assertTrue(closed.wait(10))
                            self.assertEqual(page.locator('p').inner_text(),'1')
                        self.assertEqual(context.cookies('https://example.org')[0]['value'],str(index))
                    finally:
                        context.close();browser.close()
                        if index==0:closed.set()
            await asyncio.wait_for(asyncio.gather(asyncio.to_thread(client,0),asyncio.to_thread(client,1)),30)
            self.assertTrue(pool.browser.is_connected())
            protocol=await pool.browser.new_browser_cdp_session()
            contexts=await protocol.send('Target.getBrowserContexts')
            self.assertEqual(contexts['browserContextIds'],[],'nenhum contexto de captura pode ficar abandonado')
        finally:await pool.close()

    async def test_recupera_crash_e_nao_reabre_apos_encerramento(self):
        pool=BrowserPool()
        try:
            await pool.endpoint();previous=pool.browser
            await previous.close()
            await pool.endpoint()
            self.assertIsNot(pool.browser,previous);self.assertTrue(pool.browser.is_connected())
        finally:await pool.close()
        with self.assertRaises(InterruptedError):await pool.endpoint()

    def test_filtro_preserva_midia_scripts_api_e_documentos(self):
        for kind in ('media','script','xhr','fetch','document','websocket','stylesheet'):
            self.assertFalse(expendable_resource(SimpleNamespace(resource_type=kind)))
        for kind in ('image','font'):
            self.assertTrue(expendable_resource(SimpleNamespace(resource_type=kind)))
