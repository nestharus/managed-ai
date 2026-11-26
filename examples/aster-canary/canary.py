"""A trusted local Aster profile. The sender, collective writer and private desk own different state."""
from contextlib import closing, contextmanager
from pathlib import Path
import json
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / 'src'
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
from managed_ai.store.system import System
from managed_ai.collective.store import Collective, Session
from managed_ai.collective.store.contract import pin, require
from managed_ai.core.storage import encode, exclusive, read_json, sha
from managed_ai.store.protocol.inputs import accounted_closure


PROFILE = 'canary-west/1'
OWNER = 'release-desk'
ROUTE = 'measurements'
SOURCE = 'sample-source'


@contextmanager
def transaction(path):
    with closing(sqlite3.connect(path, timeout=10)) as db, db:
        db.row_factory = sqlite3.Row
        db.execute('BEGIN IMMEDIATE')
        yield db


class Canary:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.native = System(self.root/'investigation')
        self.collective = Collective(self.root/'authority.sqlite')
        self.outbox = self.root/'outbox.sqlite'

    @classmethod
    def initialize(cls, root):
        root = Path(root).resolve()
        root.mkdir(exist_ok=False)
        System.initialize(root/'investigation', {'contribution':'Decide the west canary.',
                          'canary':{'cohort':'west','limit_ms':100,'enabled':True}})
        Collective.initialize(root/'authority.sqlite')
        app = cls(root)
        with transaction(app.outbox) as db:
            db.execute('CREATE TABLE transfers(id PRIMARY KEY, request TEXT NOT NULL, outcome TEXT)')
        # Only the profile's transaction creates a canary issue. Ordinary collective
        # publication has its own meanings and cannot produce a row in this table.
        with closing(app.collective.database.connect()) as db, db:
            db.execute('CREATE TABLE canary_issues(id TEXT PRIMARY KEY, decision TEXT NOT NULL)')
        return app

    def lock(self):
        return exclusive(self.native.ex.root/'owner.lock')

    def occurrence(self, turn):
        directory, request = self.native.ex.load(turn)
        returned = read_json(directory/'return.json')
        self.native.ex.validate_return(directory, returned)
        mats = {m['path']:m for m in request['materials']}
        def captured(ident):
            key = self.native.record_store.material(ident)
            if key not in mats:
                raise ValueError('Required native input absent: '+ident)
            return json.loads(self.native.ex.get(mats[key]['sha256']))
        source, goal = captured(SOURCE), captured('goal')
        if not isinstance(goal['content'].get('canary'),dict):
            raise ValueError('Captured goal contains no canary direction')
        return {'native_owner':str(self.native.root), 'turn':turn,
                'request_sha256':returned['request_sha256'], 'return_sha256':returned['return_sha256'],
                'worker':returned['producer'], 'reply_sha256':returned['reply_sha256'],
                'source':pin(source), 'direction':goal['content']['canary'],
                'reply':json.loads(self.native.ex.get(returned['reply_sha256']))}

    def stage(self, turn):
        with self.lock():
            occ = self.occurrence(turn)
        dest = str(self.collective.path)
        key = sha(encode([occ['native_owner'],turn,occ['return_sha256'],dest,ROUTE]))
        request = {'actor':ROUTE, 'command':'deliver-'+key, 'op':'canary-handoff',
                   'id':'metric-'+key, 'destination':dest, 'occurrence':occ}
        raw = encode(request).decode()
        with transaction(self.outbox) as db:
            existing = db.execute('SELECT request FROM transfers WHERE id=?',(key,)).fetchone()
            if existing and existing[0] != raw:
                raise ValueError('Conflicting handoff occurrence')
            if not existing:
                db.execute('INSERT INTO transfers VALUES(?,?,NULL)',(key,raw))
        return key

    def entry(self, key):
        with transaction(self.outbox) as db:
            row = db.execute('SELECT * FROM transfers WHERE id=?',(key,)).fetchone()
            if row is None:
                raise KeyError('Unknown handoff')
            return json.loads(row['request'])

    def current_basis(self, record):
        return all(p.get('sha256') is not None for p in record['basis']) and not self.native.record_store.basis_changes(record)

    def current(self, occurrence):
        require(occurrence['native_owner']==str(self.native.root),'wrong-origin','Native owner differs')
        goal = self.native.show('goal')
        require(self.current_basis(goal),'direction-basis-changed',
                'Reassess the source of the current direction')
        direction = goal['content'].get('canary')
        require(isinstance(direction,dict) and direction == occurrence['direction'] and direction.get('enabled') is True and
                direction.get('cohort')=='west' and type(direction.get('limit_ms')) is int,
                'direction-changed','Reconsider the current west canary direction')
        source = self.native.show(SOURCE)
        require(pin(source)==occurrence['source'] and self.current_basis(source),
                'sample-changed','Return to the changed measurement source')
        return direction,goal

    def deliver(self, key, after_commit=None):
        req = self.entry(key)
        def handlers(db):
            session = Session(db,self.collective.clock)
            def admit(r):
                require(r['destination']==str(self.collective.path),'wrong-destination','Route has moved')
                self.current(r['occurrence'])
                # Preserve the worker and occurrence separately from the authorized courier.
                return session.create({'op':'create','actor':ROUTE,'command':r['command'],
                                       'id':r['id'],'body':r['occurrence']})
            return {'canary-handoff':admit}
        with self.lock():
            result = self.collective.database.execute(req, handlers)
        if after_commit:
            after_commit()
        with transaction(self.outbox) as db:
            db.execute('UPDATE transfers SET outcome=? WHERE id=?',(encode(result).decode(),key))
        return result

    def issue(self, actor, command, key, expected_metric, expected_plan, why, *, observation):
        req = self.entry(key)
        request = {'op':'canary-issue','actor':actor,'command':command,'transfer':key,
                   'metric':expected_metric,'plan':expected_plan,'observation':observation,'reason':why}
        def handlers(db):
            session = Session(db,self.collective.clock)
            def admit(r):
                require(actor==OWNER,'wrong-owner','This desk owns the canary decision')
                require(isinstance(why,str) and why.strip(),'missing-reason','Record the receiving decision')
                receipt = db.execute('SELECT request,result FROM command_receipts WHERE actor=? AND command=?',
                                     (ROUTE,req['command'])).fetchone()
                require(receipt and json.loads(receipt[0])==req,'missing-handoff','Require the exact delivery')
                delivered = json.loads(receipt[1])
                require(delivered.get('artifact')==r['metric'] and r['plan']['id']=='rollout-plan',
                        'wrong-components','Bind the delivered metric and the enrolled rollout plan')
                metric, plan = session.revisions.verify([r['metric'],r['plan']],current=True)
                seen, = session.revisions.verify([r['observation']],current=True)
                check = seen['body']
                require(seen['owner']==OWNER and seen['id'] not in (metric['id'],plan['id'])
                        and isinstance(check,dict) and check.get('format')=='canary-rollback-check/1'
                        and check.get('plan')==pin(plan) and check.get('restored') is True
                        and check.get('cohort')=='west', 'rollback-unqualified',
                        'The receiving desk must observe restoration for this exact rollout plan')
                require(metric['owner']==ROUTE and plan['owner']==OWNER,'wrong-owner','Check component writers')
                occ = req['occurrence']
                require(metric['body']==occ,'wrong-metric','Delivered material differs from source occurrence')
                direction,goal = self.current(occ)
                rows = list(goal['content'].get('obligations',[]))
                for record in self.native.records():
                    if record['kind'] != 'obligation':
                        continue
                    duty = record['content']
                    if isinstance(duty,dict) and duty.get('applies_to','all') not in (PROFILE,'all'):
                        continue
                    require(self.current_basis(record),'duty-basis-changed',
                            'Reassess the evidence for this canary duty')
                    rows.append(duty)
                require(not any(not isinstance(o,dict) or (o.get('applies_to','all') in (PROFILE,'all') and
                                not accounted_closure(o)) for o in rows),'duty-open','A relevant duty is open')
                for component in (metric,plan,seen):
                    require(not session.notices.has_pending_required(component),'review-required',
                            'A component has new required work')
                    external = [p for p in component['basis'] if p['id'] != component['id']]
                    session.revisions.verify(external,current=True,keep_bodies=False)
                    session.uses.require_current(component['id'])
                    if component.get('staged_treatments'):
                        require(not session.reviews.changed(component['proposal'],installed_target=component['id']),
                                'basis-changed','The evidence used to treat a required review changed')
                body = plan['body']
                measure = occ['reply']
                require(isinstance(body,dict) and body.get('cohort')=='west' and isinstance(body.get('rollback'),str) and body['rollback'].strip() and
                        isinstance(measure,dict) and measure.get('cohort')=='west' and
                        type(measure.get('p95_ms')) is int and 0 <= measure['p95_ms'] <= direction['limit_ms'],
                        'canary-outside-policy','Measurement, plan or rollback is insufficient')
                result = {'profile':PROFILE,'transfer':key,'native_goal_at_issue':pin(goal),
                          'source':occ['source'],'metric':pin(metric),'plan':pin(plan),'worker':occ['worker'],
                          'owner':actor,'observation':pin(seen),'cohort':body['cohort'],'p95_ms':measure['p95_ms'],
                          'rollback':body['rollback'],'reason':why}
                require(not db.execute('SELECT 1 FROM canary_issues WHERE id=?',(key,)).fetchone(),
                        'already-issued','Recover the original decision for this handoff')
                db.execute('INSERT INTO canary_issues VALUES(?,?)',(key,encode(result).decode()))
                return {'decision':result}
            return {'canary-issue':admit}
        # Native CLI writers obey this same POSIX lock. SQLite owns the decision and
        # collective component revisions. Always acquire native first, then SQLite.
        with self.lock():
            return self.collective.database.execute(request,handlers)

    def decision(self, key):
        with self.collective.database.read() as db:
            row = db.execute('SELECT decision FROM canary_issues WHERE id=?',(key,)).fetchone()
            return json.loads(row[0]) if row else None
