"""Single-operator, scoped review ledger. All arguments after the command are positional."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import tempfile
import uuid

BASE = Path(__file__).resolve().parent
CAP = 4


def require(ok, message):
    if not ok:
        raise ValueError(message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read(path):
    return json.loads(path.read_text())


def save(path, data):
    with tempfile.NamedTemporaryFile('w', dir=path.parent, delete=False) as f:
        json.dump(data, f, indent=2)
        f.write('\n')
    os.replace(f.name, path)


def knowledge(regfile, routefile):
    reg, routes = read(regfile), read(routefile)
    require(reg and set(reg) == {i for v in routes.values() for i in v['ids']}, 'routing coverage mismatch')
    snippets = {}
    for i, r in reg.items():
        for name in ('unit','contrast','interpretation'):
            nonblank(r,name)
        source = regfile.parent / r['owner_file']
        require(regfile.parent.resolve() in source.resolve().parents and not source.is_symlink(), 'owner outside registry')
        data = source.read_bytes()
        require(digest(data) == r['source_sha256'], f'{i}: owner drift')
        a, b = r['lines']
        lines = data.decode().splitlines()
        require(1 <= a <= b <= len(lines), f'{i}: invalid range')
        snippet = '\n'.join(lines[a-1:b]); snippets[i] = snippet
        require(digest(snippet.encode()) == r['exact_lines_sha256'], f'{i}: range drift')
    pin = digest(json.dumps([reg, routes], sort_keys=True).encode())
    return reg, routes, snippets, pin


def nonblank(o, key):
    value = o.get(key)
    require(isinstance(value, str) and value.strip(), f'missing {key}')
    return value


def act(args):
    reg, routes, snippets, pin = knowledge(args.registry, args.routes)
    data = args.artifact.read_bytes(); artifact = data.decode(); rev = digest(data)
    ledger = args.ledger
    if args.command == 'init':
        require(not ledger.exists(), 'ledger exists')
        state = dict(basis=pin, revision=rev, items={i:'open' for i in reg}, tickets={}, history=[])
        save(ledger, state); return {'basis':pin, 'revision': rev, 'known': len(reg)}
    state = read(ledger); items, tickets = state['items'], state['tickets']
    if args.command == 'supersede':
        require(args.previous_basis == state['basis'] and pin != state['basis'],'expected a changed interpretation, owner or route and exact previous basis')
        require(args.reason.strip(),'explain supersession')
        state['history'].append(dict(basis=state['basis'], revision=state['revision'],items=dict(items),tickets=dict(tickets),reason=args.reason))
        state.update(basis=pin,revision=rev,items={i:'open' for i in reg},tickets={})
        save(ledger,state)
        return {'basis':pin,'revision':rev,'reopened':len(reg)}
    require(pin == state['basis'], 'basis changed; supersede with the old pin and reason')
    require(set(items) == set(reg), 'ledger ID mismatch')
    if args.command == 'reconcile':
        require(rev != state['revision'], 'artifact digest unchanged')
        state['history'].append({'replaced_revision':state['revision'], 'items':dict(items),'basis':pin,'tickets':dict(tickets)})
        state['revision'] = rev
        state['tickets'] = {}
        for i in items: items[i] = 'open'
        save(ledger,state); return {'revision':rev,'reopened':len(items),'next':'issue one relevant view on this revision'}
    require(rev == state['revision'], 'artifact changed; reconcile before reviewing')
    if args.command == 'status':
        return {'revision':rev, 'basis':pin,'counts':{s:list(items.values()).count(s) for s in sorted(set(items.values()))}, 'items':items}
    if args.command == 'issue':
        require(args.view in routes, 'unknown view')
        require(1 <= args.limit <= CAP, f'limit must be 1..{CAP}')
        v=routes[args.view]; selection=args.ids or v['ids']; require(set(selection)<=set(v['ids']), 'ID outside view')
        require(len(set(selection))==len(selection),'duplicate selection')
        ids=[i for i in selection if items[i]=='open'][:args.limit]
        require(ids,'no open selected IDs in view'); ticket=uuid.uuid4().hex
        tickets[ticket]={'revision':rev,'ids':ids,'view':args.view,'observations':{},'closed':False}
        for i in ids: items[i]='issued'
        save(ledger,state)
        return {'ticket':ticket,'revision':rev,'view':args.view,'phase':v['phase'],'probe':v['probe'],'items':[{'id':i,'source':reg[i]['source'],'unit':reg[i]['unit'],'contrast':reg[i]['contrast'],'interpretation':reg[i]['interpretation'],'lines':reg[i]['lines'],'owner_text':snippets[i]} for i in ids]}
    t=tickets.get(args.ticket); require(t and t['revision']==rev and not t['closed'], 'unknown, closed or stale ticket')
    if args.command=='release':
        for i in t['ids']:
            if i not in t['observations']: items[i]='open'
        t['closed']=True
    elif args.command=='receive':
        obs=read(args.observation); i=nonblank(obs,'id'); result=nonblank(obs,'result')
        require(i in t['ids'] and i not in t['observations'] and items[i]=='issued','not outstanding on this ticket')
        require(obs.get('unit') == reg[i]['unit'] and obs.get('contrast') == reg[i]['contrast'], 'sample does not name the registered unit and distinction')
        nonblank(obs,'observed_decision'); nonblank(obs,'sample_target')
        require(result in ('pass','fail','inapplicable'),'invalid result')
        quote=nonblank(obs,'artifact_quote'); owner=nonblank(obs,'owner_quote'); nonblank(obs,'reason'); nonblank(obs,'reviewer')
        start=obs.get('artifact_offset'); require(type(start) is int and start>=0 and artifact.startswith(quote,start),'artifact quote/character offset mismatch')
        require(owner in snippets[i], 'owner quote outside assigned source lines')
        if result=='inapplicable': nonblank(obs,'applicability_reason')
        t['observations'][i]=obs; items[i]=result
        if len(t['observations'])==len(t['ids']): t['closed']=True
    save(ledger,state);return {'revision':rev,'ticket':args.ticket,'closed':t['closed'],'received':list(t['observations'])}


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--ledger',type=Path,required=True);p.add_argument('--registry',type=Path,default=BASE/'registry.json');p.add_argument('--routes',type=Path,default=BASE/'routes.json')
    cmds=p.add_subparsers(dest='command',required=True)
    for name in ('init','issue','receive','release','reconcile','supersede','status'):
        sub=cmds.add_parser(name);sub.add_argument('artifact',type=Path)
        if name=='supersede':
            sub.add_argument('previous_basis');sub.add_argument('reason')
        if name=='issue':
            sub.add_argument('view');sub.add_argument('--ids',nargs='+');sub.add_argument('--limit',type=int,default=2)
        if name in ('receive','release'): sub.add_argument('ticket')
        if name=='receive': sub.add_argument('observation',type=Path)
    args=p.parse_args()
    try:
        with open(str(args.ledger)+'.lock','a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX); print(json.dumps(act(args),indent=2))
    except (ValueError,OSError,KeyError,TypeError) as err: p.exit(2,f'{err}\n')


if __name__=='__main__': main()
