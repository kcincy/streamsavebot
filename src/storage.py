"""Metadados SQLite. A conexao pertence exclusivamente ao loop do bot."""
import json
from contextlib import nullcontext
import logging
from pathlib import Path
import sqlite3
import time
import uuid
from platforms import profile_label,split_profile

log = logging.getLogger(__name__)
ACTIVE = "('pending','retry','sending')"


def relocate_media_paths(db, before: Path, after: Path):
    """Move as referencias, sem alterar IDs, estados ou destinatarios da fila."""
    before,after=before.resolve(),after.resolve()
    with db:
        for table,column in (('clips','raw_path'),('media','path')):
            for row in db.execute(f'SELECT id,{column} FROM {table} WHERE {column} IS NOT NULL').fetchall():
                path=Path(row[1]).resolve()
                if path.is_relative_to(before):
                    db.execute(f'UPDATE {table} SET {column}=? WHERE id=?',
                               (str(after/path.relative_to(before)),row[0]))


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS users(
                user_id INTEGER PRIMARY KEY, chat_id INTEGER NOT NULL, blocked INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS streamers(
                username TEXT PRIMARY KEY, platform_id TEXT NOT NULL DEFAULT '');
            CREATE TABLE IF NOT EXISTS platform_sessions(
                profile TEXT PRIMARY KEY REFERENCES streamers(username), token TEXT NOT NULL,
                live_id TEXT NOT NULL, online INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS daily_usage(
                user_id INTEGER REFERENCES users, day TEXT NOT NULL, seconds REAL NOT NULL DEFAULT 0,
                PRIMARY KEY(user_id,day));
            CREATE TABLE IF NOT EXISTS capture_parts(
                id TEXT PRIMARY KEY, profile TEXT NOT NULL, members TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS subscriptions(
                user_id INTEGER REFERENCES users, username TEXT REFERENCES streamers,
                generation TEXT NOT NULL, PRIMARY KEY(user_id,username));
            CREATE TABLE IF NOT EXISTS clips(
                id TEXT PRIMARY KEY, username TEXT NOT NULL, live_id TEXT NOT NULL,
                started REAL NOT NULL, expires REAL NOT NULL, raw_path TEXT,
                metadata TEXT NOT NULL, status TEXT NOT NULL, error TEXT NOT NULL DEFAULT '',
                part_no INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS clip_recipients(
                clip_id TEXT REFERENCES clips, user_id INTEGER REFERENCES users,
                chat_id INTEGER NOT NULL, generation TEXT NOT NULL, PRIMARY KEY(clip_id,user_id));
            CREATE TABLE IF NOT EXISTS stop_requests(
                user_id INTEGER REFERENCES users, generation TEXT NOT NULL, clip_id TEXT NOT NULL,
                PRIMARY KEY(clip_id,user_id,generation));
            CREATE TABLE IF NOT EXISTS media(
                id TEXT PRIMARY KEY, clip_id TEXT REFERENCES clips, piece INTEGER NOT NULL,
                path TEXT, kind TEXT NOT NULL, size INTEGER NOT NULL, file_id TEXT,
                UNIQUE(clip_id,piece));
            CREATE TABLE IF NOT EXISTS deliveries(
                media_id TEXT REFERENCES media, user_id INTEGER REFERENCES users,
                chat_id INTEGER NOT NULL, generation TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt REAL NOT NULL DEFAULT 0, message_id INTEGER,
                error TEXT NOT NULL DEFAULT '', PRIMARY KEY(media_id,user_id));
            CREATE INDEX IF NOT EXISTS delivery_pending ON deliveries(status,next_attempt);
            CREATE INDEX IF NOT EXISTS clip_status ON clips(status,started);
            CREATE INDEX IF NOT EXISTS clip_stream ON clips(username,live_id);
            CREATE INDEX IF NOT EXISTS delivery_user ON deliveries(user_id,status);
            CREATE INDEX IF NOT EXISTS subscription_stream ON subscriptions(username);
            CREATE INDEX IF NOT EXISTS recipient_user ON clip_recipients(user_id,clip_id);
        """)
        if 'language' not in {r[1] for r in self.db.execute('PRAGMA table_info(users)')}:
            with self.db:
                self.db.execute("ALTER TABLE users ADD COLUMN language TEXT CHECK(language IN ('en','pt'))")
        if 'last_delivery' not in {r[1] for r in self.db.execute('PRAGMA table_info(users)')}:
            with self.db:self.db.execute('ALTER TABLE users ADD COLUMN last_delivery REAL NOT NULL DEFAULT 0')
        if 'part_no' not in {r[1] for r in self.db.execute('PRAGMA table_info(clips)')}:
            with self.db:
                self.db.execute('ALTER TABLE clips ADD COLUMN part_no INTEGER NOT NULL DEFAULT 0')
        if 'info' not in {r[1] for r in self.db.execute('PRAGMA table_info(media)')}:
            with self.db:
                self.db.execute("ALTER TABLE media ADD COLUMN info TEXT NOT NULL DEFAULT '{}'")
        columns = {r[1] for r in self.db.execute('PRAGMA table_info(subscriptions)')}
        with self.db:
            for name,definition in (('enabled','INTEGER NOT NULL DEFAULT 1'),
                                    ('stopped_live_id',"TEXT NOT NULL DEFAULT ''")):
                if name not in columns:
                    self.db.execute(f'ALTER TABLE subscriptions ADD COLUMN {name} {definition}')
        columns={r[1] for r in self.db.execute('PRAGMA table_info(streamers)')}
        with self.db:
            for name in ('display_name','profile_username','shared_id'):
                if name not in columns:self.db.execute(f"ALTER TABLE streamers ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")

    def profile_name(self,key):
        if split_profile(key)[0]!='superlive':return ''
        row=self.db.execute('SELECT display_name FROM streamers WHERE username=?',(key,)).fetchone()
        return row[0] if row else ''

    def profile_label(self,key,with_id=False):
        name=self.profile_name(key)
        platform,handle=split_profile(key)
        if with_id and platform=='superlive':
            uid=self.platform_id(key) or (handle.split('/',1)[1] if handle.startswith('profile/') else '')
            if uid:name=f'{name or handle} ({uid})'
        return profile_label(key,name)

    def superlive_alias(self,key,user=None,owner_id=None):
        platform,handle=split_profile(key)
        if platform!='superlive':return key
        uid=str((user or {}).get('user_id') or (handle.split('/',1)[1] if handle.startswith('profile/') else ''))
        shared=handle.split('/',1)[1] if handle.startswith('shared/') else ''
        name=str((user or {}).get('username') or (handle if '/' not in handle else ''))
        rows=self.db.execute("SELECT username,platform_id,profile_username,shared_id FROM streamers "
            "WHERE instr(username,':')=0 OR username LIKE 'superlive:%'").fetchall()
        if owner_id is not None:
            owned=set(self.subscriptions(owner_id))
            if key in owned:return key
            rows.sort(key=lambda row:row['username'] not in owned)
        if not uid:
            uid=next((row['platform_id'] for row in rows if row['username']==key),'')
        for row in rows:
            if uid and row['platform_id']==uid:return row['username']
            if shared and row['shared_id']==shared:return row['username']
        for row in rows:
            if name and name.casefold() in (row['username'].casefold(),row['profile_username'].casefold()):
                if not uid or not row['platform_id'] or row['platform_id']==uid:return row['username']
        return key

    def update_superlive_profile(self,key,user):
        uid=str(user.get('user_id') or '')
        if not uid:raise ValueError('Profile identity unavailable.')
        row=self.db.execute('SELECT platform_id,display_name,profile_username,shared_id FROM streamers WHERE username=?',(key,)).fetchone()
        if row is None:return
        if row['platform_id'] and row['platform_id']!=uid:raise ValueError('The profile belongs to another account.')
        values=(uid,' '.join(str(user.get('name',row['display_name']) or '').split())[:128],
                str(user.get('username',row['profile_username']) or ''),str(user.get('shared_id',row['shared_id']) or ''))
        if tuple(row)==values:return
        with self.db:self.db.execute('UPDATE streamers SET platform_id=?,display_name=?,profile_username=?,shared_id=? WHERE username=?',(*values,key))

    def user(self, user_id, chat_id):
        with self.db:
            self.db.execute("INSERT INTO users(user_id,chat_id) VALUES(?,?) "
                            "ON CONFLICT(user_id) DO UPDATE SET chat_id=excluded.chat_id,blocked=0",
                            (user_id, chat_id))

    def add(self, user_id, chat_id, username):
        self.user(user_id, chat_id)
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO streamers(username) VALUES(?)", (username,))
            return self.db.execute("INSERT OR IGNORE INTO subscriptions(user_id,username,generation) VALUES(?,?,?)",
                                   (user_id, username, uuid.uuid4().hex)).rowcount > 0

    def language(self,user_id):
        row=self.db.execute('SELECT language FROM users WHERE user_id=?',(user_id,)).fetchone()
        return row['language'] if row and row['language'] else 'en'

    def set_language(self,user_id,language):
        if language not in ('en','pt'):raise ValueError('unsupported_language')
        with self.db:
            self.db.execute('UPDATE users SET language=? WHERE user_id=?',(language,user_id))

    def remove(self, user_id, username):
        with self.db:
            self.db.execute('DELETE FROM stop_requests WHERE user_id=? AND generation IN '
                            '(SELECT generation FROM subscriptions WHERE user_id=? AND username=?)',
                            (user_id,user_id,username))
            removed = self.db.execute("DELETE FROM subscriptions WHERE user_id=? AND username=?",
                                      (user_id, username)).rowcount
            self.db.execute("UPDATE deliveries SET status='cancelled' WHERE user_id=? AND media_id IN "
                            "(SELECT m.id FROM media m JOIN clips c ON c.id=m.clip_id WHERE c.username=?) "
                            "AND status IN ('pending','retry')", (user_id, username))
        return bool(removed)

    def block(self, user_id):
        with self.db:
            self.db.execute("UPDATE users SET blocked=1 WHERE user_id=?", (user_id,))
            self.db.execute("UPDATE deliveries SET status='blocked' WHERE user_id=? AND "
                            "status IN ('pending','retry','sending')", (user_id,))

    def streamers(self):
        return [r[0] for r in self.db.execute("SELECT DISTINCT s.username FROM subscriptions s "
                "JOIN users u USING(user_id) WHERE u.blocked=0 AND s.enabled=1 ORDER BY s.username")]

    def subscriptions(self, user_id):
        return [r[0] for r in self.db.execute("SELECT username FROM subscriptions WHERE user_id=? ORDER BY username", (user_id,))]

    def active_subscriptions(self, user_id):
        return [r[0] for r in self.db.execute(
            'SELECT username FROM subscriptions WHERE user_id=? AND enabled=1', (user_id,))]

    def subscription(self, user_id, username=None, generation=None):
        return self.db.execute('SELECT * FROM subscriptions WHERE user_id=? AND '+
            ('username=?' if username is not None else 'generation=?'),
            (user_id,username if username is not None else generation)).fetchone()

    def set_enabled(self, user_id, username, enabled):
        with self.db:
            self.db.execute("UPDATE subscriptions SET enabled=?,stopped_live_id=CASE WHEN ? THEN '' ELSE stopped_live_id END "
                            "WHERE user_id=? AND username=?",(int(enabled),int(enabled),user_id,username))

    def stop_live(self, user_id, username, live_id, clip_id):
        with self.db:
            self.db.execute('UPDATE subscriptions SET stopped_live_id=? WHERE user_id=? AND username=?',
                            (live_id,user_id,username))
            if clip_id:
                self.db.execute('INSERT OR IGNORE INTO stop_requests SELECT user_id,generation,? FROM subscriptions '
                                'WHERE user_id=? AND username=?',(clip_id,user_id,username))

    def recipient_rows(self, username, live_id=None, clip_id=''):
        rows=list(self.db.execute(
            'SELECT u.user_id,u.chat_id,s.generation,EXISTS(SELECT 1 FROM stop_requests r '
            'WHERE r.user_id=s.user_id AND r.generation=s.generation AND r.clip_id=:clip) AS tail '
            'FROM subscriptions s JOIN users u USING(user_id) '
            'WHERE s.username=:profile AND u.blocked=0 AND ((s.enabled=1 AND '
            '(:live IS NULL OR s.stopped_live_id<>:live)) OR EXISTS(SELECT 1 FROM stop_requests r '
            'WHERE r.user_id=s.user_id AND r.generation=s.generation AND r.clip_id=:clip))',
            dict(profile=username,live=live_id,clip=clip_id)))
        snapshot=self.capture_snapshot(clip_id,username) if clip_id else None
        if snapshot is not None:
            members={(m['user_id'],m['generation']):m for m in snapshot}
            rows=[r for r in rows if (r['user_id'],r['generation']) in members
                  and (members[r['user_id'],r['generation']].get('active',True) or r['tail'])]
        return rows

    def recipients(self, username, live_id=None, clip_id=''):
        return {r['user_id']:r['chat_id'] for r in self.recipient_rows(username,live_id,clip_id)}

    def capture_snapshot(self, part_id, profile):
        row=self.db.execute('SELECT profile,members FROM capture_parts WHERE id=?',(part_id,)).fetchone()
        return (json.loads(row['members']) if row['profile']==profile else []) if row else None

    def begin_capture(self, profile, part_id, live_id):
        members=[dict(row,active=True) for row in self.recipient_rows(profile,live_id)]
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO capture_parts VALUES(?,?,?)',(part_id,profile,json.dumps(members)))

    def capture_users(self, profile, part_id, live_id):
        rows=self.recipient_rows(profile,live_id)
        snapshot=self.capture_snapshot(part_id,profile)
        if snapshot is not None:
            active={(m['user_id'],m['generation']) for m in snapshot if m.get('active',True)}
            rows=[r for r in rows if (r['user_id'],r['generation']) in active]
        return rows

    def leave_capture(self, user_id, profile, part_id):
        snapshot=self.capture_snapshot(part_id,profile)
        if snapshot is not None:
            for member in snapshot:
                if member['user_id']==user_id:member['active']=False
            with self.db:
                self.db.execute('UPDATE capture_parts SET members=? WHERE id=?',(json.dumps(snapshot),part_id))

    def request_tail(self, user_id, profile, part_id):
        if not part_id:return False
        with self.db:
            return self.db.execute('INSERT OR IGNORE INTO stop_requests SELECT user_id,generation,? '
                'FROM subscriptions WHERE user_id=? AND username=?',(part_id,user_id,profile)).rowcount>0

    def quota_used(self, user_id, day):
        row=self.db.execute('SELECT seconds FROM daily_usage WHERE user_id=? AND day=?',(user_id,day)).fetchone()
        return row[0] if row else 0.0

    def quota_used_between(self,user_id,start,end):
        return self.db.execute('SELECT COALESCE(SUM(seconds),0) FROM daily_usage '
            'WHERE user_id=? AND day>=? AND day<?',(user_id,start,end)).fetchone()[0]

    def charge_quota(self, user_id, day, seconds, *,commit=True):
        if seconds<=0:return
        with self.db if commit else nullcontext():
            self.db.execute('INSERT INTO daily_usage VALUES(?,?,?) ON CONFLICT(user_id,day) '
                            'DO UPDATE SET seconds=seconds+excluded.seconds',(user_id,day,seconds))

    def platform_id(self, username):
        row = self.db.execute("SELECT platform_id FROM streamers WHERE username=?", (username,)).fetchone()
        return row[0] if row else ''

    def observed_live(self, profile, token, online):
        row=self.db.execute('SELECT * FROM platform_sessions WHERE profile=?',(profile,)).fetchone()
        live_id=(row['live_id'] if row and (not online or (row['online'] and row['token']==token))
                 else uuid.uuid4().hex)
        with self.db:
            self.db.execute('INSERT INTO platform_sessions VALUES(?,?,?,?) ON CONFLICT(profile) DO UPDATE '
                            'SET token=excluded.token,live_id=excluded.live_id,online=excluded.online',
                            (profile,token,live_id,int(online)))
        return live_id

    def fix_identity(self, username, platform_id):
        current = self.platform_id(username)
        if current and current != platform_id:
            raise ValueError("O username passou a apontar para outra conta.")
        with self.db:
            self.db.execute("UPDATE streamers SET platform_id=? WHERE username=?", (platform_id, username))

    def register_clip(self, clip_id, username, live_id, started, path, metadata, retention):
        with self.db:
            number = self.db.execute('SELECT COALESCE(MAX(part_no),0)+1 FROM clips WHERE username=? AND live_id=?',
                                     (username,live_id)).fetchone()[0]
            self.db.execute("INSERT INTO clips(id,username,live_id,started,expires,raw_path,metadata,status,part_no) "
                "VALUES(?,?,?,?,?,?,?,'raw',?)",
                (clip_id, username, live_id, started, time.time()+retention, str(path), json.dumps(metadata),number))
            recipients=self.recipient_rows(username,live_id,clip_id)
            self.db.executemany('INSERT INTO clip_recipients VALUES(?,?,?,?)',
                [(clip_id,r['user_id'],r['chat_id'],r['generation']) for r in recipients])

    def processed(self, clip_id, outputs, video_info=None):
        with self.db:
            for index, (path, kind) in enumerate(outputs):
                media_id = f'{clip_id}:{index}'
                self.db.execute("INSERT INTO media(id,clip_id,piece,path,kind,size,info) VALUES(?,?,?,?,?,?,?)",
                    (media_id, clip_id, index, str(path), kind, path.stat().st_size,
                     json.dumps((video_info or {}).get(str(path),{}))))
                self.db.execute("INSERT INTO deliveries(media_id,user_id,chat_id,generation) "
                    "SELECT ?,r.user_id,r.chat_id,r.generation FROM clip_recipients r "
                    "JOIN subscriptions s ON s.user_id=r.user_id AND s.generation=r.generation "
                    "JOIN users u ON u.user_id=r.user_id WHERE r.clip_id=? AND u.blocked=0",
                    (media_id, clip_id))
            self.db.execute("UPDATE clips SET status='ready' WHERE id=?", (clip_id,))

    def pending_clip(self):
        return self.db.execute("SELECT c.* FROM clips c WHERE c.status='raw' ORDER BY "
            "EXISTS(SELECT 1 FROM stop_requests r WHERE r.clip_id=c.id) DESC,c.started,c.id LIMIT 1").fetchone()

    def set_clip(self, clip_id, status, error=''):
        with self.db:
            self.db.execute("UPDATE clips SET status=?,error=? WHERE id=?", (status,error,clip_id))

    def next_delivery(self):
        return self.db.execute(f"""
            SELECT d.*,m.path,m.kind,m.file_id,m.piece,m.info,c.username,c.live_id,c.started,c.expires,c.id AS clip_id,c.part_no,
                (SELECT COUNT(*) FROM media siblings WHERE siblings.clip_id=c.id) AS pieces
            FROM deliveries d JOIN media m ON m.id=d.media_id JOIN clips c ON c.id=m.clip_id
            JOIN users u ON u.user_id=d.user_id
            JOIN subscriptions s ON s.user_id=d.user_id AND s.username=c.username AND s.generation=d.generation
            WHERE d.status IN ('pending','retry') AND d.next_attempt<=? AND c.status='ready' AND u.blocked=0
            AND NOT EXISTS (
                SELECT 1 FROM deliveries older JOIN media om ON om.id=older.media_id
                JOIN clips oc ON oc.id=om.clip_id
                WHERE older.user_id=d.user_id AND oc.username=c.username AND older.status IN {ACTIVE}
                AND (oc.started<c.started OR (oc.id=c.id AND om.piece<m.piece)))
            ORDER BY EXISTS(SELECT 1 FROM stop_requests r WHERE r.clip_id=c.id AND r.user_id=d.user_id
                            AND r.generation=d.generation) DESC,u.last_delivery,c.started,m.piece,d.user_id LIMIT 1
        """, (time.time(),)).fetchone()

    def pending_video_metadata(self):
        return list(self.db.execute("SELECT m.* FROM media m JOIN clips c ON c.id=m.clip_id "
            "WHERE c.status='ready' AND m.kind='video' AND m.info='{}' AND m.file_id IS NULL"))

    def video_metadata(self, media_id, path, info):
        with self.db:
            self.db.execute('UPDATE media SET path=?,size=?,info=? WHERE id=?',
                            (str(path),path.stat().st_size,json.dumps(info),media_id))

    def fail_media(self, media_id, error):
        with self.db:
            self.db.execute("UPDATE deliveries SET status='failed',error=? WHERE media_id=? "
                            "AND status IN ('pending','retry')",(error,media_id))

    def unconfirmed_uploads(self):
        return self.db.execute("SELECT 1 FROM media m JOIN clips c ON c.id=m.clip_id "
            "JOIN deliveries d ON d.media_id=m.id WHERE c.status NOT IN ('done','expired') "
            "AND m.file_id IS NULL AND d.status IN ('pending','retry','sending','failed','uncertain') LIMIT 1").fetchone() is not None

    def sending(self, row):
        with self.db:
            self.db.execute('UPDATE users SET last_delivery=? WHERE user_id=?',(time.time(),row['user_id']))
            self.db.execute("UPDATE deliveries SET status='sending',attempts=attempts+1 WHERE media_id=? AND user_id=?",
                            (row['media_id'],row['user_id']))

    def delivery(self, row, status, *, delay=0, error='', message_id=None, file_id=None):
        with self.db:
            if file_id:
                self.db.execute("UPDATE media SET file_id=? WHERE id=?", (file_id,row['media_id']))
            self.db.execute("UPDATE deliveries SET status=?,next_attempt=?,error=?,message_id=? "
                            "WHERE media_id=? AND user_id=?",
                            (status,time.time()+delay,error,message_id,row['media_id'],row['user_id']))

    def cleanup_candidates(self):
        return list(self.db.execute(f"""SELECT c.* FROM clips c
            WHERE c.status='ready' AND NOT EXISTS (
                SELECT 1 FROM deliveries d JOIN media m ON m.id=d.media_id
                WHERE m.clip_id=c.id AND (d.status IN {ACTIVE} OR
                (d.status IN ('failed','uncertain') AND m.file_id IS NULL)))"""))

    def expire(self, clip_id):
        with self.db:
            self.db.execute("UPDATE deliveries SET status='expired' WHERE media_id IN "
                            "(SELECT id FROM media WHERE clip_id=?) AND status IN ('pending','retry')", (clip_id,))

    def recover(self):
        with self.db:
            uncertain = self.db.execute("UPDATE deliveries SET status='uncertain',error='restart_during_send' "
                                        "WHERE status='sending'").rowcount
            self.db.execute("UPDATE clips SET status='raw' WHERE status='processing'")
        if uncertain:
            log.error("reinicio envios_sem_confirmacao=%s; nao reenviados para evitar duplicacao", uncertain)

    def active_clips(self):
        return list(self.db.execute("SELECT * FROM clips WHERE status IN ('raw','processing','ready','failed')"))

    def delivery_status(self, user_id, username):
        states = dict(self.db.execute("SELECT d.status,COUNT(*) FROM deliveries d JOIN media m ON m.id=d.media_id "
            "JOIN clips c ON c.id=m.clip_id WHERE d.user_id=? AND c.username=? GROUP BY d.status", (user_id,username)))
        queued=self.db.execute("SELECT COUNT(*) FROM clips c JOIN clip_recipients r ON r.clip_id=c.id "
            "WHERE r.user_id=? AND c.username=? AND c.status IN ('raw','processing')",(user_id,username)).fetchone()[0]
        if queued:states['pending']=states.get('pending',0)+queued
        missing = self.db.execute("SELECT COUNT(*) FROM clips c JOIN clip_recipients r ON r.clip_id=c.id "
            "WHERE r.user_id=? AND c.username=? AND c.status IN ('failed','expired') "
            "AND NOT EXISTS(SELECT 1 FROM media m WHERE m.clip_id=c.id)",(user_id,username)).fetchone()[0]
        if missing:
            states['failed'] = states.get('failed',0)+missing
        return states

    def statistics(self,user_id):
        """Historico individual; cada segmento e somado uma vez mesmo com captura compartilhada."""
        clips=self.db.execute('SELECT c.id,c.username,c.live_id,c.started,c.metadata FROM clips c '
                              'JOIN clip_recipients r ON r.clip_id=c.id WHERE r.user_id=? ORDER BY c.started',(user_id,))
        media=self.db.execute('SELECT m.clip_id,m.size,m.info,d.status AS delivery_status '
            'FROM media m JOIN clips c ON c.id=m.clip_id JOIN clip_recipients r ON r.clip_id=c.id '
            'LEFT JOIN deliveries d ON d.media_id=m.id AND d.user_id=r.user_id WHERE r.user_id=?',(user_id,))
        by_clip={}
        for row in media:by_clip.setdefault(row['clip_id'],[]).append(row)
        seconds=volume=sent_seconds=sent_bytes=received=estimated=0
        profiles={};lives=set();longest=0;first=last=None
        for clip in clips:
            parts=by_clip.get(clip['id'],[])
            if not parts or all(p['delivery_status']=='skipped' for p in parts):continue
            metadata=json.loads(clip['metadata'])
            durations=[]
            known=[]
            for part in parts:
                info=json.loads(part['info'])
                duration=info.get('duration_seconds',info.get('duration'))
                durations.append(max(0,float(duration or 0)));known.append(duration is not None)
            if not all(known):
                fallback=max(0,float(metadata.get('seconds',0))-sum(d for d,k in zip(durations,known) if k))
                missing=sum(p['size'] for p,k in zip(parts,known) if not k)
                durations=[d if k else fallback*p['size']/missing if missing else 0
                           for p,k,d in zip(parts,known,durations)]
                estimated+=1
            total=sum(durations)
            seconds+=total;volume+=sum(p['size'] for p in parts)
            profiles[clip['username']]=profiles.get(clip['username'],0)+total
            lives.add((clip['username'],clip['live_id']))
            first=clip['started'] if first is None else min(first,clip['started'])
            last=clip['started'] if last is None else max(last,clip['started'])
            for part,duration in zip(parts,durations):
                if part['delivery_status']=='sent':
                    received+=1;sent_bytes+=part['size'];sent_seconds+=duration;longest=max(longest,duration)
        subscriptions=self.db.execute('SELECT COUNT(*),COALESCE(SUM(enabled),0) FROM subscriptions '
                                      'WHERE user_id=?',(user_id,)).fetchone()
        queue=dict(self.db.execute('SELECT status,COUNT(*) FROM deliveries WHERE user_id=? GROUP BY status',(user_id,)))
        queued=self.db.execute("SELECT COUNT(*) FROM clips c JOIN clip_recipients r ON r.clip_id=c.id "
            "WHERE r.user_id=? AND c.status IN ('raw','processing')",(user_id,)).fetchone()[0]
        return dict(seconds=seconds,bytes=volume,received=received,sent_bytes=sent_bytes,sent_seconds=sent_seconds,
                    lives=len(lives),profiles=len(profiles),longest=longest,first=first,last=last,
                    registered=subscriptions[0],enabled=subscriptions[1],estimated=estimated,
                    pending=queued+sum(queue.get(s,0) for s in ('pending','retry','sending')),
                    failures=sum(queue.get(s,0) for s in ('failed','uncertain','expired')),
                    top=sorted(profiles.items(),key=lambda item:(-item[1],item[0]))[:3])

    def close(self):
        self.db.close()
