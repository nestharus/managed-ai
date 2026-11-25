#!/usr/bin/env python3
"""Optional host queue for supplied observing uses; no semantic observer or effect authority.

One local SQLite owner. The capable host supplies use definitions, source observations,
worker invocation and semantic acceptance. This file owns only recoverable eligibility,
current-basis service and explicit occurrence retention.
"""
from __future__ import annotations
import argparse
from contextlib import contextmanager
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import uuid

FORMAT = 'g60-signal-field/1'
MODES = {'live', 'historical', 'each-change'}
MAX_WORKER_STREAM_BYTES = 16 * 1024


def encoded(x):
    return json.dumps(x, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(x):
    return hashlib.sha256(encoded(x).encode()).hexdigest()


def require(ok, message):
    if not ok:
        raise ValueError(message)


def _unique_json(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key: ' + key)
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError('nonfinite JSON constant: ' + value)


def loads_strict(raw):
    text = raw.decode('utf-8') if isinstance(raw, (bytes, bytearray)) else raw
    return json.loads(text, object_pairs_hook=_unique_json, parse_constant=_reject_constant)


def _product_root() -> Path:
    here = Path(__file__).resolve().parent
    if here.name == 'signal_field' and here.parent.name == 'host':
        return here.parents[1]
    return here


def inside_product(path: Path, product: Path | None = None) -> bool:
    product = _product_root() if product is None else Path(product).resolve()
    resolved = Path(path).expanduser().resolve()
    if resolved == product or resolved.is_relative_to(product):
        return True
    if product.name == 'runtime' and (product.parent / 'CURRENT-RELEASE.json').is_file():
        if resolved == product.parent or resolved.is_relative_to(product.parent):
            return True
    return False


def _require_ordinary_non_symlink(path: Path, role: str, *, must_exist: bool = False) -> None:
    require(not path.is_symlink(), f'{role} must not be a symlink: {path}')
    try:
        info = path.lstat()
    except FileNotFoundError:
        if must_exist:
            raise ValueError(f'ordinary {role} required: {path}')
        return
    require(stat.S_ISREG(info.st_mode), f'ordinary {role} required: {path}')


def validate_db_path(path, *, product: Path | None = None) -> Path:
    require(isinstance(path, (str, Path)) and str(path).strip() and '\0' not in str(path),
            'database path must be a valid nonempty path')
    raw = Path(path).expanduser()
    _require_ordinary_non_symlink(raw, 'database file', must_exist=False)
    for suffix in ('-journal', '-wal', '-shm'):
        _require_ordinary_non_symlink(Path(str(raw) + suffix), 'database journal file', must_exist=False)
    resolved = raw.resolve()
    require(not inside_product(resolved, product),
            'database must be outside the product directory')
    if raw.parent.exists():
        require(raw.parent.is_dir(), 'database parent must be a directory')
    return resolved


def no_journal(path):
    require(not any(Path(str(path) + suffix).exists() or Path(str(path) + suffix).is_symlink()
                 for suffix in ('-journal', '-wal', '-shm')),
         'host journal/WAL state requires its owner; no host operations prepared')


def _stream_details(data, limit: int = MAX_WORKER_STREAM_BYTES) -> tuple[str, int, str, bool]:
    if data is None:
        raw = b''
    elif isinstance(data, str):
        raw = data.encode('utf-8', errors='replace')
    else:
        raw = bytes(data)
    truncated = len(raw) > limit
    text = raw[:limit].decode('utf-8', errors='replace')
    return text, len(raw), hashlib.sha256(raw).hexdigest(), truncated


def _worker_outcome(kind: str, *, returncode: int | None, stdout=b'', stderr=b'',
                    error: str | None = None, timeout: int | float | None = None,
                    errno_value: int | None = None) -> dict:
    out_text, out_bytes, out_sha, out_trunc = _stream_details(stdout)
    err_text, err_bytes, err_sha, err_trunc = _stream_details(stderr)
    outcome = {
        'kind': kind,
        'returncode': returncode,
        'stdout': out_text,
        'stdout_bytes': out_bytes,
        'stdout_sha256': out_sha,
        'stdout_truncated': out_trunc,
        'stderr': err_text,
        'stderr_bytes': err_bytes,
        'stderr_sha256': err_sha,
        'stderr_truncated': err_trunc,
    }
    if error is not None:
        outcome['error'] = error
    if timeout is not None:
        outcome['timeout'] = timeout
    if errno_value is not None:
        outcome['errno'] = errno_value
    return outcome


def planning_snapshot(path, read_guard):
    """Read the exact rollback-journal cut guarded by the caller."""
    path = Path(path)
    _require_ordinary_non_symlink(path, 'database file', must_exist=True)
    no_journal(path)
    database_raw = read_guard()
    require(database_raw[:16] == b'SQLite format 3\x00' and len(database_raw) >= 100
         and database_raw[18:20] == b'\x01\x01',
         'only a rollback-journal host cut is supported; inspect WAL mode with its owner')
    try:
        db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
    except sqlite3.Error as exc:
        raise ValueError(str(exc)) from exc
    db.row_factory = sqlite3.Row
    try:
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        fmt = db.execute('SELECT value FROM meta WHERE key=?', ('format',)).fetchone()
        require(fmt and fmt['value'] == FORMAT, 'unsupported host format')
        sources = {r['id']: {'source': r['id'], 'epoch': r['epoch'],
                            'position': r['position'], 'value': loads_strict(r['value'])}
                   for r in db.execute('SELECT id,epoch,position,value FROM sources ORDER BY id')}
        uses = {}
        for row in db.execute('SELECT id,spec,signature,generation FROM uses ORDER BY id'):
            spec = loads_strict(row['spec'])
            signature = digest(spec)
            require(spec.get('id') == row['id'] and signature == row['signature']
                 and type(row['generation']) is int and row['generation'] > 0,
                 'host use identity/signature differs')
            uses[row['id']] = spec
        dependencies = {(r['source'], r['use_id']) for r in
                        db.execute('SELECT source,use_id FROM dependencies ORDER BY source,use_id')}
        require(dependencies == {(source, ident) for ident, spec in uses.items()
                              for source in spec['sources']},
             'host dependency rows differ from declared use definitions')
        jobs = [dict(r) for r in db.execute(
            'SELECT id,use_id,generation,ticket,token,gap FROM jobs ORDER BY id')]
        read_guard()
        no_journal(path)
    except sqlite3.Error as exc:
        raise ValueError(str(exc)) from exc
    finally:
        db.close()
    read_guard()
    no_journal(path)
    return {'sources': sources, 'uses': uses, 'jobs': jobs}


class Field:
    def __init__(self, path, *, product: Path | None = None):
        self.path = validate_db_path(path, product=product)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.sql_calls = 0
        self.rows_returned = 0
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sources (id TEXT PRIMARY KEY, epoch TEXT NOT NULL,
            position INTEGER NOT NULL, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS uses (id TEXT PRIMARY KEY, spec TEXT NOT NULL,
            signature TEXT NOT NULL, generation INTEGER NOT NULL, latest TEXT, last_rejected TEXT);
        CREATE TABLE IF NOT EXISTS dependencies (source TEXT NOT NULL, use_id TEXT NOT NULL,
            PRIMARY KEY (source,use_id));
        CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, use_id TEXT NOT NULL,
            generation INTEGER NOT NULL, basis TEXT NOT NULL, spec TEXT NOT NULL,
            ticket INTEGER, token TEXT, gap TEXT, attempt TEXT, worker_outcome TEXT);
        CREATE INDEX IF NOT EXISTS eligible ON jobs(ticket) WHERE token IS NULL;
        CREATE INDEX IF NOT EXISTS use_jobs ON jobs(use_id);
        CREATE TABLE IF NOT EXISTS selected_history (name TEXT PRIMARY KEY, snapshot TEXT NOT NULL);
        ''')
        with self.mutation():
            columns = {row['name'] for row in self.db.execute('PRAGMA table_info(jobs)').fetchall()}
            if 'worker_outcome' not in columns:
                self.db.execute('ALTER TABLE jobs ADD COLUMN worker_outcome TEXT')
            self.db.execute('INSERT OR IGNORE INTO meta VALUES (?,?)', ('format', FORMAT))
            self.db.execute('INSERT OR IGNORE INTO meta VALUES (?,?)', ('sequence', '0'))
        require(self.one('SELECT value FROM meta WHERE key=?', ('format',))['value'] == FORMAT,
                'unsupported signal field format')

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    @contextmanager
    def mutation(self):
        self.execute('BEGIN IMMEDIATE')
        try:
            yield
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def execute(self, query, values=()):
        self.sql_calls += 1
        return self.db.execute(query, values)

    def one(self, query, values=()):
        row = self.execute(query, values).fetchone()
        self.rows_returned += int(row is not None)
        return row

    def all(self, query, values=()):
        rows = self.execute(query, values).fetchall()
        self.rows_returned += len(rows)
        return rows

    def ticket(self):
        n = int(self.one('SELECT value FROM meta WHERE key=?', ('sequence',))['value']) + 1
        self.execute('UPDATE meta SET value=? WHERE key=?', (str(n), 'sequence'))
        return n

    def basis(self, spec):
        result = {}
        for name in spec['sources']:
            row = self.one('SELECT epoch,position,value FROM sources WHERE id=?', (name,))
            result[name] = dict(row) if row else None
            if row:
                result[name]['value'] = loads_strict(row['value'])
        return result

    def queue(self, use, basis, *, occurrence=None, gap=None):
        key = ('occurrence:' + digest([use['id'], occurrence])) if occurrence else 'use:' + use['id']
        old = self.one('SELECT ticket FROM jobs WHERE id=?', (key,))
        ticket = old['ticket'] if old and old['ticket'] is not None else self.ticket()
        self.execute('''INSERT INTO jobs (id,use_id,generation,basis,spec,ticket,token,gap,attempt,worker_outcome)
            VALUES (?,?,?,?,?,?,NULL,?,NULL,NULL)
            ON CONFLICT(id) DO UPDATE SET generation=excluded.generation,
            basis=excluded.basis,spec=excluded.spec,ticket=excluded.ticket,gap=excluded.gap''',
            (key, use['id'], use['generation'], encoded(basis), use['spec'], ticket,
             encoded(gap) if gap else None))
        return key

    def enroll(self, spec):
        require(isinstance(spec, dict) and set(spec) == {'id', 'mode', 'question', 'purpose', 'sources', 'observer', 'scope'},
                'supply id, mode, question, purpose, sources, observer and scope')
        require(all(isinstance(spec[k], str) and spec[k].strip() for k in ('id', 'mode', 'question', 'purpose')),
                'use identity, mode, question and purpose must be nonempty')
        require(spec['mode'] in MODES, 'unsupported receiving lifetime')
        require(isinstance(spec['sources'], list) and len(set(spec['sources'])) == len(spec['sources'])
                and all(isinstance(s, str) and s for s in spec['sources']), 'sources must be distinct names')
        signature = digest(spec)
        with self.mutation():
            old = self.one('SELECT * FROM uses WHERE id=?', (spec['id'],))
            require(not old or loads_strict(old['spec'])['mode'] == spec['mode'],
                    'a different receiving lifetime requires a distinct use identity')
            if old and old['signature'] == signature:
                return {'enrolled': spec['id'], 'changed': False}
            generation = old['generation'] + 1 if old else 1
            self.execute('''INSERT INTO uses VALUES (?,?,?,?,NULL,NULL)
                ON CONFLICT(id) DO UPDATE SET spec=excluded.spec,signature=excluded.signature,
                generation=excluded.generation''', (spec['id'], encoded(spec), signature, generation))
            self.execute('DELETE FROM dependencies WHERE use_id=?', (spec['id'],))
            for source in spec['sources']:
                self.execute('INSERT INTO dependencies VALUES (?,?)', (source, spec['id']))
            use = self.one('SELECT * FROM uses WHERE id=?', (spec['id'],))
            if spec['mode'] != 'each-change':
                self.queue(use, self.basis(spec))
        return {'enrolled': spec['id'], 'changed': True, 'generation': generation}

    def change(self, source, epoch, position, value, *, notify=True, reset=False):
        require(isinstance(source, str) and source and isinstance(epoch, str) and epoch,
                'source and stream epoch must be nonempty')
        require(type(position) is int and position >= 0, 'position must be a nonnegative integer')
        require(type(notify) is bool and type(reset) is bool, 'notify and reset must be booleans')
        with self.mutation():
            old = self.one('SELECT * FROM sources WHERE id=?', (source,))
            require(not old or old['epoch'] == epoch or reset, 'stream epoch changed; explicit source reset required')
            if old and old['epoch'] == epoch and position <= old['position']:
                if position == old['position']:
                    require(old['value'] == encoded(value), 'same stream position has different content')
                return {'source': source, 'admitted': False, 'reason': 'already observed stream position'}
            self.execute('''INSERT INTO sources VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                epoch=excluded.epoch,position=excluded.position,value=excluded.value''',
                (source, epoch, position, encoded(value)))
            users = self.all('''SELECT uses.* FROM uses JOIN dependencies ON uses.id=dependencies.use_id
                WHERE dependencies.source=? ORDER BY uses.id''', (source,))
            live, occurrences, gaps = 0, 0, 0
            for use in users:
                spec = loads_strict(use['spec'])
                if spec['mode'] == 'historical':
                    continue
                if spec['mode'] == 'live':
                    if notify:
                        self.execute('UPDATE uses SET generation=generation+1 WHERE id=?', (use['id'],))
                        use = self.one('SELECT * FROM uses WHERE id=?', (use['id'],))
                        self.queue(use, self.basis(spec)); live += 1
                else:
                    gap = None
                    if old and (old['epoch'] != epoch or position != old['position'] + 1):
                        gap = {'source': source, 'before': {'epoch': old['epoch'], 'position': old['position']},
                               'after': {'epoch': epoch, 'position': position},
                               'requires': 'Recover missing owed occurrences or obtain a separately authorized change of duty.'}
                        gaps += 1
                    self.queue(use, self.basis(spec), occurrence=[source, epoch, position], gap=gap)
                    occurrences += 1
        return {'source': source, 'admitted': True, 'live_uses_affected': live,
                'owed_occurrences': occurrences, 'replay_gaps': gaps}

    def reconcile(self):
        """Recover live rechecks from current dependencies; never fabricate missed occurrences."""
        queued = 0
        with self.mutation():
            for use in self.all('SELECT * FROM uses ORDER BY id'):
                spec = loads_strict(use['spec'])
                if spec['mode'] != 'live':
                    continue
                basis = self.basis(spec)
                latest = loads_strict(use['latest']) if use['latest'] else None
                job = self.one('SELECT * FROM jobs WHERE id=?', ('use:' + use['id'],))
                if job and job['basis'] == encoded(basis) and job['spec'] == use['spec']:
                    continue
                if latest and latest['basis'] == basis and latest['signature'] == use['signature']:
                    continue
                self.execute('UPDATE uses SET generation=generation+1 WHERE id=?', (use['id'],))
                self.queue(self.one('SELECT * FROM uses WHERE id=?', (use['id'],)), basis); queued += 1
        return {'live_rechecks_reconstructed': queued, 'scope': 'Current declared dependencies only.'}

    def pull(self):
        with self.mutation():
            job = self.one('SELECT * FROM jobs WHERE ticket IS NOT NULL AND token IS NULL AND gap IS NULL ORDER BY ticket,id LIMIT 1')
            if job is None:
                return None
            token = uuid.uuid4().hex
            packet = {'job': job['id'], 'use': job['use_id'], 'generation': job['generation'],
                      'token': token, 'spec': loads_strict(job['spec']), 'basis': loads_strict(job['basis'])}
            self.execute('UPDATE jobs SET token=?,ticket=NULL,attempt=?,worker_outcome=NULL WHERE id=?',
                         (token, encoded(packet), job['id']))
            return packet

    def receive(self, packet, result):
        require(isinstance(packet, dict) and isinstance(packet.get('job'), str) and packet['job']
                and isinstance(packet.get('token'), str) and packet['token'],
                'packet must be a captured job attempt')
        require(isinstance(result, dict) and isinstance(result.get('state'), str)
                and result['state'] in {'complete', 'blocked'},
                'worker must return complete or blocked with its actual result')
        require('account' in result and isinstance(result['account'], str) and result['account'].strip(),
                'worker must supply an account; service is not semantic acceptance')
        with self.mutation():
            job = self.one('SELECT * FROM jobs WHERE id=?', (packet['job'],))
            if not job or job['token'] != packet['token']:
                return {'status': 'refused', 'reason': 'unknown, completed or replaced attempt'}
            if job['attempt'] != encoded(packet):
                return {'status': 'refused', 'reason': 'packet differs from the actual captured attempt'}
            use = self.one('SELECT * FROM uses WHERE id=?', (job['use_id'],))
            matches = (job['use_id'] == packet['use'] and job['generation'] == packet['generation']
                       and loads_strict(job['spec']) == packet['spec'] and loads_strict(job['basis']) == packet['basis'])
            if packet['spec']['mode'] == 'live':
                matches = matches and use['spec'] == job['spec'] and self.basis(packet['spec']) == packet['basis']
            if not matches:
                self.execute('UPDATE uses SET last_rejected=? WHERE id=?',
                             (encoded({'generation': packet['generation'], 'basis': packet['basis'], 'result': result}), use['id']))
                if packet['spec']['mode'] == 'live':
                    current_spec = loads_strict(use['spec'])
                    current_basis = self.basis(current_spec)
                    if job['basis'] != encoded(current_basis) or job['spec'] != use['spec']:
                        self.execute('UPDATE uses SET generation=generation+1 WHERE id=?', (use['id'],))
                        use = self.one('SELECT * FROM uses WHERE id=?', (use['id'],))
                    # Recover this affected live use now, even when its optional
                    # wakeup was missed during the callback. Preserve its ticket.
                    self.queue(use, current_basis)
                self.execute('UPDATE jobs SET token=NULL,attempt=NULL,worker_outcome=NULL,ticket=COALESCE(ticket,?) WHERE id=?',
                             (self.ticket(), job['id']))
                return {'status': 'stale', 'reason': 'Returned work differs from current receiving basis; candidate retained separately by its native owner.'}
            snapshot = {'signature': digest(packet['spec']), 'generation': packet['generation'],
                        'basis': packet['basis'], 'result': result, 'mode': packet['spec']['mode']}
            # An owed historical return can be served under its original contract,
            # but may not overwrite the current view after that use is redefined.
            current_contract = use['signature'] == snapshot['signature']
            if current_contract:
                self.execute('UPDATE uses SET latest=? WHERE id=?', (encoded(snapshot), use['id']))
            if result['state'] == 'blocked' and packet['spec']['mode'] == 'each-change':
                self.execute('UPDATE jobs SET token=NULL,attempt=NULL,worker_outcome=NULL,gap=? WHERE id=?',
                             (encoded({'requires': result['account']}), job['id']))
            else:
                self.execute('DELETE FROM jobs WHERE id=?', (job['id'],))
            return {'status': 'received', 'service_state': result['state'], 'current_contract': current_contract,
                    'scope': 'Attributed worker return, not an automatic observation-quality or whole-goal judgment.'}

    def retry(self, job_id, token):
        """Explicit host recovery fences the old worker. The effect owner must recover effects separately."""
        require(isinstance(job_id, str) and job_id and isinstance(token, str) and token,
                'job_id and token must be nonempty strings')
        with self.mutation():
            row = self.one('SELECT * FROM jobs WHERE id=?', (job_id,))
            require(row is not None and row['token'] == token, 'attempt is absent or already replaced')
            self.execute('UPDATE jobs SET token=NULL,attempt=NULL,worker_outcome=NULL,ticket=COALESCE(ticket,?) WHERE id=?',
                         (self.ticket(), job_id))
        return {'retry_eligible': job_id, 'old_worker_fenced': True}

    def retain(self, use_id, name):
        require(isinstance(use_id, str) and use_id and isinstance(name, str) and name.strip(),
                'use_id and retained name must be nonempty strings')
        with self.mutation():
            row = self.one('SELECT latest FROM uses WHERE id=?', (use_id,))
            require(row and row['latest'], 'no actual received observation to retain')
            previous = self.one('SELECT snapshot FROM selected_history WHERE name=?', (name,))
            require(not previous or previous['snapshot'] == row['latest'], 'retained name identifies different historical observation')
            self.execute('INSERT OR IGNORE INTO selected_history VALUES (?,?)', (name, row['latest']))
        return {'retained': name, 'scope': 'Explicit historical cut; no current validity inferred.'}

    @staticmethod
    def job_view(row):
        outcome = row['worker_outcome'] if 'worker_outcome' in row.keys() and row['worker_outcome'] else None
        return {'job': row['id'], 'use': row['use_id'], 'desired_generation': row['generation'],
                'desired_basis': loads_strict(row['basis']), 'desired_spec': loads_strict(row['spec']),
                'eligible_ticket': row['ticket'], 'token': row['token'],
                'captured_attempt': loads_strict(row['attempt']) if row['attempt'] else None,
                'blocked_recovery': loads_strict(row['gap']) if row['gap'] else None,
                'worker_outcome': loads_strict(outcome) if outcome else None}

    def pending(self, after='', limit=20, max_bytes=65536):
        require(isinstance(after, str) and type(limit) is int and 1 <= limit <= 100,
                'pending requires a string cursor and limit from 1 through 100')
        require(type(max_bytes) is int and 1024 <= max_bytes <= 4 * 1024 * 1024,
                'pending byte limit must be from 1024 through 4194304')
        rows = self.all('SELECT * FROM jobs WHERE id>? ORDER BY id LIMIT ?', (after, limit + 1))
        items = []
        for row in rows[:limit]:
            candidate = self.job_view(row)
            if len(encoded(items + [candidate]).encode()) > max_bytes:
                break
            items.append(candidate)
        return {'items': items, 'next_after': items[-1]['job'] if items else after,
                'more': len(rows) > len(items), 'next_omitted': rows[len(items)]['id'] if len(rows) > len(items) else None,
                'scope': 'Trusted host recovery metadata, bounded output over enrolled jobs. The token supplies no external-effect authority.'}

    def inspect(self, use_id=None, job_id=None, history_name=None, max_bytes=1048576):
        require(sum(x is not None for x in (use_id, job_id, history_name)) == 1,
                'inspect exactly one use, pending job or selected historical account')
        for label, value in (('use_id', use_id), ('job_id', job_id), ('history_name', history_name)):
            if value is not None:
                require(isinstance(value, str) and value.strip(), f'{label} must be nonempty text')
        require(type(max_bytes) is int and 1024 <= max_bytes <= 4 * 1024 * 1024,
                'inspection byte limit must be from 1024 through 4194304')
        if job_id is not None:
            row = self.one('SELECT * FROM jobs WHERE id=?', (job_id,))
            require(row is not None, 'pending job is absent')
            result = self.job_view(row)
        elif history_name is not None:
            row = self.one('SELECT snapshot FROM selected_history WHERE name=?', (history_name,))
            require(row is not None, 'selected historical account is absent')
            result = {'name': history_name, 'historical': loads_strict(row['snapshot'])}
        else:
            row = self.one('SELECT * FROM uses WHERE id=?', (use_id,))
            require(row is not None, 'receiving use is absent')
            spec = loads_strict(row['spec']); basis = self.basis(spec)
            latest = loads_strict(row['latest']) if row['latest'] else None
            result = {'use': use_id, 'spec': spec, 'current_declared_basis': basis, 'latest': latest,
                      'definition_matches': latest['signature'] == row['signature'] if latest else None,
                      'declared_basis_matches': latest['basis'] == basis if latest else None,
                      'last_rejected': loads_strict(row['last_rejected']) if row['last_rejected'] else None}
        result['scope'] = 'Observed bindings and retained accounts only; semantic validity and effect permission require their actual receivers.'
        require(len(encoded(result).encode()) <= max_bytes, 'selected inspection exceeds output byte limit; request a larger explicit bound')
        return result

    def status(self):
        jobs = self.all('SELECT id,use_id,token,ticket,gap FROM jobs ORDER BY COALESCE(ticket,0),id')
        states = {'eligible': 0, 'in_flight': 0, 'blocked_occurrences': 0, 'blocked_live_uses': 0}
        for job in jobs:
            states['blocked_occurrences' if job['gap'] else 'in_flight' if job['token'] else 'eligible'] += 1
        for row in self.all('SELECT spec,latest FROM uses WHERE latest IS NOT NULL'):
            if loads_strict(row['spec'])['mode'] != 'each-change' and loads_strict(row['latest'])['result']['state'] == 'blocked':
                states['blocked_live_uses'] += 1
        return {'format': FORMAT, **states, 'uses': self.one('SELECT COUNT(*) AS n FROM uses')['n'],
                'selected_historical_observations': self.one('SELECT COUNT(*) AS n FROM selected_history')['n'],
                'remaining_jobs': len(jobs), 'sql_calls_this_connection': self.sql_calls,
                'rows_returned_this_connection': self.rows_returned, 'database_bytes': self.path.stat().st_size,
                'scope': 'Known enrolled uses only. Quiet queue is not whole-field semantic success.'}

    def _record_worker_outcome(self, packet: dict, outcome: dict) -> None:
        with self.mutation():
            self.execute('UPDATE jobs SET worker_outcome=? WHERE id=? AND token=?',
                         (encoded(outcome), packet['job'], packet['token']))

    def run(self, argv, budget, timeout):
        require(isinstance(argv, list) and argv and all(isinstance(x, str) and x and '\0' not in x for x in argv),
                'worker requires explicit subprocess argv')
        require(type(budget) is int and budget > 0, 'positive invocation budget required')
        require(type(timeout) in (int, float) and math.isfinite(timeout) and timeout > 0,
                'finite positive worker timeout required before any work is claimed')
        self.reconcile()
        invoked = 0; packet_bytes = 0; received = []
        for _ in range(budget):
            packet = self.pull()
            if packet is None:
                break
            payload = encoded(packet).encode(); packet_bytes += len(payload); invoked += 1
            worker = None
            try:
                worker = subprocess.run(argv, input=payload, capture_output=True, timeout=timeout, check=False)
                if worker.returncode != 0:
                    outcome = _worker_outcome('non-zero-exit', returncode=worker.returncode,
                                              stdout=worker.stdout, stderr=worker.stderr,
                                              error='worker exited without a valid return')
                    self._record_worker_outcome(packet, outcome)
                    return {'status': 'worker-unresolved', 'invocations': invoked, 'packet_bytes': packet_bytes,
                            'returns': received,
                            'reason': 'worker exited without a valid return', 'in_flight_job': packet['job'],
                            'worker': outcome,
                            'recovery': 'Inspect the retained attempt and actual worker/native outcome before explicitly retrying.',
                            'remaining': self.status()}
                result = loads_strict(worker.stdout)
                received.append(self.receive(packet, result))
            except subprocess.TimeoutExpired as exc:
                outcome = _worker_outcome('timeout', returncode=None,
                                          stdout=exc.stdout, stderr=exc.stderr,
                                          error=str(exc), timeout=timeout)
                self._record_worker_outcome(packet, outcome)
                return {'status': 'worker-unresolved', 'invocations': invoked, 'packet_bytes': packet_bytes,
                        'returns': received,
                        'reason': str(exc), 'in_flight_job': packet['job'], 'worker': outcome,
                        'recovery': 'Inspect the retained attempt and actual worker/native outcome before explicitly retrying.',
                        'remaining': self.status()}
            except OSError as exc:
                outcome = _worker_outcome('launch-error', returncode=None,
                                          error=str(exc), errno_value=exc.errno)
                self._record_worker_outcome(packet, outcome)
                return {'status': 'worker-unresolved', 'invocations': invoked, 'packet_bytes': packet_bytes,
                        'returns': received,
                        'reason': str(exc), 'in_flight_job': packet['job'], 'worker': outcome,
                        'recovery': 'Inspect the retained attempt and actual worker/native outcome before explicitly retrying.',
                        'remaining': self.status()}
            except (ValueError, TypeError) as exc:
                outcome = _worker_outcome('invalid-return',
                                          returncode=worker.returncode if worker is not None else None,
                                          stdout=worker.stdout if worker is not None else b'',
                                          stderr=worker.stderr if worker is not None else b'',
                                          error=str(exc))
                self._record_worker_outcome(packet, outcome)
                return {'status': 'worker-unresolved', 'invocations': invoked, 'packet_bytes': packet_bytes,
                        'returns': received,
                        'reason': str(exc), 'in_flight_job': packet['job'], 'worker': outcome,
                        'recovery': 'Inspect the retained attempt and actual worker/native outcome before explicitly retrying.',
                        'remaining': self.status()}
        state = self.status()
        return {'status': 'budget-exhausted' if state['eligible'] else 'awaiting-in-flight' if state['in_flight'] else
                'blocked' if state['blocked_occurrences'] or state['blocked_live_uses'] else 'current-enrolled-service-caught-up',
                'invocations': invoked, 'packet_bytes': packet_bytes, 'returns': received, 'remaining': state,
                'scope': 'Actual configured worker calls. No provider/model or all-dimension semantic measurement is inferred.'}


def _check_keys(op: str, params: dict, *, required: set[str] = frozenset(), optional: set[str] = frozenset()) -> None:
    missing = set(required) - set(params)
    require(not missing, f'{op} missing required parameters: ' + ', '.join(sorted(missing)))
    extra = set(params) - set(required) - set(optional)
    require(not extra, f'{op} received unsupported parameters: ' + ', '.join(sorted(extra)))


def _op_enroll(field: Field, params: dict):
    _check_keys('enroll', params, required={'spec'})
    return field.enroll(params['spec'])


def _op_change(field: Field, params: dict):
    _check_keys('change', params, required={'source', 'epoch', 'position', 'value'}, optional={'notify', 'reset'})
    return field.change(
        params['source'],
        params['epoch'],
        params['position'],
        params['value'],
        notify=params.get('notify', True),
        reset=params.get('reset', False),
    )


def _op_reconcile(field: Field, params: dict):
    _check_keys('reconcile', params)
    return field.reconcile()


def _op_pull(field: Field, params: dict):
    _check_keys('pull', params)
    return field.pull()


def _op_receive(field: Field, params: dict):
    _check_keys('receive', params, required={'packet', 'result'})
    return field.receive(params['packet'], params['result'])


def _op_retry(field: Field, params: dict):
    _check_keys('retry', params, required={'job_id', 'token'})
    return field.retry(params['job_id'], params['token'])


def _op_retain(field: Field, params: dict):
    _check_keys('retain', params, required={'use_id', 'name'})
    return field.retain(params['use_id'], params['name'])


def _op_pending(field: Field, params: dict):
    _check_keys('pending', params, optional={'after', 'limit', 'max_bytes'})
    return field.pending(
        after=params.get('after', ''),
        limit=params.get('limit', 20),
        max_bytes=params.get('max_bytes', 65536),
    )


def _op_inspect(field: Field, params: dict):
    _check_keys('inspect', params, optional={'use_id', 'job_id', 'history_name', 'max_bytes'})
    return field.inspect(
        use_id=params.get('use_id'),
        job_id=params.get('job_id'),
        history_name=params.get('history_name'),
        max_bytes=params.get('max_bytes', 1048576),
    )


def _op_status(field: Field, params: dict):
    _check_keys('status', params)
    return field.status()


def _op_run(field: Field, params: dict):
    _check_keys('run', params, required={'argv', 'budget', 'timeout'})
    return field.run(params['argv'], params['budget'], params['timeout'])


OPERATIONS = {
    'enroll': _op_enroll,
    'change': _op_change,
    'reconcile': _op_reconcile,
    'pull': _op_pull,
    'receive': _op_receive,
    'retry': _op_retry,
    'retain': _op_retain,
    'pending': _op_pending,
    'inspect': _op_inspect,
    'status': _op_status,
    'run': _op_run,
}


def dispatch_operation(field: Field, request):
    require(isinstance(request, dict), 'request must be a JSON object')
    op = request.get('op')
    require(isinstance(op, str) and op in OPERATIONS, 'unknown operation')
    params = {k: v for k, v in request.items() if k != 'op'}
    return OPERATIONS[op](field, params)


def main():
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('--db', required=True, type=Path)
    parser.add_argument('--request', type=Path, help='JSON operation; omit to read one operation from stdin')
    args = parser.parse_args()
    if args.request is not None:
        _require_ordinary_non_symlink(args.request, 'request file', must_exist=True)
        raw_request = args.request.read_bytes()
    else:
        raw_request = sys.stdin.buffer.read()
    request = loads_strict(raw_request)
    with Field(args.db) as field:
        result = dispatch_operation(field, request)
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
        print(json.dumps({'status': 'unresolved', 'reason': str(exc)}, allow_nan=False), file=sys.stderr)
        raise SystemExit(2)
