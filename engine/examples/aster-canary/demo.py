"""Exercise the canary receiver in separate processes. Supply a NEW directory to keep the desks."""
from pathlib import Path
import argparse
import json
import os
import subprocess
import sys
import tempfile

from canary import Canary, OWNER, PROFILE, SOURCE, pin
from workbench import Workbench

SCRIPT = Path(__file__).resolve()


def command(app, op, who, cid, **fields):
    result = app.collective.call(dict(op=op, actor=who, command=cid, **fields))
    assert result['status']=='accepted',result
    return result


def register(app,ident,kind,content,revision=0):
    with app.lock():
        return app.native.put(dict(id=ident,kind=kind,content=content),revision)


def start(root):
    app = Canary.initialize(root)
    register(app,SOURCE,'note',{'probe':'west latency sample'})
    command(app,'create',OWNER,'init-plan',id='rollout-plan',body={'cohort':'west','rollback':'undo-west-v1'},
            reconsider_from=['observer'])
    with app.lock():
        app.native.context('measure',{'task':'Return the west p95 in milliseconds.','records':[SOURCE]})
        reply = app.root/'reading.json'
        reply.write_text('{"cohort":"west","p95_ms":72}\n')
        app.native.receive('measure',reply,'worker-1',[])
    return app


def code(r):
    return r.get('code',r['status'])


def drill(app,plan):
    # A local receiving desk observes restoration of actual bytes in a temporary workspace.
    target = app.collective.show(plan['id'],plan['revision'])
    rollback = target['body']['rollback']
    with tempfile.TemporaryDirectory() as directory:
        base = Path(directory)
        (base/'undo-west-v1').write_bytes(b'stable-west')
        (base/'undo-west-v3').write_bytes(b'stable-west')
        live = base/'west'
        live.write_bytes(b'candidate-west')
        snapshot = base/rollback if rollback in ('undo-west-v1','undo-west-v3') else None
        if snapshot is not None:
            live.write_bytes(snapshot.read_bytes())
        restored = live.read_bytes()==b'stable-west'
    body = {'format':'canary-rollback-check/1','cohort':'west','plan':plan,'restored':restored}
    ident = 'drill-'+str(plan['revision'])
    return command(app,'create',OWNER,ident,id=ident,body=body)['artifact']


