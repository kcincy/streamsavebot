"""Inicializacao e encerramento coordenados do bot e seus servicos."""
import os
import signal
import sys

import asyncio
import logging
from dotenv import load_dotenv
from telegram.error import NetworkError

from config import Config
from delivery_service import DeliveryService
from logging_setup import configure_logging
from monitor_service import MonitorRegistry
from process_control import InstanceGuard, LocalAPI, install_process_job
from project_runtime import ROOT, LOGS, configure_runtime, media_tool
from storage import Store
from operations import Operations
from version import VERSION
from telegram_bot import build_application, install_commands, configure_command_menu

log=logging.getLogger(__name__)


async def run(config, stop=None):
    stop=stop or asyncio.Event()
    loop=asyncio.get_running_loop()
    previous={}
    for name in ('SIGINT','SIGTERM','SIGBREAK'):
        sig=getattr(signal,name,None)
        if sig is not None:
            previous[sig]=signal.signal(sig,lambda *_:loop.call_soon_threadsafe(stop.set))
    api=LocalAPI(config)
    store=None;delivery=None;registry=None;application=None;operations=None
    initialized=False
    try:
        await api.start()
        application=build_application(config)
        for attempt in range(12):
            try:
                await application.initialize()
                initialized=True
                break
            except NetworkError:
                if stop.is_set() or attempt == 11:
                    raise
                await asyncio.sleep(2)
        application.bot_data['stop_event']=stop
        await configure_command_menu(application.bot)
        # Polling e webhook sao mutuamente exclusivos; nunca descarta updates pendentes.
        webhook=await application.bot.get_webhook_info()
        if webhook.url:
            log.warning('webhook anterior removido para habilitar polling')
        await application.bot.delete_webhook(drop_pending_updates=False)
        store=Store(config.database)
        for user in store.db.execute('SELECT chat_id,language FROM users WHERE language IS NOT NULL AND blocked=0'):
            await configure_command_menu(application.bot,user['language'],user['chat_id'])
        delivery=DeliveryService(config,store,application.bot)
        registry=MonitorRegistry(config,store,delivery)
        install_commands(application,registry)
        await delivery.start()
        await registry.start()
        operations=Operations(config,registry,delivery);operations.start()
        await application.start()
        def polling_error(error):
            asyncio.create_task(application.process_error(update=None,error=error))
        await application.updater.start_polling(allowed_updates=['message','callback_query'],drop_pending_updates=False,
                                                error_callback=polling_error,bootstrap_retries=0)
        log.info('bot pronto version=%s username=%s polling unico API=%s',VERSION,application.bot.username,config.api_mode)
        print(f'Bot iniciado. Toda a interacao esta no Telegram. Logs: {LOGS / "bot.log"}.',flush=True)
        while not stop.is_set():
            for task in operations.tasks:
                if task.done():
                    task.result()
                    raise RuntimeError('A background operations service stopped unexpectedly.')
            if registry.resource_task.done():
                registry.resource_task.result()
                raise RuntimeError('Resource monitoring stopped unexpectedly.')
            if api.process and api.process.poll() is not None:
                raise RuntimeError('O servidor local encerrou inesperadamente.')
            for task in delivery.tasks:
                if task.done():
                    task.result()
                    raise RuntimeError('Um servico de midia encerrou inesperadamente.')
            try:
                await asyncio.wait_for(stop.wait(),timeout=1)
            except asyncio.TimeoutError:
                pass
        if application.bot_data.get('fatal_error'):
            raise RuntimeError(application.bot_data['fatal_error'])
    finally:
        log.info('encerramento coordenado iniciado')
        try:
            phases=[]
            if application and application.updater.running:
                phases.append(application.updater.stop)
            if application and application.running:
                phases.append(application.stop)
            if operations:phases.append(operations.close)
            if registry:
                phases.append(registry.close)
            if delivery:
                phases.append(delivery.close)
            if initialized:
                phases.append(application.shutdown)
            for phase in phases:
                try:
                    await phase()
                except Exception as exc:
                    log.error('falha no encerramento fase=%s erro=%s',phase.__qualname__,type(exc).__name__)
        finally:
            if store:
                store.close()
            try:
                await api.close()
            finally:
                for sig,handler in previous.items():
                    signal.signal(sig,handler)
        log.info('encerramento concluido')


def main():
    os.chdir(ROOT)
    configure_runtime()
    load_dotenv(ROOT/'.env',override=False)
    config=None
    try:
        config=Config.from_env()
        with InstanceGuard(config.token):
            configure_logging([os.environ.get(k,'') for k in
                               ('TELEGRAM_BOT_TOKEN','TELEGRAM_API_HASH','TELEGRAM_API_ID')])
            install_process_job()
            if not media_tool('ffmpeg') or not media_tool('ffprobe'):
                raise RuntimeError('FFmpeg/FFprobe ausentes em tools/ffmpeg/bin.')
            asyncio.run(run(config))
        return 0
    except Exception as exc:
        # Excecoes de HTTP podem conter o token. Somente mensagens locais conhecidas vao ao console.
        message=str(exc) if isinstance(exc,(ValueError,RuntimeError)) else type(exc).__name__
        if config and config.token in message:
            message=type(exc).__name__
        if logging.getLogger().handlers:
            log.error('inicializacao/execucao falhou erro=%s detalhe=%s',type(exc).__name__,message)
        print(f'Erro: {message}. Consulte {LOGS / "bot.log"}.',file=sys.stderr)
        return 1


if __name__=='__main__':
    raise SystemExit(main())
