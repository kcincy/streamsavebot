"""Catalogo permanente e nove amostras por gravacao, sem decodificar o video inteiro."""
from contextlib import closing
from datetime import datetime, timezone
import math
import os
from pathlib import Path
import re
import sqlite3
import tempfile

from project_runtime import media_tool


def initialize(folder):
    folder=Path(folder)
    (folder/'images').mkdir(parents=True,exist_ok=True)
    with closing(sqlite3.connect(folder/'sheets.sqlite3')) as db, db:
        db.execute('''CREATE TABLE IF NOT EXISTS recordings (
            recording_id TEXT PRIMARY KEY, live_id TEXT NOT NULL, part_number INTEGER NOT NULL,
            title TEXT, profile_name TEXT NOT NULL, profile_url TEXT NOT NULL,
            profile_id TEXT NOT NULL, platform TEXT NOT NULL,
            sheet_filename TEXT NOT NULL CHECK(instr(sheet_filename,'/')=0 AND instr(sheet_filename,char(92))=0),
            duration_seconds REAL NOT NULL CHECK(duration_seconds>0),
            recording_started_at TEXT NOT NULL, recording_ended_at TEXT NOT NULL,
            telegram_user_ids TEXT NOT NULL CHECK(json_valid(telegram_user_ids) AND json_type(telegram_user_ids)='array'),
            video_file_count INTEGER NOT NULL, created_at TEXT NOT NULL,
            status TEXT NOT NULL, error TEXT NOT NULL DEFAULT '')''')
    return folder


def set_video_count(folder,recording_id,count):
    with closing(sqlite3.connect(Path(folder)/'sheets.sqlite3')) as db,db:
        db.execute('UPDATE recordings SET video_file_count=? WHERE recording_id=?',(count,recording_id))


def create_sheet(folder,record,outputs,info,run):
    """Uma folha cobre toda a parte capturada, mesmo quando o upload exige varios arquivos."""
    ident=record['recording_id']
    if not re.fullmatch(r'[0-9a-f]{32}',ident):raise ValueError('invalid_recording_id')
    videos=[(path,float(info[str(path)]['duration_seconds'])) for path,kind in outputs if kind=='video']
    duration=sum(seconds for _,seconds in videos)
    if not videos or any(not math.isfinite(seconds) or seconds<=0 for _,seconds in videos):
        raise ValueError('invalid_sheet_duration')
    filename=ident+'.jpg'
    values=dict(record,sheet_filename=filename,duration_seconds=duration,video_file_count=len(videos),
                created_at=datetime.now(timezone.utc).isoformat(),status='pending',error='')
    with closing(sqlite3.connect(folder/'sheets.sqlite3')) as db, db:
        columns=','.join(values)
        updates=','.join(f'{name}=excluded.{name}' for name in values if name not in ('recording_id','created_at'))
        db.execute(f'INSERT INTO recordings ({columns}) VALUES ({",".join("?" for _ in values)}) '
                   f'ON CONFLICT(recording_id) DO UPDATE SET {updates}',tuple(values.values()))
    try:
        with tempfile.TemporaryDirectory(prefix='.frames-',dir=folder/'images') as temp:
            temp=Path(temp)
            for index in range(9):
                # Centros de nove intervalos iguais evitam buscar exatamente no EOF.
                position=duration*(index+.5)/9
                for path,seconds in videos:
                    if position<seconds:break
                    position-=seconds
                frame=temp/f'{index:02d}.png'
                arguments=[media_tool('ffmpeg'),'-v','error','-xerror','-nostdin','-y','-threads','1',
                    '-ss',f'{position:.6f}','-i',str(path),'-map','0:v:0','-an',
                    '-vf',"crop='min(iw,ih*16/9/sar)':'min(ih,iw*sar*9/16)',scale=480:270,setsar=1",
                    '-frames:v','1','-threads','1','-filter_threads','1',str(frame)]
                run(arguments)
                if not frame.is_file() or not frame.stat().st_size:
                    # Videos de um unico frame podem nao ter imagem apos o seek.
                    arguments[arguments.index('-ss')+1]='0'
                    run(arguments)
                if not frame.is_file() or not frame.stat().st_size:raise RuntimeError('sheet_frame_missing')
            result=temp/'sheet.jpg'
            run([media_tool('ffmpeg'),'-v','error','-nostdin','-y','-threads','1',
                 '-i',str(temp/'%02d.png'),'-vf','tile=3x3:nb_frames=9:padding=0:margin=0',
                 '-frames:v','1','-q:v','2','-threads','1','-filter_threads','1',
                 '-map_metadata','-1',str(result)])
            if not result.is_file() or not result.stat().st_size:raise RuntimeError('sheet_image_missing')
            os.replace(result,folder/'images'/filename)
        with closing(sqlite3.connect(folder/'sheets.sqlite3')) as db, db:
            db.execute("UPDATE recordings SET status='ready',error='' WHERE recording_id=?",(ident,))
    except Exception as exc:
        with closing(sqlite3.connect(folder/'sheets.sqlite3')) as db, db:
            db.execute("UPDATE recordings SET status='failed',error=? WHERE recording_id=?",(type(exc).__name__,ident))
        raise