def trial(root, fault):
    app = start(root)
    crash = subprocess.run([sys.executable,'-B',SCRIPT,'--crash',fault,str(root)],capture_output=True)
    assert crash.returncode==73,(crash.returncode,crash.stderr)
    key = app.stage('measure')
    req = app.entry(key)
    prior = app.collective.receipt(req['actor'],req['command'])
    assert bool(prior)==(fault=='after-commit')
    replay = app.deliver(key)
    assert replay['status']=='accepted' and replay==app.deliver(key)
    if prior: assert prior==replay
    metric = replay['artifact']
    plan = pin(app.collective.show('rollout-plan'))
    first_drill = drill(app,plan)
    desk = Workbench(root/'private.sqlite',OWNER,app.collective)
    # The exposed span contains the first 12 bytes of the acquired JSON body.
    exposed = desk.receive('preview',metric['id'],metric['revision'],0,12)
    assert len(desk.delivered('preview'))==12 < exposed['whole_bytes']
    assert desk.receive('preview',metric['id'],1,0,12)==exposed
    try:
        desk.receive('preview',metric['id'],1,1,13)
    except ValueError:
        pass
    else:
        raise AssertionError('conflicting exposure accepted')
    desk.open('rollout-plan')
    desk.edit('rollout-plan',0,{'cohort':'west','rollback':'undo-west-v2'})
    saved = desk.submit('save-two','rollout-plan')
    desk.edit('rollout-plan',1,{'cohort':'west','rollback':'still-typing'})
    # Another authorized operation changes the accepted target before the old reply arrives.
    command(app,'capture',OWNER,'shared-c',id='shared-ctx',target='rollout-plan')
    command(app,'propose',OWNER,'shared-p',id='shared-prop',context='shared-ctx',body={'cohort':'west','rollback':'undo-west-v3'})
    shared = command(app,'publish',OWNER,'shared-w',proposal='shared-prop',account='New accepted rollback.')
    new_plan = shared['artifact']
    reopened = Workbench(root/'private.sqlite',OWNER,app.collective)
    late = reopened.reconcile('save-two')
    assert saved['artifact']['revision']==2 and late['base']==plan and late['generation']==2
    assert late['body']['rollback']=='still-typing' and late['incoming']==new_plan
    new_drill = drill(app,new_plan)
    def issue(cmd, expected=new_plan, who=OWNER, obs=new_drill):
        return app.issue(who,cmd,key,metric,expected,'Review west canary with rollback.', observation=obs)
    assert code(issue('wrong-drill',obs=first_drill))=='rollback-unqualified'
    old = issue('old-plan',plan,obs=first_drill)
    other = issue('worker-attempt',who='worker-1')
    register(app,'west-duty','obligation',{'applies_to':PROFILE,'disposition':'open','text':'Verify rollback.'})
    hold = issue('hold')
    sibling = command(app,'create','stock','stock-one',id='sibling',body={'available':6})
    assert app.decision(key) is None
    register(app,'west-duty','obligation',{'applies_to':PROFILE,'disposition':'fulfilled','account':'Verified rollback.'},1)
    register(app,'elsewhere','obligation',{'applies_to':'east','text':'East still owes a reading.'})
    goal = app.native.show('goal')['content']
    modified = dict(goal, canary={**goal['canary'],'limit_ms':60})
    register(app,'goal','goal',modified,1)
    redirected = issue('redirected')
    # Returning to these exact policy values is an explicit reinstatement in this finite profile.
    register(app,'goal','goal',{'contribution':'Other goal'},2)
    assert code(issue('withdrawn-direction'))=='direction-changed'
    register(app,'goal','goal',goal,3)
    accepted = issue('owner-accept')
    assert [code(x) for x in (old,other,hold,redirected,accepted)]==[
        'basis-changed','wrong-owner','duty-open','direction-changed','accepted']
    assert issue('owner-accept')==accepted
    try:
        app.issue(OWNER,'owner-accept',key,metric,plan,'Different decision', observation=new_drill)
    except ValueError as exc:
        assert getattr(exc,'code',None)=='command-conflict'
    else:
        raise AssertionError('Conflicting issue replay accepted')
    follow = subprocess.check_output([sys.executable,'-B',SCRIPT,'--read',key,str(root)],text=True)
    seen = json.loads(follow)
    assert seen=={'cohort':'west','p95_ms':72,'rollback':'undo-west-v3','worker':'worker-1','owner':OWNER}
    assert code(issue('second-issue'))=='already-issued'
    merged = reopened.merge('rollout-plan',2,new_plan,{'cohort':'west','rollback':'merged-and-reviewed'})
    merged_save = reopened.submit('save-merge','rollout-plan')
    assert merged['generation']==3 and merged_save['artifact']['revision']==4
    return dict(fault=fault,had_destination_receipt=bool(prior),metric_revision=metric['revision'],
                owner_checks=[code(x) for x in (old,other,hold,redirected)],sibling_count=app.collective.show(sibling['artifact']['id'])['body']['available'],
                published=seen,private_edit=late['body']['rollback'],base_revision=late['base']['revision'],
                incoming_revision=late['incoming']['revision'],generation=late['generation'],merged_saved_revision=merged_save['artifact']['revision'],delivered_bytes=exposed['stop'],
                partial_delivery_confirmed=exposed['stop']<exposed['whole_bytes'])


def stale_source(root, before):
    app = start(root)
    key = app.stage('measure')
    if not before:
        accepted = app.deliver(key)
    register(app,SOURCE,'note',{'probe':'replacement sample'},1)
    if before:
        result = app.deliver(key)
    else:
        plan = pin(app.collective.show('rollout-plan'))
        result = app.issue(OWNER,'old',key,accepted['artifact'],plan,'Use west.',observation=drill(app,plan))
    assert code(result)=='sample-changed' and app.decision(key) is None
    return code(result)


def run(parent):
    return dict(trials=[trial(parent/f,f) for f in ('after-stage','after-commit')],
                changed_before_delivery=stale_source(parent/'source-before',True),
                changed_before_issue=stale_source(parent/'source-after',False))


if __name__=='__main__':
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument('--crash',choices=['after-stage','after-commit'])
    cli.add_argument('--read',metavar='TRANSFER')
    cli.add_argument('directory',nargs='?',type=Path)
    args=cli.parse_args()
    if args.crash:
        app=Canary(args.directory)
        key=app.stage('measure')
        if args.crash=='after-stage':
            os._exit(73)
        app.deliver(key,after_commit=lambda:os._exit(73))
    elif args.read:
        d=Canary(args.directory).decision(args.read)
        print(json.dumps({k:d[k] for k in ('cohort','p95_ms','rollback','worker','owner')}))
    elif args.directory:
        args.directory.mkdir(exist_ok=False)
        print(json.dumps(run(args.directory),indent=2))
    else:
        with tempfile.TemporaryDirectory() as tmp:
            print(json.dumps(run(Path(tmp)),indent=2))
