"""Open or upgrade an existing store; govern every command receipt and transaction."""
from contextlib import closing, contextmanager
from pathlib import Path
import json
import sqlite3

from .contract import CONTEXT_LIMIT, Refused, encoded, name, require

PREVIOUS_FORMAT = 'g60-collective-work/1'
FORMAT = 'g60-collective-work/2'

LEGACY_RECEIPTS = 'CREATE VIEW receipts AS SELECT actor,command,request,result FROM command_receipts'

QUALIFIED_REVISION_INDEX = """CREATE INDEX qualified_revision
    ON events(subject,json_extract(body,'$.target.revision'))
    WHERE kind='qualified-use-source'"""

USE_RELATIONS = """CREATE TABLE use_sources (
    target TEXT NOT NULL, source TEXT NOT NULL, pin TEXT NOT NULL,
    PRIMARY KEY(target,source))"""

REVIEW_TABLE = '''CREATE TABLE impact_reviews (
    proposal TEXT NOT NULL REFERENCES proposals(id),
    notice INTEGER NOT NULL REFERENCES notices(id),
    judgment TEXT NOT NULL, PRIMARY KEY(proposal,notice))'''

SCHEMA = '''
CREATE TABLE metadata (format TEXT NOT NULL);
CREATE TABLE artifacts (id TEXT PRIMARY KEY, record TEXT NOT NULL);
CREATE TABLE versions (id TEXT NOT NULL, revision INTEGER NOT NULL, record TEXT NOT NULL,
                       PRIMARY KEY(id,revision));
CREATE TABLE contexts (id TEXT PRIMARY KEY, actor TEXT NOT NULL, record TEXT NOT NULL);
CREATE TABLE proposals (id TEXT PRIMARY KEY, actor TEXT NOT NULL, record TEXT NOT NULL);
CREATE TABLE command_receipts (actor TEXT NOT NULL, command TEXT NOT NULL, request TEXT NOT NULL,
                       result TEXT NOT NULL, PRIMARY KEY(actor,command));
CREATE TABLE claims (target TEXT PRIMARY KEY, actor TEXT NOT NULL, generation INTEGER NOT NULL,
                     token TEXT NOT NULL, expires REAL NOT NULL, active INTEGER NOT NULL);
CREATE TABLE watches (actor TEXT NOT NULL, subject TEXT NOT NULL, reason TEXT NOT NULL,
                      PRIMARY KEY(actor,subject));
CREATE TABLE events (seq INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,
                     subject TEXT NOT NULL, actor TEXT NOT NULL, body TEXT NOT NULL);
CREATE TABLE notices (id INTEGER PRIMARY KEY AUTOINCREMENT, receiver TEXT NOT NULL,
                      target TEXT NOT NULL, kind TEXT NOT NULL, required INTEGER NOT NULL,
                      body TEXT NOT NULL, state TEXT NOT NULL, acknowledgment TEXT, receipt TEXT);
CREATE INDEX notices_receiver ON notices(receiver,state,id);
CREATE INDEX notices_target ON notices(target,required,state);
'''


def check_engine(db):
    try:
        db.execute("SELECT json_array(1) -> '$[0]'").fetchone()
    except sqlite3.OperationalError as exc:
        raise ValueError('Collective v2 requires SQLite 3.38 or newer with JSON support') from exc



