"""Importa uma cópia do projeto parado para os volumes Linux, sem alterar a origem."""
from contextlib import closing
from pathlib import Path,PureWindowsPath
import json
import os
import shutil
import sqlite3
import sys
import tempfile

from dotenv import dotenv_values

ROOT=Path(__file__).resolve().parents[1]


def rebase_paths(db,spool):
    spool=spool.resolve();changed=0
    with db:
        for table,column in (('clips','raw_path'),('media','path')):
            for ident,value in db.execute(f'SELECT id,{column} FROM {table} WHERE {column} IS NOT NULL').fetchall():
                parts=PureWindowsPath(value).parts
                for index in range(len(parts)-2):
                    if (parts[index].casefold(),parts[index+1].casefold())!=('cache','spool'):continue
                    tail=parts[index+2:]
                    if tail[0]!=ident.split(':',1)[0]:continue
                    target=spool.joinpath(*tail).resolve()
                    if '..' in tail or not target.is_relative_to(spool):
                        raise ValueError('Caminho de mídia fora do spool; importação cancelada.')
                    db.execute(f'UPDATE {table} SET {column}=? WHERE id=?',(str(target),ident))
                    changed+=1
                    break
    return changed


def import_runtime(source,target):
    source,target=source.resolve(),target.resolve()
    runtime=source/'data' if (source/'data'/'database'/'bot.sqlite3').is_file() else source
    database,cache=target/'database',target/'cache'
    for folder in (database,cache):folder.mkdir(parents=True,exist_ok=True)
    destinations=[database/'bot.sqlite3',database/'telegram-bot-api',database/'sessions',
                  cache/'spool',cache/'telegram-bot-api']
    if any(path.exists() for path in destinations):
        raise ValueError('Os volumes de destino já contêm dados; nada foi sobrescrito.')
    original=runtime/'database'/'bot.sqlite3'
    if not original.is_file():raise ValueError('Banco de origem não encontrado.')
    with tempfile.TemporaryDirectory(prefix='.import-',dir=database) as first,\
         tempfile.TemporaryDirectory(prefix='.import-',dir=cache) as second:
        db_stage,cache_stage=Path(first),Path(second)
        with closing(sqlite3.connect(original.as_uri()+'?mode=ro',uri=True)) as old,\
             closing(sqlite3.connect(db_stage/'bot.sqlite3')) as new:
            old.backup(new)
            if new.execute('PRAGMA integrity_check').fetchone()[0]!='ok':
                raise ValueError('O banco de origem não passou na verificação de integridade.')
            changed=rebase_paths(new,cache/'spool')
        for parent,name,stage in ((runtime/'database','telegram-bot-api',db_stage),
                                  (runtime/'cache','spool',cache_stage),
                                  (runtime/'cache','telegram-bot-api',cache_stage)):
            if (parent/name).is_dir():shutil.copytree(parent/name,stage/name)
        settings=dotenv_values(source/'settings.env',interpolate=False)
        for name,filename in (('SUPERLIVE_STATE','superlive.json'),('STRIPCHAT_STATE','stripchat.json')):
            value=(settings.get(name) or '').strip()
            if not value:continue
            relative=PureWindowsPath(value)
            if relative.drive or relative.root:
                raise ValueError(f'Use caminho relativo ao projeto em {name} antes da importação.')
            state=source.joinpath(*relative.parts).resolve()
            if not state.is_relative_to(source) or not state.is_file():
                raise ValueError(f'{name} precisa apontar para um arquivo dentro do projeto de origem.')
            json.loads(state.read_text(encoding='utf8'))
            destination=db_stage/'sessions'/filename;destination.parent.mkdir(exist_ok=True)
            shutil.copyfile(state,destination);destination.chmod(0o600)
        # O banco só aparece depois dos arquivos; uma importação incompleta não inicia o bot.
        for stage,destination in ((db_stage,database),(cache_stage,cache)):
            for path in stage.iterdir():
                if path.name!='bot.sqlite3':path.rename(destination/path.name)
        os.link(db_stage/'bot.sqlite3',database/'bot.sqlite3')
    return changed


if __name__=='__main__':
    try:
        if len(sys.argv)!=2:raise ValueError('Uso: python scripts/docker_import.py /import')
        count=import_runtime(Path(sys.argv[1]),ROOT)
        print(f'Importação concluída. {count} referências de mídia ajustadas. A origem foi preservada.')
    except (OSError,ValueError,sqlite3.Error) as exc:
        print(f'Importação não concluída: {exc}',file=sys.stderr)
        raise SystemExit(1)
