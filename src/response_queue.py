"""Busca imediata e aplicação ordenada; nunca espera rede sob o bloqueio do escritor."""
import asyncio
import hashlib
import logging
import time

log=logging.getLogger(__name__)
MAX_BODY=8*1024*1024


class ResponseQueue:
    def __init__(self,page,accept,valid):
        self.page,self.accept,self.valid=page,accept,valid
        self.tasks=set()
        self.tail=None
        self.epoch=0
        self.last_navigation=time.monotonic()
        self.closed=False
        self.error=None
        self.ready=asyncio.Event()

    def navigation(self,frame):
        if frame==self.page.main_frame:
            self.epoch+=1
            self.last_navigation=time.monotonic()

    def submit(self,response):
        if self.closed:
            return
        if len(self.tasks)>=12:
            self.error=RuntimeError('response_queue_full')
            log.warning('fila de respostas cheia; sessão será finalizada sem misturar fluxos')
            return
        task=asyncio.create_task(self.consume(response,self.tail,self.epoch))
        self.tail=task
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    @staticmethod
    def check_size(response):
        length=response.headers.get('content-length','0')
        if int(length)>MAX_BODY:
            raise RuntimeError('segment_body_too_large')

    async def read(self,response,epoch):
        self.check_size(response)
        try:
            body=await asyncio.wait_for(response.body(),12)
        except Exception:
            if epoch==self.epoch and not self.ready.is_set():
                await asyncio.wait_for(self.ready.wait(),12)
            if epoch!=self.epoch or not self.valid():
                raise RuntimeError('navigation_invalidated_response') from None
            # Uma recuperação explícita com os cookies da sessão, somente para o mesmo alvo.
            headers={k:v for k,v in response.request.headers.items() if k.lower() in ('range','referer','user-agent')}
            recovered=await self.page.request.get(response.url,headers=headers,timeout=12000,max_redirects=0)
            try:
                self.check_size(recovered)
                if recovered.status!=response.status or recovered.status not in (200,206):
                    raise RuntimeError('segment_recovery_rejected')
                if (response.headers.get('content-range')
                        and recovered.headers.get('content-range')!=response.headers['content-range']):
                    raise RuntimeError('segment_recovery_range_mismatch')
                body=await recovered.body()
            finally:
                await recovered.dispose()
        if len(body)>MAX_BODY:
            raise RuntimeError('segment_body_too_large')
        return body

    async def consume(self,response,previous,epoch):
        try:
            # Começa antes de esperar identidade, init ou qualquer resposta anterior.
            body=await self.read(response,epoch)
            if previous:
                await previous
            await asyncio.wait_for(self.ready.wait(),30)
            if self.error is None and epoch==self.epoch and self.valid():
                await self.accept(response.url,body)
            elif epoch!=self.epoch:
                raise RuntimeError('navigation_invalidated_response')
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.error=self.error or exc
            segment=hashlib.sha256(response.url.split('?',1)[0].encode()).hexdigest()[:12]
            safe_reasons={'segment_body_too_large','navigation_invalidated_response','segment_recovery_rejected',
                          'segment_recovery_range_mismatch','invalid_mp4_segment','segment_reorder_buffer_full',
                          'segment_exceeds_part_limit','next_segment_exceeds_limit'}
            reason=str(exc) if str(exc) in safe_reasons else type(exc).__name__
            log.warning('falha segmento id=%s época=%s navegação_há=%.2fs erro=%s',segment,epoch,
                        time.monotonic()-self.last_navigation,reason)

    async def drain(self):
        self.closed=True
        self.ready.set()
        tasks=list(self.tasks)
        try:
            await asyncio.wait_for(asyncio.gather(*tasks,return_exceptions=True),30)
        except asyncio.TimeoutError:
            self.error=self.error or RuntimeError('pending_responses_timeout')
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks,return_exceptions=True)
            log.warning('respostas pendentes expiraram; prefixo capturado será preservado')
