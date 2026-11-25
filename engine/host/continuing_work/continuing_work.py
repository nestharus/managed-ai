#!/usr/bin/env python3
"""Optional G60 host bridge: continuing commission, useful reminders and causal repair custody.

No model client, semantic scheduler, universal ontology or inferred completeness.
Uses the existing native runtime for context, Attention, receive/adopt and goal use.
"""
from __future__ import annotations
import argparse
import fcntl
import json
import os
import shutil
import stat
import subprocess
import sys
import threading
import runpy
from pathlib import Path
from tempfile import NamedTemporaryFile

HERE = Path(__file__).resolve().parent

# This entry also supports loading by an explicit filename from a deployment.
sys.path.insert(0, str(HERE))
from occurrence_files import atomic_bytes, digest, ident, loads_strict, nonempty, now, read, sync_dir, write
from repair_custody import RepairCustody

MAX_EVENT_STREAM_BYTES = 16 * 1024
MAX_EVENT_LINE_BYTES = 64 * 1024
_EVENT_LOCKS_GUARD = threading.Lock()
_EVENT_LOCKS: dict[Path, threading.Lock] = {}


def _event_thread_lock(path: Path) -> threading.Lock:
    key = path.resolve()
    with _EVENT_LOCKS_GUARD:
        lock = _EVENT_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _EVENT_LOCKS[key] = lock
        return lock


