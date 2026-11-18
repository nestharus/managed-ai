"""Teaching host. The venue owns the canonical notice, grants and the first visible proposal."""
import hashlib
import json
import sqlite3
from contextlib import closing


def digest(body):
    return hashlib.sha256(body.encode()).hexdigest()


class Venue:
    def __init__(self, path):
        self.path = path
        with closing(sqlite3.connect(path)) as db:
            db.executescript("""
                CREATE TABLE editions(site TEXT, edition INTEGER, notice TEXT, door TEXT,
                                     PRIMARY KEY(site, edition));
                CREATE TABLE jobs(id TEXT PRIMARY KEY, owner TEXT, site TEXT, reader TEXT, mode TEXT,
                                  contributor TEXT);
                CREATE TABLE approvals(id INTEGER PRIMARY KEY, job TEXT, reader TEXT, contributor TEXT,
                                       draft_hash TEXT, withdrawn INTEGER DEFAULT 0,
                                       UNIQUE(job, reader, contributor, draft_hash));
                CREATE TABLE collected(job TEXT PRIMARY KEY, reader TEXT, door TEXT);
                CREATE TABLE board(job TEXT PRIMARY KEY, site TEXT, edition INTEGER, notice_hash TEXT,
                                   reader TEXT, door TEXT, approval INTEGER);
            """)

    def post(self, site, notice, door):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("""INSERT INTO editions SELECT ?,COALESCE(max(edition),0)+1,?,?
                          FROM editions WHERE site=?""", (site, notice, door, site))

    def commission(self, owner, job, site, reader, mode, contributor):
        if mode not in ('history', 'current', 'unsupported-live'):
            raise ValueError('unknown currentness policy')
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?)', (job, owner, site, reader, mode, contributor))

    @staticmethod
    def encode_draft(draft):
        return json.dumps(draft, sort_keys=True, separators=(',', ':'), allow_nan=False)

    def approve(self, contributor, job, reader, draft):
        """Private contributor action. Repeating it returns the original, possibly withdrawn grant."""
        fingerprint = digest(self.encode_draft(draft))
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('BEGIN IMMEDIATE')
            target = db.execute('SELECT contributor,reader FROM jobs WHERE id=?', (job,)).fetchone()
            if target != (contributor, reader):
                raise ValueError('commission requires its named contributor and reader')
            binding = (job, reader, contributor, fingerprint)
            db.execute('INSERT OR IGNORE INTO approvals(job,reader,contributor,draft_hash) VALUES(?,?,?,?)',
                       binding)
            return db.execute('SELECT id FROM approvals WHERE job=? AND reader=? AND contributor=? '
                              'AND draft_hash=?', binding).fetchone()[0]

    def withdraw(self, contributor, approval):
        with closing(sqlite3.connect(self.path)) as db, db:
            return bool(db.execute('UPDATE approvals SET withdrawn=1 WHERE id=? AND contributor=?',
                                   (approval, contributor)).rowcount)

    def export(self, site, path):
        with closing(sqlite3.connect(self.path)) as db:
            edition, notice = db.execute('SELECT edition, notice FROM editions WHERE site=? '
                                         'ORDER BY edition DESC LIMIT 1', (site,)).fetchone()
        path.write_bytes(notice.encode())
        return edition

    def share(self, job, draft, approval=None):
        """Admit the submitted JSON value under the contributor grant and current venue policy."""
        encoded = self.encode_draft(draft)
        draft = json.loads(encoded)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('BEGIN IMMEDIATE')
            target = db.execute('SELECT site,reader,mode,contributor FROM jobs WHERE id=?', (job,)).fetchone()
            if target is None:
                return 'unknown commission'
            site, reader, mode, contributor = target
            grant = db.execute('SELECT withdrawn FROM approvals WHERE id=? AND job=? AND reader=? '
                               'AND contributor=? AND draft_hash=?',
                               (approval, job, reader, contributor, digest(encoded))).fetchone()
            if grant is None:
                return 'contributor approval required'
            if grant[0]:
                return 'disclosure withdrawn'
            if draft['issuer'] != 'venue-office' or draft['site'] != site:
                return 'wrong source'
            edition = draft['edition']
            source = db.execute('SELECT notice FROM editions WHERE site=? AND edition=?',
                                (site, edition)).fetchone()
            if source is None or digest(source[0]) != draft['source_hash']:
                return 'unknown edition'
            payload = (job, site, edition, draft['source_hash'], reader, draft['door'], approval)
            prior = db.execute('SELECT * FROM board WHERE job=?', (job,)).fetchone()
            if prior is not None:
                return 'already shared' if prior == payload else 'publication conflict'
            if mode == 'unsupported-live':
                return 'no admission contract'
            if mode == 'current':
                head, = db.execute('SELECT max(edition) FROM editions WHERE site=?', (site,)).fetchone()
                if edition != head:
                    return 'source changed'
            db.execute('INSERT INTO board VALUES(?,?,?,?,?,?,?)', payload)
            return 'shared'

    def read(self, reader):
        # The intended reader's board view, through a new connection after commit.
        with closing(sqlite3.connect(self.path)) as db:
            return db.execute('SELECT job,edition,door FROM board WHERE reader=? ORDER BY job',
                              (reader,)).fetchall()

    def active_door(self, site):
        with closing(sqlite3.connect(self.path)) as db:
            door, = db.execute('SELECT door FROM editions WHERE site=? ORDER BY edition DESC LIMIT 1',
                               (site,)).fetchone()
        return door

    def collect(self, job, reader):
        """Exercise the direction actually delivered to this reader and issue at most one parcel."""
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('BEGIN IMMEDIATE')
            item = db.execute('SELECT site,door FROM board WHERE job=? AND reader=?',
                              (job, reader)).fetchone()
            if item is None:
                return 'no direction'
            site, door = item
            active, = db.execute('SELECT door FROM editions WHERE site=? ORDER BY edition DESC LIMIT 1',
                                 (site,)).fetchone()
            if door != active:
                return 'wrong entrance'
            issued = db.execute('INSERT OR IGNORE INTO collected VALUES(?,?,?)', (job, reader, door))
            return 'parcel collected' if issued.rowcount else 'already collected'
