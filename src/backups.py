"""Local, verified SQLite snapshots; no credentials are written to the archive."""
from contextlib import closing
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import zipfile


def verify_archive(path,destination=None):
    with zipfile.ZipFile(path) as archive:
        if archive.testzip() is not None:raise ValueError('backup_crc_failed')
        manifest=json.loads(archive.read('manifest.json'))
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(destination) if destination is not None else Path(tmp)
            root.mkdir(parents=True,exist_ok=True)
            if destination is not None and any(root.iterdir()):raise ValueError('restore_directory_must_be_empty')
            for name,digest in manifest['sha256'].items():
                relative=Path(name)
                if relative.is_absolute() or '..' in relative.parts or '\\' in name:raise ValueError('invalid_backup_path')
                target=root/relative;target.parent.mkdir(parents=True,exist_ok=True)
                with archive.open(name) as source,target.open('wb') as out:
                    checksum=hashlib.sha256()
                    while chunk:=source.read(1024*1024):checksum.update(chunk);out.write(chunk)
                if checksum.hexdigest()!=digest:raise ValueError('backup_digest_failed')
            for name in ('database/bot.sqlite3','database/sheets/sheets.sqlite3'):
                with closing(sqlite3.connect(root/name)) as db:
                    if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('backup_database_invalid')
            return manifest


def create_backup(config):
    database=config.database.parent;folder=database/'backups';folder.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.snapshot-',dir=folder) as tmp:
        snapshot=Path(tmp);files={}
        for relative in ('bot.sqlite3','sheets/sheets.sqlite3'):
            source=database/relative;target=snapshot/relative;target.parent.mkdir(parents=True,exist_ok=True)
            with closing(sqlite3.connect(source.as_uri()+'?mode=ro',uri=True)) as src,closing(sqlite3.connect(target)) as dst:src.backup(dst)
            files['database/'+relative]=target
        with closing(sqlite3.connect(snapshot/'bot.sqlite3')) as db:
            pending=db.execute("SELECT raw_path FROM clips WHERE status NOT IN ('done','expired') UNION SELECT m.path FROM media m JOIN clips c ON c.id=m.clip_id WHERE c.status NOT IN ('done','expired') AND m.file_id IS NULL").fetchall()
        for (name,) in pending:
            if not name:continue
            path=Path(name).resolve()
            if not path.is_relative_to(config.spool.resolve()):raise ValueError('backup_media_outside_spool')
            if path.is_file():files['cache/spool/'+path.relative_to(config.spool.resolve()).as_posix()]=path
        for path in (database/'sheets/images').glob('*.jpg'):
            if not path.is_symlink():files['database/sheets/images/'+path.name]=path
        root=Path(__file__).resolve().parents[1]
        for name in ('settings.env','compose.yaml','requirements.txt','Dockerfile','main.py'):
            path=root/name
            if path.is_file():files['project/'+name]=path
        for path in (root/'src').glob('*.py'):files['project/src/'+path.name]=path
        estimated=sum(path.stat().st_size for path in files.values())
        if shutil.disk_usage(folder).free<2*estimated+config.spool_bytes+1024**3:raise OSError('backup_insufficient_disk_margin')
        stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        temporary=folder/('production-'+stamp+'.partial');final=temporary.with_suffix('.zip')
        manifest=dict(created_at=stamp,format=1,sha256={},excluded=['credentials','sessions','in_progress_captures'])
        try:
            with zipfile.ZipFile(temporary,'w',compression=zipfile.ZIP_STORED) as archive:
                for name,path in files.items():
                    checksum=hashlib.sha256()
                    with path.open('rb') as source,archive.open(name,'w',force_zip64=True) as out:
                        while chunk:=source.read(1024*1024):checksum.update(chunk);out.write(chunk)
                    manifest['sha256'][name]=checksum.hexdigest()
                archive.writestr('manifest.json',json.dumps(manifest))
            verify_archive(temporary)
            temporary.replace(final)
            for old in sorted(folder.glob('production-*.zip'))[:-3]:old.unlink()
            return final
        finally:temporary.unlink(missing_ok=True)


if __name__=='__main__':
    import argparse
    from project_runtime import configure_runtime
    configure_runtime()
    parser=argparse.ArgumentParser(description='Verify a local backup or extract it into an empty recovery directory.')
    parser.add_argument('archive',type=Path)
    parser.add_argument('--restore-to',type=Path)
    args=parser.parse_args()
    result=verify_archive(args.archive,args.restore_to)
    print(json.dumps(dict(verified=True,created_at=result['created_at'],files=len(result['sha256']))))
