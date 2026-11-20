"""Um Chromium do projeto; cookies, paginas e capturas continuam isolados por contexto."""
import asyncio
import logging
import socket

from playwright.async_api import async_playwright
from recorder import launch_args

log=logging.getLogger(__name__)


class BrowserPool:
    def __init__(self):
        self.lock=asyncio.Lock()
        self.pw=None;self.browser=None;self.url=None;self.closed=False

    async def endpoint(self):
        async with self.lock:
            if self.closed:raise InterruptedError()
            if self.browser and self.browser.is_connected():return self.url
            if self.pw is None:self.pw=await async_playwright().start()
            with socket.socket() as sock:
                sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
            try:
                self.browser=await self.pw.chromium.launch(channel='chromium',headless=True,handle_sigint=False,
                    args=[*launch_args(False),f'--remote-debugging-port={port}','--remote-debugging-address=127.0.0.1'])
            except Exception:
                await self.pw.stop();self.pw=None
                raise
            self.url=f'http://127.0.0.1:{port}'
            log.info('navegador compartilhado iniciado; contextos de captura isolados')
            return self.url

    async def close(self):
        self.closed=True
        async with self.lock:
            try:
                if self.browser:await self.browser.close()
            finally:
                if self.pw:await self.pw.stop()
                self.browser=self.pw=None
