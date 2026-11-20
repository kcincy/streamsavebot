"""Exclusao mutua e propriedade dos processos nativos, sem pidfiles obsoletos."""
import asyncio
import ctypes
import hashlib
import logging
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import threading
from urllib.parse import urlsplit

from project_runtime import CACHE, DATABASE, TOOLS

log = logging.getLogger(__name__)
_job = None


class InstanceGuard:
    def __init__(self, token, folder=CACHE):
        self.token, self.folder = token, Path(folder)
        self.file = None
        self.mutex = None

    def __enter__(self):
        self.folder.mkdir(parents=True,exist_ok=True)
        try:
            self.file = (self.folder/'application.lock').open('a+b')
            if os.name == 'nt':
                import msvcrt
                self.file.seek(0)
                if not self.file.read(1):
                    self.file.write(b'0'); self.file.flush()
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(),msvcrt.LK_NBLCK,1)
                k = ctypes.WinDLL('kernel32',use_last_error=True)
                k.CreateMutexW.restype = ctypes.c_void_p
                k.CreateMutexW.argtypes = [ctypes.c_void_p,ctypes.c_bool,ctypes.c_wchar_p]
                name = 'Global\\SuperLiveBot-'+hashlib.sha256(self.token.encode()).hexdigest()
                self.mutex = k.CreateMutexW(None,False,name)
                if not self.mutex or ctypes.get_last_error() == 183:
                    raise OSError('already_running')
            else:
                import fcntl
                fcntl.flock(self.file,fcntl.LOCK_EX|fcntl.LOCK_NB)
            return self
        except OSError:
            self.__exit__(None,None,None)
            raise RuntimeError('Ja existe uma instancia do projeto ou deste bot em execucao.') from None

    def __exit__(self,*args):
        if self.mutex:
            k = ctypes.WinDLL('kernel32',use_last_error=True)
            k.CloseHandle.argtypes = [ctypes.c_void_p]
            k.CloseHandle(self.mutex)
            self.mutex = None
        if self.file:
            self.file.close(); self.file = None


def install_process_job():
    """No Windows, um encerramento abrupto tambem encerra todos os descendentes."""
    global _job
    if os.name != 'nt' or _job:
        return
    from ctypes import wintypes as w
    class Basic(ctypes.Structure):
        _fields_=[('ProcessTime',ctypes.c_int64),('JobTime',ctypes.c_int64),('LimitFlags',w.DWORD),
                  ('MinimumWorkingSet',ctypes.c_size_t),('MaximumWorkingSet',ctypes.c_size_t),
                  ('ActiveProcessLimit',w.DWORD),('Affinity',ctypes.c_size_t),('PriorityClass',w.DWORD),('SchedulingClass',w.DWORD)]
    class IO(ctypes.Structure):
        _fields_=[(name,ctypes.c_uint64) for name in ('ReadOps','WriteOps','OtherOps','ReadBytes','WriteBytes','OtherBytes')]
    class Extended(ctypes.Structure):
        _fields_=[('Basic',Basic),('IO',IO),('ProcessMemoryLimit',ctypes.c_size_t),('JobMemoryLimit',ctypes.c_size_t),
                  ('PeakProcessMemoryUsed',ctypes.c_size_t),('PeakJobMemoryUsed',ctypes.c_size_t)]
    k=ctypes.WinDLL('kernel32',use_last_error=True)
    k.CreateJobObjectW.restype=w.HANDLE
    k.CreateJobObjectW.argtypes=[ctypes.c_void_p,w.LPCWSTR]
    k.SetInformationJobObject.argtypes=[w.HANDLE,ctypes.c_int,ctypes.c_void_p,w.DWORD]
    k.AssignProcessToJobObject.argtypes=[w.HANDLE,w.HANDLE]
    k.GetCurrentProcess.restype=w.HANDLE
    job=k.CreateJobObjectW(None,None)
    limits=Extended();limits.Basic.LimitFlags=0x2000 # KILL_ON_JOB_CLOSE
    if not job or not k.SetInformationJobObject(job,9,ctypes.byref(limits),ctypes.sizeof(limits)) or not k.AssignProcessToJobObject(job,k.GetCurrentProcess()):
        raise RuntimeError('Nao foi possivel proteger os processos filhos no Windows.')
    # Mantido ate o fim do processo; o Windows libera o handle mesmo em crash.
    _job=job


