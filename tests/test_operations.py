import asyncio
from dataclasses import replace
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,Mock,patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from config import Config
from operations import Operations


class OperationsTests(unittest.IsolatedAsyncioTestCase):
    async def test_stalled_capture_restarts_only_its_session_and_preserves_subscription(self):
        rec=SimpleNamespace(progress=lambda:dict(part_id='part',part_bytes=100,recording=True),
                            stop_event=threading.Event(),request_part=Mock())
        healthy=SimpleNamespace(progress=lambda:dict(part_id='other',part_bytes=200,recording=True),
                                stop_event=threading.Event(),request_part=Mock())
        gate=SimpleNamespace(blocked=False,reason='',revoked=set())
        registry=SimpleNamespace(monitors={'stalled':SimpleNamespace(recorder=rec),'healthy':SimpleNamespace(recorder=healthy)},gate=gate)
        delivery=SimpleNamespace(alert=AsyncMock())
        ops=Operations(Config('test'),registry,delivery)
        ops.progress={'stalled':(('part',100),0),'healthy':(('other',100),0)}
        async def finish(_):ops.stop.set()
        ops.wait=finish
        with patch('operations.time.monotonic',return_value=100):await ops.watch()
        self.assertTrue(rec.stop_event.is_set());rec.request_part.assert_called_once()
        self.assertFalse(healthy.stop_event.is_set());healthy.request_part.assert_not_called()
        delivery.alert.assert_awaited_once()

    async def test_backup_failure_releases_pipeline_and_alerts(self):
        registry=SimpleNamespace(gate=SimpleNamespace(sample=dict(cpu=.1,memory=.1)))
        delivery=SimpleNamespace(backing_up=False,processing=None,inflight=None,alert=AsyncMock())
        ops=Operations(Config('test'),registry,delivery)
        async def wait(seconds):
            if seconds==3600:ops.stop.set()
        ops.wait=wait
        with patch('operations.create_backup',side_effect=OSError):await ops.backups()
        self.assertFalse(delivery.backing_up);delivery.alert.assert_awaited_once()


if __name__=='__main__':unittest.main()