def _safe_jsonl(row) -> bytes:
    encoded = json.dumps(row, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
    # Escape Unicode line separators that str.splitlines() splits on.
    encoded = (encoded.replace('\u2028', '\\u2028')
                      .replace('\u2029', '\\u2029')
                      .replace('\u0085', '\\u0085'))
    if '\n' in encoded or '\r' in encoded:
        raise ValueError('event line framing contains unescaped newline')
    return (encoded + '\n').encode('utf-8')


def capture_error(raw):
    """Preserve known selected-capture errors, not arbitrary owner history."""
    try: detail=json.loads(raw).get('error')
    except (ValueError,AttributeError): detail=None
    safe={'disposition must be considered or deferred',
          'each assessment must name a distinct actually delivered item',
          'receipt identity already used','receipt identity not recorded in native Attention',
          'receipt must contain id and assessments',
          'invalid assessment fields','nonempty bounded assessments required',
          'Native return already exists; use its retained body or a new current occurrence',
          'Selected native engine does not match the reviewed capture adapter; requalify this adapter before capture'}
    if isinstance(detail,str) and detail in safe: return detail
    return ('Candidate capture could not be confirmed; raw failure details remain in owner evidence. '
            'Do not blindly repeat the receipt.')

def prepare_questions(supplied_questions, selected_sources, task):
    # Resolve the question input contract before any host/native effects.
    # Omission deliberately leaves a question unbound; an unknown spelling
    # or unresolved identity must never be converted into that omission.
    if not isinstance(selected_sources,list) or any(not isinstance(r,dict) for r in selected_sources):
        raise ValueError('sources must be a list of selected resource objects')
    selected_ids=[ident(r['id']) for r in selected_sources]
    if len(set(selected_ids))!=len(selected_ids): raise ValueError('Duplicate selected resource')
    if not isinstance(supplied_questions,list): raise ValueError('questions must be a list')
    questions=[]
    for i,q in enumerate(supplied_questions):
        label='questions['+str(i)+']'
        if not isinstance(q,dict): raise ValueError(label+' must be an object')
        unknown=set(q)-{'question','use','sources'}
        if unknown:
            raise ValueError(label+' has unsupported fields: '+', '.join(sorted(unknown))+
                             '; use sources for selected resource IDs')
        refs=q.get('sources',[])
        if not isinstance(refs,list) or any(not isinstance(r,str) for r in refs):
            raise ValueError(label+'.sources must be a list of selected resource IDs')
        if len(set(refs))!=len(refs): raise ValueError(label+'.sources contains duplicates')
        missing=set(refs)-set(selected_ids)
        if missing:
            raise ValueError(label+'.sources names unselected resource IDs: '+', '.join(sorted(missing)))
        questions.append({'question':nonempty(q.get('question'),label+'.question'),
                          'use':nonempty(q.get('use',task),label+'.use'),'sources':refs})
    return questions


def prepare_attention_selection(spec):
    # Native Attention owns the meaning, current treatment and active-ID
    # checks. Preserve the caller's explicit earlier selection instead of
    # silently replacing it with only this occurrence's new questions.
    attention={}
    for key,limit in (('attention_items',2000),('attention_groups',100)):
        value=spec.get(key,[])
        if not isinstance(value,list) or len(value)>limit or any(not isinstance(v,str) for v in value):
            raise ValueError(key+' must be a bounded list of strings')
        if len(value)!=len(set(value)): raise ValueError(key+' contains duplicates')
        attention[key]=value
    return attention


def overlap_rows(previous_scopes, sources, reads, writes):
    """Nominate declared footprints and observed source/output identity, without semantic ranking."""
    rows=[]; a=set(reads); b=set(writes)
    current_by_hash={}
    for source in sources: current_by_hash.setdefault(source['sha256'],[]).append(source['id'])
    for previous in previous_scopes:
        c=set(previous['reads']); d=set(previous['writes'])
        conflict=sorted((b & (c|d)) | (d & a)); shared=sorted(a&c)
        prior_by_hash={}
        for source in previous['sources']: prior_by_hash.setdefault(source['sha256'],[]).append(source['id'])
        same=[{'sha256':key,'current_source_ids':current_by_hash[key],'prior_source_ids':prior_by_hash[key]}
              for key in sorted(current_by_hash.keys() & prior_by_hash.keys())]
        output_uses=[{'sha256':out['sha256'],'current_source_ids':current_by_hash[out['sha256']],
                      'received_output':out['path']}
                     for out in previous.get('result',{}).get('outputs',[]) if out['sha256'] in current_by_hash]
        if not (conflict or shared or same or output_uses): continue
        rows.append({'scope':previous['id'],'prepared_at_utc':previous['prepared_at_utc'],
                     'read_write':conflict,'shared_reads':shared,'shared_source_bodies':same,
                     'received_output_uses':output_uses,
                     'scope_state':'returned' if 'result' in previous else 'superseded-without-output' if 'superseded' in previous else 'prepared-or-unreconciled',
                     'successor':previous.get('superseded',{}).get('successor'),
                     'return_sha256':previous.get('result',{}).get('return',{}).get('return_sha256')})
    # These categories expose concrete relationships first; they are not inferred importance.
    rows.sort(key=lambda r:(bool(r['received_output_uses']),bool(r['read_write']),
                            r['scope_state']=='prepared-or-unreconciled',r['prepared_at_utc'],r['scope']),reverse=True)
    return rows

class Host(RepairCustody):
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.config_path = self.root/'host.json'
        self.config = read(self.config_path) if self.config_path.exists() else None
        self.native = (self.root/(self.config or {}).get('native_root','native')).resolve()
        self.work = self.native/'work'

    @property
    def engine(self):
        return str((self.root/self.config['engine']).resolve())

    def _put_object_bytes(self, raw: bytes) -> tuple[str, str]:
        sha = digest(raw)
        obj = self.root / 'objects' / sha
        if obj.is_symlink() or obj.exists():
            if obj.is_symlink() or not obj.is_file():
                raise ValueError(f'Retained object must be an ordinary file: {obj}')
            existing = obj.read_bytes()
            if existing != raw or digest(existing) != sha:
                raise ValueError(f'Retained object integrity mismatch for {sha}')
        else:
            atomic_bytes(obj, raw)
        return str(obj.relative_to(self.root)), sha

    def _bound_event_value(self, value: dict) -> dict:
        bounded = dict(value)
        for field in ('stdout', 'stderr'):
            text = bounded.get(field)
            if isinstance(text, str):
                raw = text.encode('utf-8', errors='replace')
                if len(raw) > MAX_EVENT_STREAM_BYTES:
                    obj_rel, sha = self._put_object_bytes(raw)
                    bounded[field] = raw[:MAX_EVENT_STREAM_BYTES].decode('utf-8', errors='replace')
                    bounded[field + '_truncated'] = True
                    bounded[field + '_bytes'] = len(raw)
                    bounded[field + '_sha256'] = sha
                    bounded[field + '_object'] = obj_rel
        return bounded

    def event(self, action, value):
        self.root.mkdir(parents=True, exist_ok=True)
        bounded = self._bound_event_value(value)
        row = {'at_utc': now(), 'action': action, **bounded}
        line = _safe_jsonl(row)
        written_row = row
        if len(line) > MAX_EVENT_LINE_BYTES:
            obj_rel, sha = self._put_object_bytes(line)
            summary = {'at_utc': row['at_utc'], 'action': action, 'truncated': True,
                       'event_bytes': len(line), 'event_sha256': sha, 'event_object': obj_rel}
            for key in ('id', 'receipt', 'exit'):
                if key in row and len(_safe_jsonl(row[key])) <= 1024:
                    summary[key] = row[key]
            if isinstance(row.get('argv'), list) and len(_safe_jsonl(row['argv'])) <= 4096:
                summary['argv'] = row['argv']
            line = _safe_jsonl(summary)
            written_row = summary
        events_path = self.root / 'EVENTS.jsonl'
        with _event_thread_lock(events_path):
            if events_path.is_symlink():
                raise ValueError('EVENTS.jsonl must be an ordinary file')
            created = not events_path.exists()
            with events_path.open('ab') as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                try:
                    if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
                        raise ValueError('EVENTS.jsonl must be an ordinary file')
                    f.write(line)
                    f.flush()
                    os.fsync(f.fileno())
                finally:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            if created:
                sync_dir(self.root)
        return written_row

    def call(self, *args, scoped_errors=False):
        if self.config.get('attached_instance_id') and read(self.native/'system.json').get('instance_id') != self.config['attached_instance_id']:
            raise ValueError('attached native store identity changed')
        argv = [sys.executable, '-B', self.engine, '--root', str(self.native), *map(str,args)]
        p = subprocess.run(argv, capture_output=True, text=True)
        self.event('native-call', {'argv':argv, 'exit':p.returncode, 'stdout':p.stdout, 'stderr':p.stderr})
        if p.returncode:
            raise ValueError(capture_error(p.stderr or p.stdout) if scoped_errors else p.stderr or p.stdout)
        return json.loads(p.stdout)

    def inspect_native(self,sid,scoped_errors=False):
        engine=Path(self.engine).with_name('exchange_packets.py' if Path(self.engine).name == 'manage_store.py' else 'work.py')
        argv=[sys.executable,'-B',str(engine),'--root',str(self.native/'exchange'),'inspect',ident(sid)]
        p=subprocess.run(argv,capture_output=True,text=True)
        self.event('native-inspection',{'argv':argv,'exit':p.returncode,'stdout':p.stdout,'stderr':p.stderr})
        if p.returncode: raise ValueError(capture_error(p.stderr or p.stdout) if scoped_errors else p.stderr or p.stdout)
        return json.loads(p.stdout)

    def capture_native(self,sid,receipt,producer):
        """Use the qualified host adapter over the unchanged selected engine."""
        adapter=HERE/'capture_native.py'
        argv=[sys.executable,'-B',str(adapter),'--engine',self.engine,
              '--root',str(self.native),'assess',sid,'--receipt',str(receipt),'--producer',producer]
        p=subprocess.run(argv,capture_output=True,text=True)
        self.event('native-call',{'argv':argv,'exit':p.returncode,'stdout':p.stdout,'stderr':p.stderr,
                                 'capture_adapter':self.pin(adapter)})
        if p.returncode: raise ValueError(capture_error(p.stderr or p.stdout))
        return json.loads(p.stdout)

    def reconcile_capture_native(self,sid,receipt,producer):
        """Verify an already-committed native Attention receipt for crash recovery."""
        adapter=HERE/'capture_native.py'
        argv=[sys.executable,'-B',str(adapter),'--engine',self.engine,
              '--root',str(self.native),'reconcile-assess',sid,'--receipt',str(receipt),'--producer',producer]
        p=subprocess.run(argv,capture_output=True,text=True)
        self.event('native-capture-reconciliation',{'argv':argv,'exit':p.returncode,'stdout':p.stdout,'stderr':p.stderr,
                                                    'capture_adapter':self.pin(adapter)})
        if p.returncode: raise ValueError(capture_error(p.stderr or p.stdout))
        return json.loads(p.stdout)

    def temp(self, name, value):
        p=self.root/'requests'/name
        write(p,value)
        return p

    def pin(self, path):
        p=Path(path).resolve()
        raw=p.read_bytes()
        obj_rel, sha=self._put_object_bytes(raw)
        return {'path':str(p),'sha256':sha,'bytes':len(raw),'object':obj_rel}

    def producer_dependencies(self):
        return [self.pin(HERE / name) for name in ('occurrence_files.py', 'repair_custody.py')]

    def goal(self):
        return self.call('show','goal')

    def current(self, pin, label):
        path=Path(pin['path'])
        actual=digest(path.read_bytes()) if path.is_file() else None
        if actual!=pin['sha256']:
            self.event('changed-current-source',{'label':label,'captured':pin,'current_sha256':actual})
            raise ValueError(label+' changed outside this captured occurrence; preserve the proposal and prepare current work')

    def received_outputs(self,scope):
        if 'result' not in scope: raise ValueError('Scope has no actual received output')
        for pin in scope['result']['outputs']:
            self.current(pin,'Received output '+scope['id'])
            obj=self.root/pin['object']
            if not obj.is_file() or digest(obj.read_bytes())!=pin['sha256']:
                raise ValueError('Retained received-output object differs')
        return scope['result']['outputs']

    def init(self, spec):
        if self.config is not None or self.root.exists() and any(self.root.iterdir()):
            raise ValueError('Retain the existing host; initialize only a new empty root')
        engine=Path(spec['engine']).resolve()
        if not engine.is_file(): raise ValueError('Current native engine is absent')
        commission=Path(spec['commission']).resolve()
        duties=spec['duties']
        if not duties: raise ValueError('This continuing-work route requires explicit commissioned duties')
        for row in duties:
            ident(row['id']); nonempty(row['text'],'duty text')
        if len({r['id'] for r in duties})!=len(duties): raise ValueError('Duplicate duty')
        self.root.mkdir(parents=True,exist_ok=True)
        self.config={'format':'g60-continuing-work-host/1','engine':os.path.relpath(engine,self.root),'created_at_utc':now(),
                     'commission':self.pin(commission), 'scope':'Caller-enrolled continuing work; semantic completeness is not inferred.'}
        if spec.get('native_root'):
            self.native=Path(spec['native_root']).resolve()
            self.work=self.native/'work'
            descriptor=read(self.native/'system.json')
            self.config.update(native_root=os.path.relpath(self.native,self.root),
                               attached_instance_id=descriptor['instance_id'])
            status=self.call('status')
            supplied=set(status['owed_record_obligations']) | {r['id'] for r in status['owed_goal_obligations']}
            if any(row['id'] not in supplied for row in duties):
                raise ValueError('attached host duties must name existing owed native obligations')
            write(self.config_path,self.config)
            return {'root':str(self.root),'native':str(self.native),'attached':True,
                    'instance_id':descriptor['instance_id'],'goal':status['current_goal'],
                    'boundary':'The existing native store retains goals and duty ownership. Attachment creates no new runtime or artificial participant return.'}
        write(self.config_path,self.config)
        initial={'id':'goal','kind':'goal','content':{'contribution':commission.read_text(),
                 'obligations':[dict(r,disposition='open') for r in duties]}}
        result=self.call('init','--goal',self.temp('init-goal.json',initial))
        (self.work/'sources/host').mkdir(parents=True,exist_ok=True)
        shutil.copyfile(commission,self.work/'sources/host/COMMISSION.md')
        shutil.copyfile(HERE/'OPERATION.md',self.work/'sources/host/OPERATION.md')
        current=self.goal()
        bound={'id':'goal','kind':'goal','content':current['content'],
               'basis':['sources/host/COMMISSION.md']}
        self.call('put','--record',self.temp('bind-commission.json',bound),'--expected',current['revision'])
        field=self.work/'sources/host/FIELD.md'
        field.write_text('The full commission remains active. A local contribution may finish while other duties remain owed. Reopen broader alternatives when evidence changes the useful contribution. Discover unrepresented relationships; the host only routes nominated ones.\n')
        question={'field':'sources/host/FIELD.md','items':[{'id':'continuing-commission',
            'question':'Which commissioned effects remain owed beyond this local contribution?',
            'use':'Keep current work aligned with the containing commission.',
            'basis':['sources/host/COMMISSION.md','records/goal.json'],'groups':['continuation']}]}
        self.call('track','--spec',self.temp('initial-attention.json',question))
        reply=self.root/'requests/start-receipt.md'
        reply.write_text('Host initialized exact commission, explicit duties and the continuing commission Attention item. This is enrollment, not a model reading or semantic completion claim.\n')
        self.call('receive','start','--reply',reply,'--producer','continuing-work enrollment adapter')
        return {'root':str(self.root),'native':str(self.native),'goal':self.goal(),
                'boundary':'Subsequent actual capable participant must read the prepared context.'}

    def bind_continuation(self,spec):
        """Add current-goal dependency to an existing host-owned inquiry through native APIs."""
        if set(spec)!={'id','account'}:
            raise ValueError('Continuation binding requires a fresh id and account only')
        operation=ident(spec['id']); account=nonempty(spec['account'],'continuation binding account')
        destination=self.root/'continuation-bindings'/f'{operation}.json'
        if destination.exists():
            raise ValueError('Retain the existing continuation binding; use a fresh operation identity')
        self.current(self.config['commission'],'Current commission')
        goal=self.goal()
        if not (self.work/'records/goal.json').is_file():
            raise ValueError('Current native goal material is absent')
        # Public, item-filtered feedback obtains the actual current specification.
        # Its administrative observation is not a semantic consideration receipt.
        command=[sys.executable,'-B',str(Path(self.engine).with_name('track_attention.py' if Path(self.engine).name == 'manage_store.py' else 'attend.py')),
                 '--root',str(self.native/'exchange'),'feedback',operation,
                 '--item','continuing-commission','--limit','1']
        result=subprocess.run(command,capture_output=True,text=True)
        self.event('continuation-binding-observation',{'argv':command,'exit':result.returncode,
                                                     'stdout':result.stdout,'stderr':result.stderr})
        if result.returncode:
            raise ValueError(result.stderr or result.stdout)
        observed=json.loads(result.stdout)
        if observed['omitted'] or len(observed['delivered'])!=1 or observed['delivered'][0]['id']!='continuing-commission':
            raise ValueError('The continuing inquiry was not completely delivered; preserve the observation and inspect it')
        before=observed['delivered'][0]['spec']
        after=dict(before,basis=sorted(set(before['basis'])|{'records/goal.json'}))
        changed=after!=before
        tracked=None
        if changed:
            tracked=self.call('track','--spec',self.temp(operation+'-continuation-binding.json',{'items':[after]}))
        record={'id':operation,'at_utc':now(),'account':account,
                'goal':{k:goal[k] for k in ('revision','sha256')},
                'observation':{'id':observed['id'],'object':observed['feedback_object'],
                               'context_sha256':observed['context_sha256']},
                'before':before,'current':after,'changed':changed,'track':tracked,
                'boundary':'Basis enrollment only. Historical accounts remain unchanged; the next actual feedback and semantic receiving determine current applicability. External effects absent from the basis are not inferred.'}
        write(destination,record)
        self.event('continuation-basis-bound',{'id':operation,'record':self.pin(destination),'changed':changed})
        return record

    def commission(self,spec):
        """Explicitly revise current instructions while preserving earlier enrolled duties."""
        old=self.config['commission']; new=self.pin(spec['path']); goal=self.goal()
        duties={r['id']:dict(r) for r in goal['content']['obligations']}
        for row in spec.get('duties',[]):
            rid=ident(row['id']); nonempty(row['text'],'duty text')
            if rid in duties and duties[rid]['text']!=row['text']:
                raise ValueError('Do not silently replace an earlier duty; add a new identity or reopen/account for it')
            if rid not in duties: duties[rid]=dict(row,disposition='open')
        target=self.work/'sources/host/COMMISSION.md'
        atomic_bytes(target,(self.root/new['object']).read_bytes())
        content={'contribution':target.read_text(),'obligations':list(duties.values()),
                 'continuation_account':nonempty(spec['account'],'commission continuation account')}
        record={'id':'goal','kind':'goal','content':content,'basis':['sources/host/COMMISSION.md']}
        result=self.call('put','--record',self.temp('commission-'+str(goal['revision'])+'.json',record),'--expected',goal['revision'])
        self.config['commission']=new; write(self.config_path,self.config)
        self.event('commission-continued',{'before':old,'current':new,'account':spec['account'],
                                         'preserved_duties':list(duties)})
        return result

    def upgrade(self,spec):
        """Select a regenerated host/guidance release with byte-identical native programs."""
        new=Path(spec['engine']).resolve(); old=Path(self.engine)
        if not new.is_file(): raise ValueError('New installed engine is absent')
        native_sources = runpy.run_path(str(HERE / 'capture_native.py'))['native_sources']
        if native_sources(old)[1] != native_sources(new)[1]:
            raise ValueError('This upgrade route requires identical native programs; a native-code change needs its own compatibility receiving')
        account=nonempty(spec['account'],'upgrade account')
        before=self.call('status'); previous=dict(self.config)
        self.config=dict(self.config,engine=os.path.relpath(new,self.root))
        try: after=self.call('status')
        except Exception:
            self.config=previous
            raise
        if before['current_goal']['sha256']!=after['current_goal']['sha256']:
            self.config=previous
            raise ValueError('Current goal changed while checking the selected release')
        write(self.config_path,self.config)
        event=self.event('producing-release-selected',{'before_engine':self.pin(old),'current_engine':self.pin(new),
            'host_producer':self.pin(Path(__file__).resolve()),
               'host_dependencies':self.producer_dependencies(),'host_method':self.pin(HERE/'OPERATION.md'),
            'account':account,'native_programs_unchanged':True,'prior_captures_unchanged':True})
        return {'engine':str(new),'host_producer':event['host_producer'],'host_method':event['host_method'],'host_dependencies':event['host_dependencies'],
                'native_programs_unchanged':True,'parent_duties_unchanged':True}

    def refresh_resource(self, rid, source):
        rid=ident(rid); old_path=self.root/'resources'/f'{rid}.json'
        previous=read(old_path) if old_path.exists() else None
        pin=self.pin(source)
        suffix=Path(source).suffix or '.txt'
        # The stable resource path is the relevant native publication basis.
        path='sources/resources/'+rid+suffix
        if previous and previous['work_path']!=path:
            raise ValueError('A resource retains its file type/path; use a new identity for another interpretation')
        target=self.work/path; target.parent.mkdir(parents=True,exist_ok=True)
        if not target.exists() or digest(target.read_bytes())!=pin['sha256']:
            atomic_bytes(target,(self.root/pin['object']).read_bytes())
        row={'id':rid, **pin,'work_path':path}
        write(old_path,row)
        return row, previous if previous and previous['sha256']!=row['sha256'] else None

    resource = refresh_resource  # Earlier host callers use this spelling.

    def scopes(self):
        return [read(p) for p in sorted((self.root/'scopes').glob('*.json'))]

    def _write_overlap_index(self, sid, sources, resource_changes, reads, writes, overlap_limit):
        changed = list(resource_changes)
        previous_scopes=self.scopes()
        for source in sources:
            if any(c['resource']==source['id'] for c in changed): continue
            prior=[r for old_scope in sorted(previous_scopes,key=lambda x:x['prepared_at_utc'],reverse=True)
                   for r in old_scope['sources'] if r['path']==source['path'] and r['sha256']!=source['sha256']]
            if prior:
                changed.append({'resource':source['id'],'before':prior[0],'current':source,
                                'nomination_basis':'same selected current source path under possibly different resource IDs'})
        for change in changed:
            change['affected_prior_consumers']=[s['id'] for s in previous_scopes
                if any((r['id']==change['resource'] or r['path']==change['current']['path']) and r['sha256']!=change['current']['sha256']
                       for r in s['sources'])]
        all_overlaps=overlap_rows(previous_scopes,sources,reads,writes)
        affected={name for change in changed for name in change['affected_prior_consumers']}
        # Keep current selected output use ahead of affected-history nominees.
        # Stable sorting preserves the existing order within each category.
        all_overlaps.sort(key=lambda r:(bool(r['received_output_uses']),r['scope'] in affected),reverse=True)
        overlaps=all_overlaps[:overlap_limit]
        index=self.root/'overlap-index'/f'{sid}.json'
        write(index,{'scope':sid,'previous_scope_count':len(previous_scopes),'rows':all_overlaps,
                     'meaning':'Author-declared footprints plus exact same-source/received-output identity. Shared reading need not conflict. Disjoint identities do not establish semantic independence.'})
        overlap_pin=self.pin(index)
        coverage = {'previous_scopes':len(previous_scopes),'matching_scopes':len(all_overlaps),
                'delivered':len(overlaps),'omitted':len(all_overlaps)-len(overlaps),'limit':overlap_limit,
                'omitted_scope_sample':[r['scope']for r in all_overlaps[overlap_limit:overlap_limit+20]],
                'complete_index':overlap_pin,
                'meaning':'Nominations, not inferred conflicts, automatic scheduling or semantic completeness. Acquire the complete index when omitted or new relationships matter.'}
        return changed, overlaps, coverage

    def prepare(self, spec):
        sid=ident(spec['id']); saved=self.root/'scopes'/f'{sid}.json'
        if saved.exists(): raise ValueError('A scope is one immutable occurrence; select a fresh identity')
        task=nonempty(spec['task'],'task'); prediction=nonempty(spec['prediction'],'prediction')
        selected_sources = spec.get('sources', [])
        questions = prepare_questions(spec.get('questions', []), selected_sources, task)
        for ordinal, question in enumerate(questions, 1):
            question['id'] = ident(f'{sid}-q{ordinal}')
        resource_paths = [(item, Path(item['path'])) for item in selected_sources]
        attention = prepare_attention_selection(spec)
        overlap_limit=spec.get('overlap_limit',8)
        if type(overlap_limit) is not int or not 1<=overlap_limit<=200:
            raise ValueError('overlap_limit must be an integer from1through200')
        outnames=[ident(n) for n in spec['outputs']]
        if not outnames or len(set(outnames))!=len(outnames): raise ValueError('Select distinct new output names')
        targets=['outputs/'+sid+'/'+n for n in outnames]
        reads=list(dict.fromkeys([*spec.get('reads',[]),*(r['id'] for r in selected_sources)]))
        writes=list(dict.fromkeys(spec.get('writes',[])))

        duty_ids = spec['duty_ids']
        requested_duties = set(duty_ids)
        if not requested_duties:
            raise ValueError('Bind actual current parent duties')
        repair_ids = [ident(value) for value in spec.get('repairs', [])]

        self.current(self.config['commission'],'Current commission')
        goal=self.goal()
        known={r['id']:r for r in goal['content']['obligations']}
        if requested_duties-known.keys(): raise ValueError('Bind actual current parent duties')
        sources=[]; resource_changes=[]
        for item, path in resource_paths:
            row,old=self.refresh_resource(item['id'],path)
            row=dict(row,role=item.get('role','source'))
            sources.append(row)
            if old: resource_changes.append({'resource':row['id'],'before':old,'current':row})
        changed, overlaps, coverage = self._write_overlap_index(sid, sources, resource_changes,
                                                               reads, writes, overlap_limit)
        if any((self.work/p).exists() for p in targets): raise ValueError('Fresh generation targets must be absent')
        # Native publication deliberately requires an existing parent at capture.
        for target in targets: (self.work/target).parent.mkdir(parents=True,exist_ok=True)
        repairs = self._generation_repairs(repair_ids, sources)
        source_note={'scope':sid,'prediction':prediction,'duty_ids':duty_ids,
            'changed_sources':changed,'overlap_nominations':overlaps,
            'overlap_coverage':coverage,
            'authored_relations':spec.get('relations',[]),
            'selected_treatments':spec.get('treatments',[]),
            'unknowns':spec.get('unknowns',['Unnominated semantic dependencies may remain.']),
            'repairs':repairs,
            'receiving_question':spec.get('receiving_question','Did the actual contribution produce the predicted useful effect under current sources?')}
        note='sources/host/'+sid+'-prospect.json'; write(self.work/note,source_note)
        items=[]
        for q in questions:
            item={'id':q['id'], 'question':q['question'],'use':q['use'],
                  'basis':[r['work_path'] for r in sources if r['id'] in q['sources']],
                  'groups':[sid]}
            items.append(item)
        if changed:
            items.append({'id':ident(sid+'-changed-basis'),
                'question':'How do these changed sources alter current dependent work or previously adequate partials?',
                'use':task,'basis':[note,*[r['current']['work_path'] for r in changed]],'groups':[sid]})
        if items: self.call('track','--spec',self.temp(sid+'-attention.json',{'items':items}))
        request={'task':task,'consumer':spec.get('consumer',sid),
                 'sources':[],
                 'targets':targets,
                 'attention_items':list(dict.fromkeys(['continuing-commission',*[r['id'] for r in items],*attention['attention_items']])),
                 'attention_groups':attention['attention_groups'],
                 'attention_limit':spec.get('attention_limit',max(8,len(items)+1))}
        if 'attention_max_bytes' in spec:
            request['attention_max_bytes']=spec['attention_max_bytes']
        method_raw=(HERE/'OPERATION.md').read_bytes(); method_sha=digest(method_raw)
        method_path='sources/host/method-'+method_sha+'.md'; method_file=self.work/method_path
        if method_file.exists() and method_file.read_bytes()!=method_raw:
            raise ValueError('Retained host method identity differs')
        if not method_file.exists(): atomic_bytes(method_file,method_raw)
        request['sources']=['sources/host/COMMISSION.md',method_path,note,*[r['work_path']for r in sources]]
        for key in ('techniques','discovery','compositions'):
            if key in spec: request[key]=spec[key]
        result=self.call('context',sid,'--request',self.temp(sid+'-request.json',request))
        recover=self.call('recover',sid)
        context_file=self.root/'contexts'/f'{sid}.md'
        atomic_bytes(context_file,recover['context'].encode('utf-8'))
        scope={'format':'g60-continuing-work-scope/1','id':sid,'prepared_at_utc':now(),
               'host_producer':self.pin(Path(__file__).resolve()),
               'host_dependencies':self.producer_dependencies(),
               'host_method':self.pin(HERE/'OPERATION.md'),
               'spec':spec,'parent_goal':{k:goal[k] for k in ('revision','sha256')},
               'reads':reads,'writes':writes,'sources':sources,'targets':targets,
               'repairs':repairs,'nominations':source_note,'context':str(context_file),'native_context':result}
        write(saved,scope)
        retained=self.pin(saved)
        self.event('prepared',{'id':sid,'scope':retained,'context_sha256':digest(context_file.read_bytes())})
        return {'id':sid,'context':str(context_file),'selected_source_bytes':sum(r['bytes'] for r in sources),
                'changed_sources':len(changed),'overlap_nominations':len(overlaps),'targets':targets,
                'parent_owed':[r['id'] for r in known.values() if r['disposition'] not in ('fulfilled','released')]}

    @staticmethod
    def _capture_record(sid, result):
        attention=result['attention']; status=result['status']
        matching=[r for r in status.get('turns',[]) if r.get('packet')==sid]
        turn=({'id':sid,'transport':matching[0]['transport'],
               'publication_count':len(matching[0].get('publications',[]))}
              if len(matching)==1 else {'id':sid,'transport':'unavailable','publication_count':None})
        # Use an explicit outward allowlist. The selected adapter result remains
        # in owner evidence; it is never this constructor's unfiltered response.
        return {'id':sid,'attention':{k:attention[k] for k in ('receipt','object','recorded','boundary')},
                'turn':turn,'parent_duties_closed':False,
                'scope':'Candidate custody and requested-turn status only; no unrelated history or parent-duty inventory. This is not a completed return, semantic verdict or access-control boundary.'}

    def _pending_capture_paths(self, sid):
        folder = self.root / 'captures' / sid
        if not folder.is_dir():
            return []
        return sorted(folder.glob('.pending-*.json'))

    def _reconcile_pending_captures(self, sid):
        reconciled = []
        (self.root / 'requests').mkdir(parents=True, exist_ok=True)
        for pending_path in self._pending_capture_paths(sid):
            pending = read(pending_path)
            rid = ident(pending['receipt'])
            producer = nonempty(pending['producer'], 'producer')
            target = self.root / 'captures' / sid / f'{rid}.json'
            if target.exists():
                pending_path.unlink(missing_ok=True)
                continue
            obj_path = self.root / pending['receipt_object']
            if obj_path.is_symlink() or not obj_path.is_file():
                raise ValueError(f'Retained pending receipt object is unavailable: {rid}')
            body = obj_path.read_bytes()
            if digest(body) != pending['receipt_sha256']:
                raise ValueError(f'Retained pending receipt object integrity mismatch: {rid}')
            with NamedTemporaryFile(dir=self.root / 'requests', suffix='.json') as selected:
                selected.write(body)
                selected.flush()
                try:
                    result = self.reconcile_capture_native(sid, Path(selected.name), producer)
                except ValueError as exc:
                    if str(exc) == 'receipt identity not recorded in native Attention':
                        pending_path.unlink(missing_ok=True)
                        self.event('pending-capture-uncommitted', {'id': sid, 'receipt': rid})
                        continue
                    raise
            capture = self._capture_record(sid, result)
            write(target, capture)
            pending_path.unlink(missing_ok=True)
            self.event('candidate-captured', {'id': sid, 'receipt': rid, 'capture': self.pin(target),
                                              'reconciled_from_native': True})
            reconciled.append(capture)
        return reconciled

    def capture(self,sid,spec):
        """Retain products through native Attention without exposing global status."""
        sid=ident(sid); scope=read(self.root/'scopes'/f'{sid}.json')
        if set(spec)!={'receipt','producer'}:
            raise ValueError('Candidate capture requires receipt and producer only')
        if 'result' in scope or 'superseded' in scope:
            raise ValueError('Candidate capture uses an open producing scope, not a completed or superseded one')
        producer=nonempty(spec['producer'],'producer')
        receipt=Path(spec['receipt']).resolve()
        with receipt.open('rb') as submitted:
            if not stat.S_ISREG(os.fstat(submitted.fileno()).st_mode):
                raise ValueError('An ordinary assessment receipt file is required')
            body=submitted.read()
        rid=ident(loads_strict(body)['id'])
        target=self.root/'captures'/sid/f'{rid}.json'
        if target.exists():
            raise ValueError('Retain the existing candidate capture; use a fresh receipt for a new occurrence')
        receipt_obj, receipt_sha = self._put_object_bytes(body)
        pending_path=self.root/'captures'/sid/f'.pending-{rid}.json'
        created_pending = not pending_path.exists()
        if created_pending:
            write(pending_path, {'id': sid, 'receipt': rid, 'producer': producer,
                                 'receipt_object': receipt_obj, 'receipt_sha256': receipt_sha,
                                 'started_at_utc': now()})
        # The adapter checks the selected native return under the same native
        # owner lock as assessment, without constructing global status.
        # The host lock does not protect the caller's path. Give native strict
        # JSON validation and Attention exactly the bytes used to name this capture.
        reconciled=False
        (self.root/'requests').mkdir(parents=True,exist_ok=True)
        with NamedTemporaryFile(dir=self.root/'requests', suffix='.json') as selected:
            selected.write(body)
            selected.flush()
            try:
                result=self.capture_native(sid,Path(selected.name),producer)
            except ValueError as exc:
                if str(exc) != 'receipt identity already used':
                    if created_pending and str(exc) in {
                        'disposition must be considered or deferred',
                        'each assessment must name a distinct actually delivered item',
                        'receipt must contain id and assessments',
                        'invalid assessment fields',
                        'nonempty bounded assessments required',
                        'Native return already exists; use its retained body or a new current occurrence',
                        'Selected native engine does not match the reviewed capture adapter; requalify this adapter before capture',
                    }:
                        pending_path.unlink(missing_ok=True)
                    raise
                try:
                    result=self.reconcile_capture_native(sid,Path(selected.name),producer)
                except ValueError as reconcile_exc:
                    if created_pending and str(reconcile_exc) in {
                        'receipt identity already used',
                        'receipt must contain id and assessments',
                    }:
                        pending_path.unlink(missing_ok=True)
                    raise
                reconciled=True
        capture=self._capture_record(sid,result)
        write(target,capture)
        pending_path.unlink(missing_ok=True)
        event_data={'id':sid,'receipt':rid,'capture':self.pin(target)}
        if reconciled:
            event_data['reconciled_from_native']=True
        self.event('candidate-captured',event_data)
        return capture

    def _record_partial_publication(self, path, scope, *, producer, account, returned, publications,
                                    published_outputs, pending_targets, state, failed_target=None, error=None):
        entry={'at_utc':now(),'producer':producer,'account':account,'return':returned,
               'publications':list(publications),'outputs':list(published_outputs),
               'pending_targets':list(pending_targets),'state':state}
        if failed_target is not None:
            entry['failed_target']=failed_target
        if error is not None:
            entry['error']=error
        scope['partial_publication']=entry
        write(path,scope)
        if state!='publishing':
            event_payload={'id':scope['id'],'scope':self.pin(path),
                           'published_targets':[p['path'] for p in publications],
                           'pending_targets':list(pending_targets)}
            if failed_target is not None:
                event_payload['failed_target']=failed_target
            if error is not None:
                event_payload['error']=error
            self.event('partial-publication',event_payload)

    def receive(self,sid,spec):
        sid=ident(sid); path=self.root/'scopes'/f'{sid}.json'; scope=read(path)
        if 'superseded' in scope: raise ValueError('This preparation was superseded; use its actual successor')
        if 'result' in scope: raise ValueError('Retain the completed occurrence; use native recovery for uncertain transport')
        if 'supersession_request' in scope:
            successor = scope['supersession_request']['successor']
            raise ValueError(f'{sid}: inspect the outstanding handoff to {successor} before receiving another return')
        goal=self.goal()
        if goal['sha256']!=scope['parent_goal']['sha256']:
            raise ValueError('Parent commission/duties changed; retain the proposal and prepare a fresh receiving occasion')
        self.current(self.config['commission'],'Current commission')
        for source in scope['sources']:
            self.current(source,'Selected current source '+source['id'])
        proposals=spec['proposals']; expected={Path(t).name:t for t in scope['targets']}
        if set(proposals)!=set(expected): raise ValueError('Receive exactly the selected output names')
        account=nonempty(spec['account'],'receiving account')
        producer=nonempty(spec['producer'],'producer')
        args=['receive',sid,'--reply',spec['reply'],'--producer',producer]
        for name,source in proposals.items(): args+=['--proposal',expected[name]+'='+str(Path(source).resolve())]
        if spec.get('assessment'): args+=['--assessment',spec['assessment']]
        result=self.call(*args)
        scope['transport']={'at_utc':now(),'return':result['return'],'account':account}
        write(path,scope)
        self.event('native-return-retained',{'id':sid,'scope':self.pin(path)})
        publications=[]
        published_outputs=[]
        for idx,target in enumerate(scope['targets']):
            try:
                publication=self.call('adopt',sid,'--target',target,'--reason',account)['publication']
            except Exception as exc:
                if publications:
                    self._record_partial_publication(
                        path, scope, producer=producer, account=account, returned=result['return'],
                        publications=publications, published_outputs=published_outputs,
                        pending_targets=scope['targets'][idx:],
                        state='partial-publication-needs-reconciliation',
                        failed_target=target, error=str(exc).strip())
                raise
            publications.append(publication)
            published_outputs.append(self.pin(self.work/target))
            if idx + 1 < len(scope['targets']):
                self._record_partial_publication(
                    path, scope, producer=producer, account=account, returned=result['return'],
                    publications=publications, published_outputs=published_outputs,
                    pending_targets=scope['targets'][idx + 1:], state='publishing')
        scope.pop('partial_publication', None)
        scope['result']={'at_utc':now(),'producer':producer,'return':result['return'],
                         'publications':publications,'outputs':published_outputs,
                         'account':account}
        write(path,scope)
        self.event('received',{'id':sid,'return_sha256':result['return']['return_sha256'],
                              'scope':self.pin(path),'parent_duties_closed':False})
        return {'id':sid,'outputs':scope['result']['outputs'],'parent_duties_closed':False}

    def resume(self,sid,spec):
        """Recover an actual retained return; never repeat receive or attempted publication."""
        sid=ident(sid); path=self.root/'scopes'/f'{sid}.json'; scope=read(path)
        unknown=set(spec)-{'account','settle_partial'}
        if unknown:
            raise ValueError('resume has unsupported fields: '+', '.join(sorted(unknown)))
        account=nonempty(spec['account'],'recovery account')
        settle_partial=spec.get('settle_partial',False)
        if type(settle_partial) is not bool:
            raise ValueError('settle_partial must be a boolean')
        if 'superseded' in scope:
            return {'id':sid,'state':'superseded-without-output','successor':scope['superseded']['successor']}
        if 'result' in scope:
            self.received_outputs(scope)
            out={'id':sid,'already_received':True,'outputs':scope['result']['outputs']}
            if scope['result'].get('partial'):
                out['partial']=True
                out['unpublished_targets']=scope['result'].get('unpublished_targets',[])
            return out
        reconciled_captures=self._reconcile_pending_captures(sid)
        goal=self.goal()
        observed = self.inspect_native(sid)
        returned = observed.get('return')
        handoff = scope.get('supersession_request')
        if handoff is not None and returned is None:
            raise ValueError(f"{sid}: no return confirms the handoff to {handoff['successor']}; "
                             "inspect the original packet before retrying supersede")
        if returned is None:
            if reconciled_captures:
                return {'id':sid,'state':'open-with-reconciled-captures',
                        'reconciled_captures':reconciled_captures,'parent_duties_closed':False}
            raise ValueError('No native return exists; ordinary receive is still the appropriate operation')
        if handoff is not None:
            # A bound carrier excludes caller-import at Exchange. Its actual
            # candidate can still use ordinary guarded publication on this turn.
            if returned['transport']['kind'] != 'local-command' or not returned['proposals']:
                return self._finish_supersession(path, scope, returned)
            if returned['packet'] != sid or returned['request_sha256'] != handoff['request_sha256']:
                raise ValueError(f'{sid}: carrier return differs from the prepared handoff occurrence')
        if not returned['proposals']:
            raise ValueError(f'{sid}: no-output return has no recorded successor binding; '
                             'retain the original return and reconcile the handoff at its host')
        proposals={r['path']:r for r in returned['proposals']}
        if set(proposals)!=set(scope['targets']): raise ValueError('Observed native return differs from selected host targets')
        known={p['record']['path']:p for p in observed['publications'] if isinstance(p.get('record'),dict)}
        completed_pubs=[]
        completed_outputs=[]
        remaining_targets=[]
        for target in scope['targets']:
            if target in known:
                row=known[target]
                if row.get('observation_error') or row['record'].get('phase')!='complete' or not row.get('current_target',{}).get('matches_proposal'):
                    raise ValueError(f'{sid}: publication of {target} has an unresolved outcome; '
                                     'inspect and reconcile the original native publication before continuing')
                completed_pubs.append(row['record'])
                completed_outputs.append(self.pin(self.work/target))
            else:
                remaining_targets.append(target)
        if settle_partial:
            if not completed_pubs:
                raise ValueError(f'{sid}: no published targets to settle partially')
            if remaining_targets:
                scope.pop('partial_publication', None)
                scope['result']={'at_utc':now(),'producer':returned['producer'],'return':returned,
                                 'publications':completed_pubs,'outputs':completed_outputs,
                                 'unpublished_targets':remaining_targets,'partial':True,
                                 'account':account,'recovered_from_native':True}
                write(path,scope)
                self.event('resumed-partial-return',{'id':sid,'scope':self.pin(path),
                                                     'published_targets':[p['path'] for p in completed_pubs],
                                                     'unpublished_targets':remaining_targets,
                                                     'new_receive_performed':False,
                                                     'previous_publications_replayed':False})
                res={'id':sid,'outputs':scope['result']['outputs'],
                     'published_targets':[p['path'] for p in completed_pubs],
                     'unpublished_targets':remaining_targets,'partial':True,
                     'recovered':True,'parent_duties_closed':False}
                if reconciled_captures:
                    res['reconciled_captures']=reconciled_captures
                return res
        if remaining_targets or not settle_partial:
            try:
                if goal['sha256']!=scope['parent_goal']['sha256']:
                    raise ValueError('Parent duty changed; preserve old return and prepare a new current receiving scope')
                self.current(self.config['commission'],'Current commission')
                for source in scope['sources']: self.current(source,'Selected current source '+source['id'])
            except ValueError as exc:
                if completed_pubs and remaining_targets:
                    detail = str(exc).strip()
                    self._record_partial_publication(
                        path, scope, producer=returned['producer'], account=account, returned=returned,
                        publications=completed_pubs, published_outputs=completed_outputs,
                        pending_targets=remaining_targets,
                        state='partial-publication-needs-reconciliation', error=detail)
                    raise ValueError(
                        f'{sid}: partial publication recorded ({len(completed_pubs)} published, '
                        f'{len(remaining_targets)} pending); {detail}. '
                        'Inspect and settle with settle_partial=true or prepare a fresh receiving scope'
                    ) from exc
                raise
        pubs_by_target={p['path']:p for p in completed_pubs}
        outs_by_target={p['path']:o for p,o in zip(completed_pubs,completed_outputs)}
        for idx,target in enumerate(remaining_targets):
            # Only an unattempted target may be published, through current native guards.
            (self.work/target).parent.mkdir(parents=True,exist_ok=True)
            try:
                pub=self.call('adopt',sid,'--target',target,'--reason',account)['publication']
            except Exception as exc:
                if pubs_by_target:
                    detail = str(exc).strip()
                    ordered_pubs=[pubs_by_target[t] for t in scope['targets'] if t in pubs_by_target]
                    ordered_outs=[outs_by_target[t] for t in scope['targets'] if t in outs_by_target]
                    pending_targets=list(remaining_targets[idx:])
                    self._record_partial_publication(
                        path, scope, producer=returned['producer'], account=account, returned=returned,
                        publications=ordered_pubs, published_outputs=ordered_outs,
                        pending_targets=pending_targets,
                        state='partial-publication-needs-reconciliation',
                        failed_target=target, error=detail)
                    raise ValueError(
                        f'{sid}: partial publication recorded ({len(ordered_pubs)} published, '
                        f'{len(pending_targets)} pending) after {target} failed: {detail}; '
                        'inspect and settle with settle_partial=true or prepare a fresh receiving scope'
                    ) from exc
                raise
            pubs_by_target[target]=pub
            outs_by_target[target]=self.pin(self.work/target)
            if idx + 1 < len(remaining_targets):
                ordered_pubs=[pubs_by_target[t] for t in scope['targets'] if t in pubs_by_target]
                ordered_outs=[outs_by_target[t] for t in scope['targets'] if t in outs_by_target]
                self._record_partial_publication(
                    path, scope, producer=returned['producer'], account=account, returned=returned,
                    publications=ordered_pubs, published_outputs=ordered_outs,
                    pending_targets=remaining_targets[idx + 1:], state='publishing')
        publications=[pubs_by_target[t] for t in scope['targets']]
        outputs=[outs_by_target[t] for t in scope['targets']]
        scope.pop('partial_publication', None)
        scope['result']={'at_utc':now(),'producer':returned['producer'],'return':returned,
                         'publications':publications,'outputs':outputs,
                         'account':account,'recovered_from_native':True}
        write(path,scope)
        self.event('resumed-return',{'id':sid,'scope':self.pin(path),'new_receive_performed':False,
                                    'previous_publications_replayed':False})
        res={'id':sid,'outputs':scope['result']['outputs'],'recovered':True,'parent_duties_closed':False}
        if reconciled_captures:
            res['reconciled_captures']=reconciled_captures
        return res

    def _finish_supersession(self, path, scope, returned):
        sid = scope['id']
        request = scope['supersession_request']
        successor = request['successor']
        transport = returned['transport']
        if (returned['packet'] != sid or returned['request_sha256'] != request['request_sha256']
                or returned['producer'] != request['producer'] or returned['proposals']
                or returned['reply_sha256'] != request['reply_sha256'] or transport['kind'] != 'caller-import'
                or transport.get('source_path') != request['reply_path']):
            raise ValueError(f'{sid}: native return does not confirm the recorded handoff to {successor}; '
                             'retain both for host reconciliation')
        scope['superseded'] = {'at_utc': now(), 'successor': successor, 'account': request['account'],
                               'return': returned}
        del scope['supersession_request']
        write(path, scope)
        self.event('preparation-superseded', {'id': sid, 'successor': successor, 'scope': self.pin(path),
                                              'parent_duties_closed': False})
        return {'id': sid, 'state': 'superseded-without-output', 'successor': successor,
                'return_sha256': returned['return_sha256'], 'parent_duties_closed': False}

    def supersede(self,sid,spec):
        """Return an obsolete preparation without a candidate; carry work to a real successor."""
        sid=ident(sid); path=self.root/'scopes'/f'{sid}.json'; scope=read(path)
        if 'result' in scope or 'superseded' in scope:
            raise ValueError('The occurrence already has a host outcome; preserve it')
        successor=read(self.root/'scopes'/f'{ident(spec["successor"])}.json')
        if successor['id']==sid or successor['prepared_at_utc']<=scope['prepared_at_utc']:
            raise ValueError('Select a distinct later actual preparation')
        if not set(scope['spec']['duty_ids']).issubset(successor['spec']['duty_ids']):
            raise ValueError('The actual successor must retain every selected parent duty')
        observed=self.inspect_native(sid)
        if observed.get('return') is not None or observed.get('transport_outcome') is not None:
            raise ValueError('Native outcome already exists; inspect/reconcile it instead of receiving twice')
        account=nonempty(spec['account'],'supersession account')
        producer=nonempty(spec['producer'],'actual responding participant')
        reply=self.root/'requests'/f'{sid}-supersession.md'
        body = (account + '\n\nNo candidate output is returned by this occurrence. Actual continuing scope: '
                + successor['id'] + '. Parent duties remain owed.\n').encode('utf-8')
        # Bind the host decision before Exchange can accept its no-output reply.
        # The ordinary candidate path still depends on a complete native proposal roster.
        request_sha = scope['native_context'].get('request_sha256')
        if request_sha is None:
            request_sha = self.call('recover', sid)['request_sha256']
        request = {'successor': successor['id'], 'account': account, 'producer': producer,
                   'request_sha256': request_sha, 'reply_path': str(reply), 'reply_sha256': digest(body)}
        if 'supersession_request' in scope and scope['supersession_request'] != request:
            raise ValueError(f"{sid}: the outstanding handoff to {scope['supersession_request']['successor']} differs; "
                             "inspect the original request")
        atomic_bytes(reply, body)
        scope['supersession_request'] = request
        write(path, scope)
        returned=self.call('receive',sid,'--reply',reply,'--producer',producer)['return']
        return self._finish_supersession(path, scope, returned)

    def status(self):
        native=self.call('status'); transport={r['packet']:r for r in native['turns']}
        scopes=[]
        for scope in self.scopes():
            current=transport.get(scope['id'],{})
            if 'result' in scope:
                label='partially-received' if scope['result'].get('partial') else 'received'
            elif 'superseded' in scope:
                label='superseded-without-output'
            elif 'partial_publication' in scope:
                label='partial-publication-needs-reconciliation'
            elif current.get('transport')=='returned':
                label='native-return-needs-host-reconciliation'
            else:
                label='prepared'
            row={'id':scope['id'],'host_state':label,'native':current}
            if 'result' in scope and scope['result'].get('partial'):
                row['published_targets']=[p['path'] for p in scope['result']['publications']]
                row['unpublished_targets']=scope['result'].get('unpublished_targets',[])
            if 'partial_publication' in scope and 'result' not in scope:
                partial=scope['partial_publication']
                row['published_targets']=[p['path'] for p in partial['publications']]
                row['pending_targets']=partial['pending_targets']
                if 'failed_target' in partial:
                    row['failed_target']=partial['failed_target']
                if 'error' in partial:
                    row['error']=partial['error']
            if 'supersession_request' in scope and 'result' not in scope:
                row.update(host_state='supersession-needs-reconciliation',
                           successor=scope['supersession_request']['successor'])
            pending_caps=[p.name.removeprefix('.pending-').removesuffix('.json')
                          for p in self._pending_capture_paths(scope['id'])]
            if pending_caps:
                row['pending_captures']=pending_caps
            scopes.append(row)
        return {'native':native,'scopes':scopes,
                'repairs':[read(x) for x in sorted((self.root/'repairs').glob('*.json'))]}

    def duty(self,spec):
        self.current(self.config['commission'],'Current commission')
        goal=self.goal(); rows=goal['content']['obligations']; selected=next((r for r in rows if r['id']==spec['id']),None)
        if selected is None: raise ValueError('Unknown parent duty')
        if spec['disposition'] not in ('fulfilled','open'): raise ValueError('This route cannot release a user duty')
        evidence=spec.get('evidence',[])
        if spec['disposition']=='fulfilled':
            if not evidence: raise ValueError('Fulfillment requires actual received evidence')
            published={p['path'] for s in self.scopes() if 'result' in s for p in s['result']['publications']}
            if set(evidence)-published: raise ValueError('Use actual current returned outputs as evidence')
            for scope in self.scopes():
                if 'result' in scope and {p['path'] for p in scope['result']['publications']} & set(evidence):
                    self.received_outputs(scope)
            selected.update(disposition='fulfilled',account=nonempty(spec['account'],'duty account'),evidence=evidence)
        else: selected.update(disposition='open',account=nonempty(spec['account'],'reopened duty account'))
        record={'id':'goal','kind':'goal','content':goal['content'],'basis':['sources/host/COMMISSION.md']}
        result=self.call('put','--record',self.temp('duty-'+ident(spec['id'])+'-'+str(goal['revision'])+'.json',record),'--expected',goal['revision'])
        self.event('duty-accounted',{'id':spec['id'],'disposition':spec['disposition'],'evidence':evidence})
        return result

    def finish(self,spec):
        self.current(self.config['commission'],'Current commission')
        goal=self.goal(); owed=[r['id'] for r in goal['content']['obligations'] if r['disposition'] not in ('fulfilled','released')]
        if owed: raise ValueError('Containing commission remains owed: '+', '.join(owed))
        account=self.root/'requests/whole-goal-account.md'; atomic_bytes(account,nonempty(spec['account'],'whole goal account').encode('utf-8'))
        args=['interpret','--goal-revision',goal['revision'],'--disposition','met','--account',account]
        for p in spec.get('evidence',[]): args+=['--evidence',p]
        return self.call(*args)

def main():
    p=argparse.ArgumentParser(description=__doc__, allow_abbrev=False); p.add_argument('--root',required=True,type=Path)
    sub=p.add_subparsers(dest='action',required=True)
    for name in ('init','commission','upgrade','prepare','repair-open','repair-accept','duty','finish','bind-continuation'):
        sub.add_parser(name, allow_abbrev=False).add_argument('--spec',required=True,type=Path)
    receiver=sub.add_parser('receive', allow_abbrev=False); receiver.add_argument('id'); receiver.add_argument('--spec',required=True,type=Path)
    capture=sub.add_parser('capture', allow_abbrev=False); capture.add_argument('id'); capture.add_argument('--spec',required=True,type=Path)
    resume=sub.add_parser('resume', allow_abbrev=False); resume.add_argument('id'); resume.add_argument('--spec',required=True,type=Path)
    supersede=sub.add_parser('supersede', allow_abbrev=False); supersede.add_argument('id'); supersede.add_argument('--spec',required=True,type=Path)
    sub.add_parser('status', allow_abbrev=False)
    args=p.parse_args(); root=args.root.resolve(); root.parent.mkdir(parents=True,exist_ok=True)
    # One host operation owns resource refresh plus native capture/admission.
    # This is custody serialization, not a permanent semantic integrator.
    with (root.parent/(root.name+'.host-lock')).open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        host=Host(root)
        if args.action!='init' and host.config is None: raise ValueError('Initialize the explicit continuing commission first')
        if args.action=='status':
            result=host.status()
        elif args.action in ('capture','receive','resume','supersede'): result=getattr(host,args.action)(args.id,read(args.spec))
        else: result=getattr(host,args.action.replace('-','_'))(read(args.spec))
    print(json.dumps(result,indent=2,ensure_ascii=False,allow_nan=False))

if __name__=='__main__':
    try: main()
    except (ValueError,KeyError,FileNotFoundError) as exc:
        print(str(exc),file=sys.stderr); raise SystemExit(2)