CONSOLE_STOP = """import ctypes,sys,time
k=ctypes.WinDLL('kernel32',use_last_error=True);k.FreeConsole()
assert k.AttachConsole(int(sys.argv[1])),ctypes.get_last_error()
k.SetConsoleCtrlHandler(None,True)
assert k.GenerateConsoleCtrlEvent(0,0),ctypes.get_last_error()
time.sleep(.2);k.FreeConsole()
"""


class LocalAPI:
    def __init__(self, config):
        self.config=config
        self.process=None
        self.reader=None

    async def start(self):
        if self.config.api_mode != 'local':
            return
        if not os.environ.get('TELEGRAM_API_ID') or not os.environ.get('TELEGRAM_API_HASH'):
            raise ValueError('Defina TELEGRAM_API_ID e TELEGRAM_API_HASH no .env.')
        binary=TOOLS/'telegram-bot-api/bin'/('telegram-bot-api.exe' if os.name=='nt' else 'telegram-bot-api')
        if not binary.is_file():
            raise ValueError('Servidor oficial ausente em tools/telegram-bot-api/bin.')
        port=urlsplit(self.config.api_url).port
        with socket.socket() as check:
            if check.connect_ex(('127.0.0.1',port)) == 0:
                raise RuntimeError('A porta da Bot API ja esta ocupada. Encerre a execucao anterior.')
        working=DATABASE/'telegram-bot-api'
        temporary=CACHE/'telegram-bot-api/tmp'
        for folder in (working,self.config.api_files,temporary):
            folder.mkdir(parents=True,exist_ok=True)
        args=[str(binary),'--local','--http-ip-address=127.0.0.1',f'--http-port={port}',
              '--verbosity=0','--memory-verbosity=0',f'--dir={working}',
              f'--files-dir={self.config.api_files}',f'--temp-dir={temporary}']
        flags={}
        if os.name == 'nt':
            startup=subprocess.STARTUPINFO();startup.dwFlags|=subprocess.STARTF_USESHOWWINDOW
            startup.wShowWindow=0
            flags=dict(creationflags=subprocess.CREATE_NEW_CONSOLE,startupinfo=startup)
        else:
            flags=dict(start_new_session=True)
        self.process=subprocess.Popen(args,cwd=binary.parent,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,**flags)
        self.reader=threading.Thread(target=self._read,daemon=True,name='api-log')
        self.reader.start()
        for _ in range(200):
            if self.process.poll() is not None:
                raise RuntimeError('O servidor local encerrou durante a inicializacao. Confira logs/bot.log.')
            try:
                reader,writer=await asyncio.open_connection('127.0.0.1',port)
                writer.close();await writer.wait_closed()
                log.info('Bot API local pronta pid=%s porta=%s',self.process.pid,port)
                return
            except OSError:
                await asyncio.sleep(.1)
        raise RuntimeError('Tempo esgotado ao iniciar a Bot API local.')

    def _read(self):
        for raw in iter(lambda:self.process.stdout.readline(4096),b''):
            text=raw.decode('utf-8',errors='replace').strip()
            if text:
                log.error('bot-api %s',text)

    async def close(self):
        p=self.process
        if not p:
            return
        if p.poll() is None:
            if os.name == 'nt':
                await asyncio.to_thread(subprocess.run,[sys.executable,'-B','-c',CONSOLE_STOP,str(p.pid)],
                                        stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                                        creationflags=subprocess.CREATE_NO_WINDOW,timeout=5)
            else:
                os.killpg(p.pid,signal.SIGTERM)
            try:
                await asyncio.to_thread(p.wait,timeout=20)
            except subprocess.TimeoutExpired:
                log.error('Bot API nao respondeu ao encerramento normal; terminando processo proprio pid=%s',p.pid)
                p.kill();await asyncio.to_thread(p.wait)
        if self.reader:
            await asyncio.to_thread(self.reader.join,2)
        p.stdout.close()
        log.info('Bot API encerrada pid=%s status=%s',p.pid,p.returncode)
