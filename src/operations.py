"""Background recovery and local backups, without an administrative panel."""
import asyncio
import json
import logging
from pathlib import Path
import socket
import time

from backups import create_backup

log=logging.getLogger(__name__)


class Operations:
    def __init__(self,config,registry,delivery):
        self.config,self.registry,self.delivery=config,registry,delivery
        self.stop=asyncio.Event();self.tasks=[];self.progress={}
        self.last_report=-1e9

    def start(self):
        self.tasks=[asyncio.create_task(self.watch(),name='session-watchdog'),
                    asyncio.create_task(self.backups(),name='local-backups')]

    async def wait(self,seconds):
        try:await asyncio.wait_for(self.stop.wait(),seconds)
        except asyncio.TimeoutError:pass

    async def watch(self):
        while not self.stop.is_set():
            now=time.monotonic()
            for key,monitor in list(self.registry.monitors.items()):
                rec=monitor.recorder;value=rec.progress()
                mark=(value.get('part_id'),value.get('part_bytes'))
                previous,changed=self.progress.get(key,(mark,now))
                if mark!=previous or not value.get('recording'):changed=now
                self.progress[key]=(mark,changed)
                gate=self.registry.gate
                starting=(key in getattr(gate,'starting',set()) and gate.clock()-gate.active[key][0]>120)
                if (starting or (value.get('recording') and now-changed>90)) and not rec.stop_event.is_set():
                    log.warning('stalled_capture_recovery profile=%s',key)
                    rec.request_part();rec.stop_event.set()
                    await self.delivery.alert('stalled','A recording stopped receiving data. Its current part is being finalized and recording will restart automatically.')
            for key in set(self.progress)-set(self.registry.monitors):self.progress.pop(key,None)
            gate=self.registry.gate
            if now-self.last_report>=60:
                self.last_report=now
                if hasattr(gate,'health'):log.info('runtime_health %s',json.dumps(gate.health()))
            if gate.blocked or gate.reason in ('disk','metrics'):
                await self.delivery.alert('resources','Capture admission is waiting for resources. Existing recordings and deliveries are being preserved.')
            if gate.revoked:
                await self.delivery.alert('resource_suspend','Critical resource usage: a recent capture is being finalized and will return to the waiting queue.')
            await self.wait(10)

    async def backups(self):
        await self.wait(30)
        while not self.stop.is_set():
            sample=self.registry.gate.sample
            if sample is None or sample['cpu']>=.65 or sample['memory']>=.7:
                await self.wait(60);continue
            if self.delivery.processing or self.delivery.inflight:
                await self.wait(5);continue
            self.delivery.backing_up=True
            success=False
            try:
                while (self.delivery.processing or self.delivery.inflight) and not self.stop.is_set():await self.wait(1)
                if self.stop.is_set():break
                path=await asyncio.to_thread(create_backup,self.config)
                log.info('local_backup_verified archive=%s',path.name);success=True
            except Exception as exc:
                log.error('local_backup_failed error=%s',type(exc).__name__)
                await self.delivery.alert('backup','The local backup could not be completed. Previous verified backups have been preserved.')
            finally:self.delivery.backing_up=False
            await self.wait(24*3600 if success else 3600)

    async def close(self):
        self.stop.set()
        await asyncio.gather(*self.tasks,return_exceptions=True)


def healthcheck(path=Path('/app/cache/health.json')):
    value=json.loads(Path(path).read_text())
    if time.time()-value['at']>45:raise RuntimeError('heartbeat_stale')
    with socket.create_connection(('127.0.0.1',8081),timeout=2):pass


if __name__=='__main__':
    import sys
    try:healthcheck()
    except Exception:
        print('Bot heartbeat or local API unavailable.');sys.exit(1)
