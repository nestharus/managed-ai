#!/usr/bin/env python3
"""Retain source admissions and evaluate explicitly scoped JSON receiving duties."""
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from pathlib import Path
import argparse
import json
import sqlite3
import sys

FORMAT = 'g60-source-admission/1'
MAX_BYTES = 2 * 1024 * 1024
REQUIREMENTS = ('normalized', 'selected-original', 'all-originals')
EQUALITY = 'json-number-value'


def digest(raw):
    return sha256(raw).hexdigest()


def text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(label + ' must be nonempty text')
    return value


def observe(raw):
    """Inspect these exact UTF-8 JSON bytes, retaining decoded repeated names."""
    if not isinstance(raw, bytes) or len(raw) > MAX_BYTES:
        raise ValueError('original must be bytes within the 2 MiB input bound')
    duplicates = []

    def pairs(items):
        record = {}
        for name, value in items:
            if name in record:
                duplicates.append(name)
            record[name] = value
        return record

    def constant(value):
        raise ValueError('non-JSON numeric constant ' + value)

    result = {'sha256': digest(raw), 'bytes': len(raw), 'valid_json': False,
              'object_root': False, 'unique_member_names': False,
              'duplicate_names': [], 'value': None}
    try:
        value = json.loads(raw.decode('utf-8-sig'), object_pairs_hook=pairs,
                           parse_int=Decimal, parse_float=Decimal, parse_constant=constant)
    except (RecursionError, InvalidOperation) as error:
        result.update(valid_json=None, object_root=None,
                      unique_member_names=False if duplicates else None, duplicate_names=duplicates,
                      error='observation unavailable under parser limits: ' + str(error))
        return result
    except (UnicodeError, ValueError) as error:
        result['error'] = str(error)
        return result
    result.update(valid_json=True, object_root=isinstance(value, dict),
                  unique_member_names=not duplicates, duplicate_names=duplicates,
                  value=value)
    return result


def same_value(left, right):
    """JSON numbers compare by value. Booleans remain a separate JSON type."""
    pending = [(left, right)]
    while pending:
        left, right = pending.pop()
        if isinstance(left, bool) or isinstance(right, bool):
            if type(left) is not type(right) or left != right:
                return False
        elif isinstance(left, (int, Decimal)) and isinstance(right, (int, Decimal)):
            if left != right:
                return False
        elif type(left) is not type(right):
            return False
        elif isinstance(left, dict):
            if left.keys() != right.keys():
                return False
            pending.extend((left[key], right[key]) for key in left)
        elif isinstance(left, list):
            if len(left) != len(right):
                return False
            pending.extend(zip(left, right))
        elif left != right:
            return False
    return True


def public_observation(raw):
    result = observe(raw)
    del result['value']
    return result


