"""One participant's durable private draft and confirmed byte deliveries."""
from pathlib import Path
import json

from canary import encode, pin, sha, transaction


class Workbench:
    def __init__(self, private_file, actor, collective):
        self.path = Path(private_file)
        self.actor = actor
        self.collective = collective
        with transaction(self.path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS binding(actor,authority)')
            row = db.execute('SELECT * FROM binding').fetchone()
            binding = (actor,str(collective.path))
            if row and tuple(row) != binding:
                raise ValueError('Private workbench belongs to another participant or authority')
            if not row:
                db.execute('INSERT INTO binding VALUES(?,?)',binding)
            db.execute('CREATE TABLE IF NOT EXISTS drafts(target PRIMARY KEY,base,gen,body,incoming)')
            db.execute('CREATE TABLE IF NOT EXISTS submissions(command PRIMARY KEY,target,gen,base,body,received)')
            db.execute('CREATE TABLE IF NOT EXISTS deliveries(id PRIMARY KEY,receipt,body BLOB)')

    def open(self,target):
        head = self.collective.show(target)
        with transaction(self.path) as db:
            db.execute('INSERT OR IGNORE INTO drafts VALUES(?,?,0,?,NULL)',
                       (target,encode(pin(head)),encode(head['body'])))
        return self.view(target)

    def view(self,target):
        with transaction(self.path) as db:
            row = db.execute('SELECT * FROM drafts WHERE target=?',(target,)).fetchone()
            return {'base':json.loads(row['base']), 'generation':row['gen'], 'body':json.loads(row['body']),
                    'incoming':json.loads(row['incoming']) if row['incoming'] else None}

    def edit(self,target,expected,body):
        with transaction(self.path) as db:
            changed = db.execute('UPDATE drafts SET gen=gen+1, body=? WHERE target=? AND gen=?',
                                 (encode(body),target,expected))
            if changed.rowcount != 1:
                raise ValueError('Private generation changed')

    def submit(self,command,target,*,generation=None,base=None):
        """Freeze this generation. Retry captures/proposes/publishes the same exact operations."""
        with transaction(self.path) as db:
            old = db.execute('SELECT * FROM submissions WHERE command=?',(command,)).fetchone()
            if old is None:
                db.execute('INSERT INTO submissions SELECT ?,target,gen,base,body,NULL FROM drafts WHERE target=?',
                           (command,target))
                old = db.execute('SELECT * FROM submissions WHERE command=?',(command,)).fetchone()
            if old is None or old['target'] != target:
                raise ValueError('Unknown draft or conflicting save identity')
            if (generation is not None and generation!=old['gen']) or (base is not None and base!=json.loads(old['base'])):
                raise ValueError('Save requested a different private generation or base')
        def call(op, **fields):
            result = self.collective.call(dict(op=op, actor=self.actor, command=command+'-'+op,**fields))
            if result['status'] != 'accepted':
                raise ValueError(result['code'])
            return result
        captured = call('capture',id=command+'-ctx',target=target)['context']
        if captured['target'] != json.loads(old['base']):
            self.refresh(target)
            raise ValueError('Base advanced before submission')
        call('propose',id=command+'-proposal',context=captured['id'],body=json.loads(old['body']))
        return call('publish',proposal=command+'-proposal',account='Publish the submitted private generation.')

    def reconcile(self,command):
        receipt = self.collective.receipt(self.actor,command+'-publish')
        if not receipt or receipt['status'] != 'accepted':
            raise ValueError('Publication receipt is unavailable')
        with transaction(self.path) as db:
            sent = db.execute('SELECT * FROM submissions WHERE command=?',(command,)).fetchone()
            if not sent:
                raise ValueError('No corresponding private submission')
            target = sent['target']
            observed = self.collective.show(target)
            accepted = self.collective.show(target,receipt['artifact']['revision'])
            if (receipt['artifact']!=pin(accepted) or accepted['body']!=json.loads(sent['body'])
                    or accepted.get('previous')!=json.loads(sent['base'])
                    or accepted.get('proposal')!=command+'-proposal'):
                raise ValueError('Receipt does not match the submitted change')
            db.execute('UPDATE submissions SET received=? WHERE command=?',(encode(receipt),command))
            row = db.execute('SELECT * FROM drafts WHERE target=?',(target,)).fetchone()
            if (row['gen']==sent['gen'] and row['base']==sent['base']
                    and pin(observed)==pin(accepted)):
                db.execute('UPDATE drafts SET base=?,incoming=NULL WHERE target=?',(encode(pin(accepted)),target))
            elif pin(observed)!=json.loads(row['base']):
                db.execute('UPDATE drafts SET incoming=? WHERE target=?',(encode(pin(observed)),target))
        return self.view(target)

    def refresh(self,target):
        with transaction(self.path) as db:
            row = db.execute('SELECT base FROM drafts WHERE target=?',(target,)).fetchone()
            if not row:
                raise ValueError('Unknown draft')
            current = pin(self.collective.show(target))
            if current != json.loads(row['base']):
                db.execute('UPDATE drafts SET incoming=? WHERE target=?',(encode(current),target))
        return self.view(target)

    def merge(self,target,expected_generation,expected_incoming,merged_body):
        """Explicitly accept a new base while preserving the participant's chosen merged content."""
        with transaction(self.path) as db:
            row = db.execute('SELECT * FROM drafts WHERE target=?',(target,)).fetchone()
            if (not row or row['gen']!=expected_generation or row['incoming'] is None
                    or json.loads(row['incoming'])!=expected_incoming
                    or pin(self.collective.show(target))!=expected_incoming):
                raise ValueError('Draft or incoming shared revision changed')
            db.execute('UPDATE drafts SET base=?,body=?,gen=gen+1,incoming=NULL WHERE target=?',
                       (encode(expected_incoming),encode(merged_body),target))
        return self.view(target)

    def receive(self,delivery,target,rev,start,stop):
        """The endpoint stores the exact bytes it accepted and its receipt in one commit."""
        source = self.collective.show(target,rev)
        raw = encode(source['body'])
        if not (type(start) is int and type(stop) is int and 0 <= start < stop <= len(raw)):
            raise ValueError('Invalid exposure extent')
        part = raw[start:stop]
        receipt = {'authority':str(self.collective.path),'source':pin(source),'serialization':'G60 canonical JSON bytes',
                   'recipient':self.actor,'start':start,'stop':stop,'whole_bytes':len(raw),
                   'whole_sha256':sha(raw),'delivered_sha256':sha(part),'boundary':'accepted by private workbench'}
        with transaction(self.path) as db:
            old = db.execute('SELECT * FROM deliveries WHERE id=?',(delivery,)).fetchone()
            if old and (json.loads(old['receipt']) != receipt or old['body'] != part):
                raise ValueError('Delivery identity already names another exposure')
            if not old:
                db.execute('INSERT INTO deliveries VALUES(?,?,?)',(delivery,encode(receipt),part))
        return receipt

    def delivered(self,delivery):
        with transaction(self.path) as db:
            row = db.execute('SELECT body FROM deliveries WHERE id=?',(delivery,)).fetchone()
            return row['body'] if row else None