class CollectiveDatabase:
    def __init__(self, database, product):
        self.path = Path(database).expanduser().resolve()
        if not self.path.is_file() or self.path.is_relative_to(product):
            raise ValueError('Use an existing runtime database outside the product directory')
        with closing(self.connect()) as db:
            check_engine(db)
            if db.execute('SELECT format FROM metadata').fetchone()[0] != FORMAT:
                raise ValueError('Unsupported collective store format')

    @staticmethod
    def initialize(database, product):
        path = Path(database).expanduser().resolve()
        if path.exists() or path.is_relative_to(product):
            raise ValueError('Initialization requires a new database outside the product directory')
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(':memory:')) as check:
            check_engine(check)
        with closing(sqlite3.connect(path)) as db, db:
            db.executescript(SCHEMA)
            db.execute(REVIEW_TABLE)
            db.execute(USE_RELATIONS)
            db.execute(QUALIFIED_REVISION_INDEX)
            db.execute(LEGACY_RECEIPTS)
            db.execute('INSERT INTO metadata VALUES (?)', (FORMAT,))
        return {'format': FORMAT, 'database': str(path)}

    @staticmethod
    def upgrade(database, product):
        """Offline v1 to v2 transition; every existing duty and receipt stays in place."""
        path = Path(database).expanduser().resolve()
        if not path.is_file() or path.is_relative_to(product):
            raise ValueError('Upgrade requires an existing database outside the product')
        with closing(sqlite3.connect(path.as_uri() + '?mode=rw', uri=True)) as db, db:
            check_engine(db)
            db.execute('BEGIN EXCLUSIVE')
            version, = db.execute('SELECT format FROM metadata').fetchone()
            if version == FORMAT:
                return {'format': FORMAT, 'changed': False}
            if version != PREVIOUS_FORMAT:
                raise ValueError('Unsupported collective store format')
            # v1 programs always insert a receipt inside their effect transaction.
            # The read-only old name fences a live v1 writer even after its earlier format check.
            db.execute('ALTER TABLE receipts RENAME TO command_receipts')
            db.execute(LEGACY_RECEIPTS)
            db.execute(REVIEW_TABLE)
            db.execute(USE_RELATIONS)
            db.execute(QUALIFIED_REVISION_INDEX)
            # Import the direct sources actually enrolled in current v1 ready decisions.
            # A past hold has no durable open commission declaration in the old format.
            db.execute("""INSERT INTO use_sources(target,source,pin)
                        SELECT a.id,json_extract(p.value,'$.id'),p.value
                        FROM artifacts a, json_each(a.record,'$.basis') p
                        WHERE json_extract(a.record,'$.body.format')='g60-receiver-use/1'
                        AND json_extract(a.record,'$.body.disposition')='ready'
                        AND a.id != json_extract(p.value,'$.id')""")
            db.execute('UPDATE metadata SET format=?', (FORMAT,))
            return {'format': FORMAT, 'changed': True}

    def connect(self):
        db = sqlite3.connect(self.path.as_uri() + '?mode=rw', uri=True, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA synchronous=FULL')
        return db

    @contextmanager
    def read(self):
        with closing(self.connect()) as db:
            db.execute('BEGIN')
            yield db

    def receipt(self, actor, command):
        with self.read() as db:
            row = db.execute('SELECT result FROM command_receipts WHERE actor=? AND command=?',
                             (name(actor), name(command))).fetchone()
            return json.loads(row[0]) if row else None

    def execute(self, request, operations):
        """Execute one exact command, recording accepted or refused results atomically."""
        require(isinstance(request, dict), 'invalid-input', 'Request must be an object')
        actor, command, op = (name(request.get(k), k) for k in ('actor', 'command', 'op'))
        raw = encoded(request)
        require(len(raw.encode()) <= CONTEXT_LIMIT, 'request-bound', 'Request exceeds 2 MiB')
        db = self.connect()
        try:
            db.execute('BEGIN IMMEDIATE')
            handlers = operations(db)
            require(op in handlers, 'unknown-operation', 'Unknown operation')
            old = db.execute('SELECT request,result FROM command_receipts WHERE actor=? AND command=?', (actor, command)).fetchone()
            if old:
                require(old['request'] == raw, 'command-conflict', 'This command identity already names a different request')
                result = json.loads(old['result'])
                db.rollback()
                return result
            # A refused command cannot leave partial operation writes behind.
            db.execute('SAVEPOINT operation')
            try:
                result = {'status': 'accepted', **handlers[op](request)}
                db.execute('RELEASE operation')
            except Refused as exc:
                db.execute('ROLLBACK TO operation')
                db.execute('RELEASE operation')
                result = {'status': 'refused', 'code': exc.code, 'detail': exc.detail}
            result.update(actor=actor, command=command, operation=op)
            db.execute('INSERT INTO command_receipts VALUES (?,?,?,?)', (actor, command, raw, encoded(result)))
            db.commit()
            return result
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()