class Admission:
    """One cooperative local source owner with a durable admission cutoff."""

    def __init__(self, path):
        self.path = Path(path).resolve()
        if not self.path.is_file():
            raise ValueError('initialize the source store first')
        with self.connection() as db:
            if db.execute('select format from metadata').fetchone()[0] != FORMAT:
                raise ValueError('unsupported source store format')

    @contextmanager
    def connection(self, write=False):
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA foreign_keys=ON')
            db.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @classmethod
    def initialize(cls, path, epoch, origin):
        path = Path(path).resolve()
        text(epoch, 'source epoch')
        if origin not in ('original', 'derived'):
            raise ValueError('source origin must be original or derived')
        if path.exists() or not path.parent.is_dir():
            raise ValueError('choose a new source store in an existing directory')
        db = sqlite3.connect(path)
        try:
            with db:
                db.executescript('''
                  create table metadata(format text not null);
                  create table editions(sequence integer primary key, epoch text unique not null,
                    origin text not null check(origin in ('original','derived')));
                  create table jobs(job text primary key, edition integer not null references editions,
                    prior_history text not null check(prior_history in ('empty','unknown')),
                    closed integer not null default 0, selected text);
                  create table candidates(job text references jobs, name text, raw blob not null,
                    primary key(job,name));
                ''')
                db.execute('insert into metadata values(?)', (FORMAT,))
                db.execute('insert into editions(epoch,origin) values(?,?)', (epoch, origin))
        finally:
            db.close()
        return cls(path)

    @staticmethod
    def current(db):
        return db.execute('select * from editions order by sequence desc limit 1').fetchone()

    @staticmethod
    def job(db, job):
        row = db.execute('''select jobs.*,editions.epoch,editions.origin from jobs
                            join editions on jobs.edition=editions.sequence where job=?''', (job,)).fetchone()
        if row is None:
            raise ValueError('unknown admission job')
        return row

    def current_job(self, db, job):
        row = self.job(db, job)
        if row['edition'] != self.current(db)['sequence']:
            raise ValueError('source edition changed; open a new admission job')
        return row

    def advance(self, epoch, origin, expected):
        text(epoch, 'source epoch')
        if origin not in ('original', 'derived'):
            raise ValueError('source origin must be original or derived')
        with self.connection(True) as db:
            if self.current(db)['epoch'] != expected:
                raise ValueError('source epoch conflict')
            db.execute('insert into editions(epoch,origin) values(?,?)', (epoch, origin))
        return {'epoch': epoch, 'origin': origin}

    def open(self, job, expected_epoch, prior_history):
        text(job, 'job')
        if prior_history not in ('empty', 'unknown'):
            raise ValueError('declare the prior history as empty or unknown')
        with self.connection(True) as db:
            edition = self.current(db)
            if edition['epoch'] != expected_epoch:
                raise ValueError('source epoch conflict')
            previous = db.execute('select * from jobs where job=?', (job,)).fetchone()
            if previous is not None:
                if previous['edition'] != edition['sequence'] or previous['prior_history'] != prior_history:
                    raise ValueError('admission identity already has different terms')
            else:
                db.execute('insert into jobs(job,edition,prior_history) values(?,?,?)',
                           (job, edition['sequence'], prior_history))
        return self.show(job)

    def admit(self, job, name, raw):
        text(name, 'candidate')
        observation = public_observation(raw)
        with self.connection(True) as db:
            row = self.current_job(db, job)
            prior = db.execute('select raw from candidates where job=? and name=?', (job, name)).fetchone()
            if prior is not None:
                if prior['raw'] != raw:
                    raise ValueError('candidate identity already binds different original bytes')
            else:
                if row['closed']:
                    raise ValueError('admission cutoff is final')
                db.execute('insert into candidates values(?,?,?)', (job, name, raw))
        return {'job': job, 'candidate': name, 'observation': observation}

    def close(self, job, selected):
        with self.connection(True) as db:
            row = self.current_job(db, job)
            if row['closed'] and row['selected'] != selected:
                raise ValueError('final selection cannot be replaced')
            if db.execute('select 1 from candidates where job=? and name=?', (job, selected)).fetchone() is None:
                raise ValueError('selected candidate has not been admitted')
            db.execute('update jobs set closed=1,selected=? where job=?', (selected, job))
        return self.show(job)

    def show(self, job):
        with self.connection() as db:
            row = dict(self.job(db, job))
            row['current_epoch'] = self.current(db)['epoch']
            row['candidates'] = [{'name': item['name'], **public_observation(item['raw'])}
                                 for item in db.execute('select * from candidates where job=? order by name', (job,))]
        return row

    def original(self, job, candidate):
        with self.connection() as db:
            row = db.execute('select raw from candidates where job=? and name=?', (job, candidate)).fetchone()
            if row is None:
                raise ValueError('unknown original candidate')
            return row['raw']

    def judge(self, job, expected, receiver, requirement, equality, use, expected_epoch):
        text(receiver, 'receiver')
        if requirement not in REQUIREMENTS or equality != EQUALITY or use not in ('current', 'historical'):
            raise ValueError('choose an installed requirement, equality rule and receiving use')
        wanted = observe(expected)
        if not (wanted['valid_json'] and wanted['object_root'] and wanted['unique_member_names']):
            raise ValueError('expected record must be an unambiguous JSON object')
        with self.connection() as db:
            row = self.job(db, job)
            current = self.current(db)
            candidates = {item['name']: observe(item['raw']) for item in
                          db.execute('select * from candidates where job=?', (job,))}
        selected = candidates.get(row['selected'])
        bad = [name for name, item in candidates.items()
               if item['valid_json'] is False or item['unique_member_names'] is False or
                   (item['valid_json'] is True and not item['object_root'])]
        unknown = [name for name, item in candidates.items() if item['valid_json'] is None]
        history = 'fail' if bad else 'pass' if not unknown and row['closed'] and row['prior_history'] == 'empty' else 'unknown'
        reasons = []
        if expected_epoch != row['epoch']:
            reasons.append('expected-source-epoch-differs')
        if use == 'current' and row['edition'] != current['sequence']:
            reasons.append('source-edition-changed')
        if not row['closed']:
            reasons.append('admission-window-open')
        if selected is None or not selected['valid_json'] or not same_value(wanted['value'], selected['value']):
            reasons.append('selected-record-differs')
        if requirement != 'normalized':
            if row['origin'] != 'original':
                reasons.append('original-input-authority-unavailable')
            if selected is None or not (selected['object_root'] and selected['unique_member_names']):
                reasons.append('selected-original-unqualified')
        if requirement == 'all-originals' and history != 'pass':
            reasons.append('original-history-' + history)
        return {'format': FORMAT, 'job': job, 'receiver': receiver, 'requirement': requirement,
                'equality': equality, 'use': use, 'epoch': row['epoch'], 'origin': row['origin'],
                'current_epoch': current['epoch'], 'closed': bool(row['closed']),
                'selected': row['selected'], 'selected_sha256': selected['sha256'] if selected else None,
                'expected_sha256': wanted['sha256'], 'candidate_count': len(candidates),
                'history': history, 'bad_candidates': bad, 'unobserved_candidates': unknown,
                'eligible': not reasons, 'reasons': reasons}


