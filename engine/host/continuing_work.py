#!/usr/bin/env python3
"""Optional G60 host bridge: continuing commission, useful reminders and causal repair custody.

No model client, semantic scheduler, universal ontology or inferred completeness.
Uses the existing native runtime for context, Attention, receive/adopt and goal use.
"""
from __future__ import annotations
import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()

def digest(raw):
    return hashlib.sha256(raw).hexdigest()

def read(path):
    return json.loads(Path(path).read_text())

def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name+'.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n')
    tmp.replace(path)

def ident(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,100}', value):
        raise ValueError('Use a simple local identity: '+str(value))
    return value

def nonempty(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(label+' must be nonempty text')
    return value

def capture_error(raw):
    """Preserve known selected-capture errors, not arbitrary owner history."""
    try: detail=json.loads(raw).get('error')
    except (ValueError,AttributeError): detail=None
    safe={'disposition must be considered or deferred',
          'each assessment must name a distinct actually delivered item',
          'receipt identity already used','receipt must contain id and assessments',
          'invalid assessment fields','nonempty bounded assessments required',
          'Native return already exists; use its retained body or a new current occurrence',
          'Selected native engine does not match the reviewed capture adapter; requalify this adapter before capture'}
    if isinstance(detail,str) and detail in safe: return detail
    return ('Candidate capture could not be confirmed; raw failure details remain in owner evidence. '
            'Do not blindly repeat the receipt.')

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

class Host:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.config_path = self.root/'host.json'
        self.config = read(self.config_path) if self.config_path.exists() else None
        self.native = (self.root/(self.config or {}).get('native_root','native')).resolve()
        self.work = self.native/'work'

    @property
    def engine(self):
        return str((self.root/self.config['engine']).resolve())

    def event(self, action, value):
        row = {'at_utc':now(), 'action':action, **value}
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root/'EVENTS.jsonl').open('a') as f:
            f.write(json.dumps(row, ensure_ascii=False)+'\n')
        return row

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
        engine=Path(self.engine).parent/'work.py'
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

    def temp(self, name, value):
        p=self.root/'requests'/name
        write(p,value)
        return p

    def pin(self, path):
        p=Path(path).resolve(); raw=p.read_bytes(); sha=digest(raw)
        obj=self.root/'objects'/sha
        if not obj.exists():
            obj.parent.mkdir(parents=True,exist_ok=True); obj.write_bytes(raw)
        return {'path':str(p),'sha256':sha,'bytes':len(raw),'object':str(obj.relative_to(self.root))}

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
        command=[sys.executable,'-B',str(Path(self.engine).parent/'attend.py'),
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
        pending=target.with_name('COMMISSION.pending'); pending.write_bytes((self.root/new['object']).read_bytes()); pending.replace(target)
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
        def programs(folder):
            return {p.relative_to(folder).as_posix():digest(p.read_bytes())
                    for p in folder.rglob('*.py') if '__pycache__' not in p.parts}
        if programs(old.parent)!=programs(new.parent):
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
            'host_producer':self.pin(Path(__file__).resolve()),'host_method':self.pin(HERE/'OPERATION.md'),
            'account':account,'native_programs_unchanged':True,'prior_captures_unchanged':True})
        return {'engine':str(new),'host_producer':event['host_producer'],'host_method':event['host_method'],
                'native_programs_unchanged':True,'parent_duties_unchanged':True}

    def resource(self, rid, source):
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
            pending=target.with_name(target.name+'.pending')
            pending.write_bytes((self.root/pin['object']).read_bytes()); pending.replace(target)
        row={'id':rid, **pin,'work_path':path}
        write(old_path,row)
        return row, previous if previous and previous['sha256']!=row['sha256'] else None

    def scopes(self):
        return [read(p) for p in sorted((self.root/'scopes').glob('*.json'))]

    def prepare(self, spec):
        sid=ident(spec['id']); saved=self.root/'scopes'/f'{sid}.json'
        if saved.exists(): raise ValueError('A scope is one immutable occurrence; select a fresh identity')
        task=nonempty(spec['task'],'task'); prediction=nonempty(spec['prediction'],'prediction')
        # Resolve the question input contract before any host/native effects.
        # Omission deliberately leaves a question unbound; an unknown spelling
        # or unresolved identity must never be converted into that omission.
        selected_sources=spec.get('sources',[])
        if not isinstance(selected_sources,list) or any(not isinstance(r,dict) for r in selected_sources):
            raise ValueError('sources must be a list of selected resource objects')
        selected_ids=[ident(r['id']) for r in selected_sources]
        if len(set(selected_ids))!=len(selected_ids): raise ValueError('Duplicate selected resource')
        supplied_questions=spec.get('questions',[])
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
        self.current(self.config['commission'],'Current commission')
        goal=self.goal(); duty_ids=spec['duty_ids']
        known={r['id']:r for r in goal['content']['obligations']}
        if not duty_ids or set(duty_ids)-known.keys(): raise ValueError('Bind actual current parent duties')
        sources=[]; changed=[]
        for item in selected_sources:
            row,old=self.resource(item['id'],item['path'])
            row=dict(row,role=item.get('role','source'))
            sources.append(row)
            if old: changed.append({'resource':row['id'],'before':old,'current':row})
        reads=list(dict.fromkeys([*spec.get('reads',[]),*(r['id'] for r in sources)]))
        writes=list(dict.fromkeys(spec.get('writes',[])))
        overlaps=[]
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
        overlap_limit=spec.get('overlap_limit',8)
        if type(overlap_limit) is not int or not 1<=overlap_limit<=200:
            raise ValueError('overlap_limit must be an integer from1through200')
        affected={name for change in changed for name in change['affected_prior_consumers']}
        # Keep current selected output use ahead of affected-history nominees.
        # Stable sorting preserves the existing order within each category.
        all_overlaps.sort(key=lambda r:(bool(r['received_output_uses']),r['scope'] in affected),reverse=True)
        overlaps=all_overlaps[:overlap_limit]
        index=self.root/'overlap-index'/f'{sid}.json'
        write(index,{'scope':sid,'previous_scope_count':len(previous_scopes),'rows':all_overlaps,
                     'meaning':'Author-declared footprints plus exact same-source/received-output identity. Shared reading need not conflict. Disjoint identities do not establish semantic independence.'})
        overlap_pin=self.pin(index)
        outnames=[ident(n) for n in spec['outputs']]
        if not outnames or len(set(outnames))!=len(outnames): raise ValueError('Select distinct new output names')
        targets=['outputs/'+sid+'/'+n for n in outnames]
        if any((self.work/p).exists() for p in targets): raise ValueError('Fresh generation targets must be absent')
        # Native publication deliberately requires an existing parent at capture.
        for target in targets: (self.work/target).parent.mkdir(parents=True,exist_ok=True)
        producer=[r for r in sources if r['role']=='producer']
        repairs=[]
        for rid in spec.get('repairs',[]):
            repair=read(self.root/'repairs'/f'{ident(rid)}.json')
            old={r['id']:r for r in repair['producer_before']}
            changed_means=[r for r in producer if r['id'] in old and r['sha256']!=old[r['id']]['sha256']]
            if not changed_means: raise ValueError('Repair generation requires a selected changed producing means, not only a changed output')
            original_inputs={r['sha256'] for r in repair['inputs']}
            selected_inputs={r['sha256'] for r in sources}
            if not original_inputs.issubset(selected_inputs):
                raise ValueError('Original-case repair generation must select the retained original inputs; run transfers as separate scopes')
            repairs.append({'id':rid,'changed_producers':changed_means,
                            'original_inputs':[r['sha256'] for r in repair['inputs']]})
        source_note={'scope':sid,'prediction':prediction,'duty_ids':duty_ids,
            'changed_sources':changed,'overlap_nominations':overlaps,
            'overlap_coverage':{'previous_scopes':len(previous_scopes),'matching_scopes':len(all_overlaps),
                'delivered':len(overlaps),'omitted':len(all_overlaps)-len(overlaps),'limit':overlap_limit,
                'omitted_scope_sample':[r['scope']for r in all_overlaps[overlap_limit:overlap_limit+20]],
                'complete_index':overlap_pin,
                'meaning':'Nominations, not inferred conflicts, automatic scheduling or semantic completeness. Acquire the complete index when omitted or new relationships matter.'},
            'authored_relations':spec.get('relations',[]),
            'selected_treatments':spec.get('treatments',[]),
            'unknowns':spec.get('unknowns',['Unnominated semantic dependencies may remain.']),
            'repairs':repairs,
            'receiving_question':spec.get('receiving_question','Did the actual contribution produce the predicted useful effect under current sources?')}
        note='sources/host/'+sid+'-prospect.json'; write(self.work/note,source_note)
        items=[]
        for i,q in enumerate(questions):
            item={'id':ident(sid+'-q'+str(i+1)), 'question':q['question'],'use':q['use'],
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
        if not method_file.exists(): method_file.write_bytes(method_raw)
        request['sources']=['sources/host/COMMISSION.md',method_path,note,*[r['work_path']for r in sources]]
        for key in ('techniques','discovery','compositions'):
            if key in spec: request[key]=spec[key]
        result=self.call('context',sid,'--request',self.temp(sid+'-request.json',request))
        recover=self.call('recover',sid)
        context_file=self.root/'contexts'/f'{sid}.md'; context_file.parent.mkdir(parents=True,exist_ok=True)
        context_file.write_text(recover['context'])
        scope={'format':'g60-continuing-work-scope/1','id':sid,'prepared_at_utc':now(),
               'host_producer':self.pin(Path(__file__).resolve()),
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

    def capture(self,sid,spec):
        """Retain products through native Attention without exposing global status."""
        sid=ident(sid); scope=read(self.root/'scopes'/f'{sid}.json')
        if set(spec)!={'receipt','producer'}:
            raise ValueError('Candidate capture requires receipt and producer only')
        if 'result' in scope or 'superseded' in scope:
            raise ValueError('Candidate capture uses an open producing scope, not a completed or superseded one')
        receipt=Path(spec['receipt']).resolve()
        rid=ident(read(receipt)['id'])
        target=self.root/'captures'/sid/f'{rid}.json'
        if target.exists():
            raise ValueError('Retain the existing candidate capture; use a fresh receipt for a new occurrence')
        # The adapter checks the selected native return under the same native
        # owner lock as assessment, without constructing global status.
        result=self.capture_native(sid,receipt,nonempty(spec['producer'],'producer'))
        attention=result['attention']; status=result['status']
        matching=[r for r in status.get('turns',[]) if r.get('packet')==sid]
        turn=({'id':sid,'transport':matching[0]['transport'],
               'publication_count':len(matching[0].get('publications',[]))}
              if len(matching)==1 else {'id':sid,'transport':'unavailable','publication_count':None})
        # Use an explicit outward allowlist. The selected adapter result remains
        # in owner evidence; it is never this constructor's unfiltered response.
        capture={'id':sid,'attention':{k:attention[k] for k in ('receipt','object','recorded','boundary')},
                 'turn':turn,'parent_duties_closed':False,
                 'scope':'Candidate custody and requested-turn status only; no unrelated history or parent-duty inventory. This is not a completed return, semantic verdict or access-control boundary.'}
        write(target,capture)
        self.event('candidate-captured',{'id':sid,'receipt':rid,'capture':self.pin(target)})
        return capture

    def receive(self,sid,spec):
        sid=ident(sid); path=self.root/'scopes'/f'{sid}.json'; scope=read(path)
        if 'superseded' in scope: raise ValueError('This preparation was superseded; use its actual successor')
        if 'result' in scope: raise ValueError('Retain the completed occurrence; use native recovery for uncertain transport')
        goal=self.goal()
        if goal['sha256']!=scope['parent_goal']['sha256']:
            raise ValueError('Parent commission/duties changed; retain the proposal and prepare a fresh receiving occasion')
        self.current(self.config['commission'],'Current commission')
        for source in scope['sources']:
            self.current(source,'Selected current source '+source['id'])
        proposals=spec['proposals']; expected={Path(t).name:t for t in scope['targets']}
        if set(proposals)!=set(expected): raise ValueError('Receive exactly the selected output names')
        args=['receive',sid,'--reply',spec['reply'],'--producer',nonempty(spec['producer'],'producer')]
        for name,source in proposals.items(): args+=['--proposal',expected[name]+'='+str(Path(source).resolve())]
        if spec.get('assessment'): args+=['--assessment',spec['assessment']]
        result=self.call(*args)
        scope['transport']={'at_utc':now(),'return':result['return'],'account':spec['account']}
        write(path,scope)
        self.event('native-return-retained',{'id':sid,'scope':self.pin(path)})
        publications=[]
        for target in scope['targets']:
            publications.append(self.call('adopt',sid,'--target',target,'--reason',nonempty(spec['account'],'receiving account'))['publication'])
        scope['result']={'at_utc':now(),'producer':spec['producer'],'return':result['return'],
                         'publications':publications,'outputs':[self.pin(self.work/t) for t in scope['targets']],
                         'account':spec['account']}
        write(path,scope)
        self.event('received',{'id':sid,'return_sha256':result['return']['return_sha256'],
                              'scope':self.pin(path),'parent_duties_closed':False})
        return {'id':sid,'outputs':scope['result']['outputs'],'parent_duties_closed':False}

    def resume(self,sid,spec):
        """Recover an actual retained return; never repeat receive or attempted publication."""
        sid=ident(sid); path=self.root/'scopes'/f'{sid}.json'; scope=read(path)
        account=nonempty(spec['account'],'recovery account')
        if 'superseded' in scope:
            return {'id':sid,'state':'superseded-without-output','successor':scope['superseded']['successor']}
        if 'result' in scope:
            self.received_outputs(scope)
            return {'id':sid,'already_received':True,'outputs':scope['result']['outputs']}
        goal=self.goal()
        if goal['sha256']!=scope['parent_goal']['sha256']:
            raise ValueError('Parent duty changed; preserve old return and prepare a new current receiving scope')
        self.current(self.config['commission'],'Current commission')
        for source in scope['sources']: self.current(source,'Selected current source '+source['id'])
        observed=self.inspect_native(sid); returned=observed.get('return')
        if returned is None: raise ValueError('No native return exists; ordinary receive is still the appropriate operation')
        proposals={r['path']:r for r in returned['proposals']}
        if set(proposals)!=set(scope['targets']): raise ValueError('Observed native return differs from selected host targets')
        known={p['record']['path']:p for p in observed['publications'] if isinstance(p.get('record'),dict)}
        publications=[]
        for target in scope['targets']:
            if target in known:
                row=known[target]
                if row.get('observation_error') or row['record'].get('phase')!='complete' or not row.get('current_target',{}).get('matches_proposal'):
                    raise ValueError('Publication was attempted with unresolved outcome; use native inspection/reconciliation, never replay')
                publications.append(row['record'])
            else:
                # Only an unattempted target may be published, through current native guards.
                (self.work/target).parent.mkdir(parents=True,exist_ok=True)
                publications.append(self.call('adopt',sid,'--target',target,'--reason',account)['publication'])
        scope['result']={'at_utc':now(),'producer':returned['producer'],'return':returned,
                         'publications':publications,'outputs':[self.pin(self.work/t) for t in scope['targets']],
                         'account':account,'recovered_from_native':True}
        write(path,scope)
        self.event('resumed-return',{'id':sid,'scope':self.pin(path),'new_receive_performed':False,
                                    'previous_publications_replayed':False})
        return {'id':sid,'outputs':scope['result']['outputs'],'recovered':True,'parent_duties_closed':False}

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
        reply.write_text(account+'\n\nNo candidate output is returned by this occurrence. Actual continuing scope: '+successor['id']+'. Parent duties remain owed.\n')
        returned=self.call('receive',sid,'--reply',reply,'--producer',producer)['return']
        scope['superseded']={'at_utc':now(),'successor':successor['id'],'account':account,'return':returned}
        write(path,scope)
        self.event('preparation-superseded',{'id':sid,'successor':successor['id'],'scope':self.pin(path),'parent_duties_closed':False})
        return {'id':sid,'state':'superseded-without-output','successor':successor['id'],
                'return_sha256':returned['return_sha256'],'parent_duties_closed':False}

    def status(self):
        native=self.call('status'); transport={r['packet']:r for r in native['turns']}
        scopes=[]
        for scope in self.scopes():
            current=transport.get(scope['id'],{})
            label='received' if 'result' in scope else 'superseded-without-output' if 'superseded'in scope else 'native-return-needs-host-reconciliation' if current.get('transport')=='returned' else 'prepared'
            scopes.append({'id':scope['id'],'host_state':label,'native':current})
        return {'native':native,'scopes':scopes,
                'repairs':[read(x) for x in sorted((self.root/'repairs').glob('*.json'))]}

    def repair_open(self,spec):
        rid=ident(spec['id']); path=self.root/'repairs'/f'{rid}.json'
        if path.exists(): raise ValueError('Preserve the original failure occurrence')
        row={'id':rid,'opened_at_utc':now(),'failure_account':nonempty(spec['failure_account'],'failure account'),
             'causal_hypothesis':nonempty(spec['causal_hypothesis'],'causal hypothesis'),
             'inputs':[self.pin(p) for p in spec['inputs']],
             'failures':[self.pin(p) for p in spec['failures']],
             'producer_before':[dict(self.pin(x['path']),id=ident(x['id'])) for x in spec['producer_before']]}
        if not row['inputs'] or not row['failures'] or not row['producer_before']:
            raise ValueError('A repair needs retained inputs, observed failure and responsible producing means')
        write(path,row); self.event('repair-opened',{'id':rid,'retained':self.pin(path)})
        return row

    def repair_accept(self,spec):
        rid=ident(spec['id']); repair_path=self.root/'repairs'/f'{rid}.json'; repair=read(repair_path)
        if 'acceptance' in repair: raise ValueError('Repair acceptance already recorded; new evidence requires a new occurrence')
        gen=read(self.root/'scopes'/f'{ident(spec["generation"])}.json')
        rec=read(self.root/'scopes'/f'{ident(spec["receiving"])}.json')
        if gen['id']==rec['id'] or 'result' not in gen or 'result' not in rec:
            raise ValueError('Require distinct actually returned generation and receiving scopes')
        self.current(self.config['commission'],'Current commission')
        if self.goal()['sha256']!=rec['parent_goal']['sha256']:
            raise ValueError('Parent duty changed after receiving; requalify this repair under current work')
        self.received_outputs(gen); receiving_outputs=self.received_outputs(rec)
        if gen['result']['producer']==rec['result']['producer']:
            raise ValueError('This acceptance route requires a separately attributed actual receiver')
        if rid not in [r['id'] for r in gen['repairs']]: raise ValueError('Generation did not select this changed producer')
        if rec['prepared_at_utc']<=gen['result']['at_utc']: raise ValueError('Receiver must encounter the actual returned product')
        generated={p['sha256'] for p in gen['result']['outputs']}
        seen={p['sha256'] for p in rec['sources']}
        if not generated.issubset(seen): raise ValueError('Receiver did not select all actual generated outputs')
        failed={p['sha256'] for p in repair['failures']}
        if not failed.issubset(seen): raise ValueError('Receiver must encounter retained failure outputs')
        causal_sources={p['sha256'] for p in [*repair['inputs'],*repair['producer_before']]}
        changed=next(r for r in gen['repairs'] if r['id']==rid)['changed_producers']
        causal_sources.update(p['sha256'] for p in changed)
        if not causal_sources.issubset(seen):
            raise ValueError('Receiver must encounter original inputs and actual old/new producing means')
        assessment_target='outputs/'+rec['id']+'/'+ident(spec['assessment'])
        if assessment_target not in rec['targets']: raise ValueError('Assessment must be an actual returned receiving target')
        received_pin=next((p for p in receiving_outputs if p['path']==str((self.work/assessment_target).resolve())),None)
        if received_pin is None: raise ValueError('Assessment is not one of the received output occurrences')
        assessment=read(self.root/received_pin['object'])
        if assessment.get('finding')!='fixed-within-stated-scope': raise ValueError('Receiver has not accepted this repair')
        for key in ('causal_account','comparison','transfer_or_rival'):
            nonempty(assessment.get(key),key)
        limits=assessment.get('limits')
        if isinstance(limits,list):
            if not limits: raise ValueError('limits must contain at least one substantive limit')
            for item in limits: nonempty(item,'limit')
        else:
            nonempty(limits,'limits')
        named=assessment.get('generated_sha256')
        if isinstance(named,dict):
            expected={Path(p['path']).name:p['sha256'] for p in gen['result']['outputs']}
            if named!=expected: raise ValueError('Assessment names different output names or bytes')
        elif not isinstance(named,list) or len(named)!=len(generated) or any(not isinstance(x,str) for x in named) or set(named)!=generated:
            raise ValueError('Assessment names different output bytes')
        acceptance={'at_utc':now(),'generation':gen['id'],'receiving':rec['id'],
                    'assessment':received_pin,'account':assessment,
                    'scope':'Mechanical lineage plus attributed semantic receiving; not proof of universal efficacy.'}
        repair['acceptance']=acceptance; write(repair_path,repair)
        self.event('repair-accepted',{'id':rid,'generation':gen['id'],'receiving':rec['id']})
        return acceptance

    def duty(self,spec):
        self.current(self.config['commission'],'Current commission')
        goal=self.goal(); rows=goal['content']['obligations']; selected=next((r for r in rows if r['id']==spec['id']),None)
        if selected is None: raise ValueError('Unknown parent duty')
        if spec['disposition'] not in ('fulfilled','open'): raise ValueError('This route cannot release a user duty')
        evidence=spec.get('evidence',[])
        if spec['disposition']=='fulfilled':
            if not evidence: raise ValueError('Fulfillment requires actual received evidence')
            published={t for s in self.scopes() if 'result' in s for t in s['targets']}
            if set(evidence)-published: raise ValueError('Use actual current returned outputs as evidence')
            for scope in self.scopes():
                if set(scope['targets']) & set(evidence): self.received_outputs(scope)
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
        account=self.root/'requests/whole-goal-account.md'; account.write_text(nonempty(spec['account'],'whole goal account'))
        args=['interpret','--goal-revision',goal['revision'],'--disposition','met','--account',account]
        for p in spec.get('evidence',[]): args+=['--evidence',p]
        return self.call(*args)

def main():
    p=argparse.ArgumentParser(description=__doc__); p.add_argument('--root',required=True,type=Path)
    sub=p.add_subparsers(dest='action',required=True)
    for name in ('init','commission','upgrade','prepare','repair-open','repair-accept','duty','finish','bind-continuation'):
        sub.add_parser(name).add_argument('--spec',required=True,type=Path)
    receiver=sub.add_parser('receive'); receiver.add_argument('id'); receiver.add_argument('--spec',required=True,type=Path)
    capture=sub.add_parser('capture'); capture.add_argument('id'); capture.add_argument('--spec',required=True,type=Path)
    resume=sub.add_parser('resume'); resume.add_argument('id'); resume.add_argument('--spec',required=True,type=Path)
    supersede=sub.add_parser('supersede'); supersede.add_argument('id'); supersede.add_argument('--spec',required=True,type=Path)
    sub.add_parser('status')
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
    print(json.dumps(result,indent=2,ensure_ascii=False))

if __name__=='__main__':
    try: main()
    except (ValueError,KeyError,FileNotFoundError) as exc:
        print(str(exc),file=sys.stderr); raise SystemExit(2)