def read(path):
    with Path(path).open('rb') as stream:
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError('input exceeds the 2 MiB bound')
    return raw


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, required=True)
    commands = parser.add_subparsers(dest='command', required=True)
    init = commands.add_parser('init')
    init.add_argument('--epoch', required=True)
    init.add_argument('--origin', choices=('original', 'derived'), required=True)
    advance = commands.add_parser('advance')
    advance.add_argument('--epoch', required=True)
    advance.add_argument('--origin', choices=('original', 'derived'), required=True)
    advance.add_argument('--expected', required=True)
    opening = commands.add_parser('open')
    opening.add_argument('job')
    opening.add_argument('--expected-epoch', required=True)
    opening.add_argument('--prior-history', choices=('empty', 'unknown'), required=True)
    admit = commands.add_parser('admit')
    admit.add_argument('job')
    admit.add_argument('candidate')
    admit.add_argument('--source', type=Path, required=True)
    close = commands.add_parser('close')
    close.add_argument('job')
    close.add_argument('--selected', required=True)
    show = commands.add_parser('show')
    show.add_argument('job')
    original = commands.add_parser('original')
    original.add_argument('job')
    original.add_argument('candidate')
    judge = commands.add_parser('judge')
    judge.add_argument('job')
    judge.add_argument('--expected', type=Path, required=True)
    judge.add_argument('--receiver', required=True)
    judge.add_argument('--requirement', choices=REQUIREMENTS, required=True)
    judge.add_argument('--equality', choices=(EQUALITY,), required=True)
    judge.add_argument('--use', choices=('current', 'historical'), required=True)
    judge.add_argument('--expected-epoch', required=True)
    args = parser.parse_args()
    if args.command == 'init':
        Admission.initialize(args.db, args.epoch, args.origin)
        result = {'format': FORMAT, 'epoch': args.epoch, 'origin': args.origin}
    else:
        source = Admission(args.db)
        if args.command == 'advance':
            result = source.advance(args.epoch, args.origin, args.expected)
        elif args.command == 'open':
            result = source.open(args.job, args.expected_epoch, args.prior_history)
        elif args.command == 'admit':
            result = source.admit(args.job, args.candidate, read(args.source))
        elif args.command == 'close':
            result = source.close(args.job, args.selected)
        elif args.command == 'show':
            result = source.show(args.job)
        elif args.command == 'original':
            sys.stdout.buffer.write(source.original(args.job, args.candidate))
            return
        elif args.command == 'judge':
            result = source.judge(args.job, read(args.expected), args.receiver, args.requirement,
                                  args.equality, args.use, args.expected_epoch)
    print(json.dumps(result, sort_keys=True, indent=2))


if __name__ == '__main__':
    main()
