"""Refine a finite deterministic command/return model using resettable history experiments."""
import argparse
import json
from pathlib import Path

def infer(spec):
    alphabet=spec['commands']
    if (not isinstance(alphabet,list) or not 1<=len(alphabet)<=6 or
            not all(isinstance(a,str) and a for a in alphabet) or len(set(alphabet))!=len(alphabet)):
        raise ValueError('expected one to six distinct command names')
    alphabet=tuple(sorted(alphabet))
    supplied=spec['traces']
    if not isinstance(supplied,list) or len(supplied)>4096:
        raise ValueError('expected up to 4096 reset traces')
    memory={():()}
    for run in supplied:
        calls,returns=run['calls'],run['returns']
        if (not isinstance(calls,list) or not isinstance(returns,list) or len(calls)!=len(returns)
                or len(calls)>24 or any(a not in alphabet for a in calls)
                or not all(isinstance(v,str) and v for v in returns)):
            raise ValueError('invalid command/return trace')
        word=tuple(calls);out=tuple(returns)
        for i in range(1,len(word)+1):
            key=word[:i];prior=memory.setdefault(key,out[:i])
            if prior!=out[:i]:
                return {'status':'inconsistent-replay','calls':list(key),'returns':[list(prior),list(out[:i])]}
    order=lambda w:(len(w),w)
    access={()};tests={(a,) for a in alphabet}
    while True:
        table=access|{s+(a,) for s in access for a in alphabet}
        if len(table)>256 or len(tests)>32:return {'status':'resource-limit'}
        needed=sorted({s+t for s in table for t in tests if s+t not in memory},key=order)
        if needed:
            room=4096-len(supplied)
            if len(needed[-1])>24 or room==0:return {'status':'resource-limit'}
            # A longer experiment also observes every prefix. Request only maximal missing words.
            covered={w[:i] for w in needed for i in range(1,len(w))}
            return {'status':'need-observations','calls':[list(w) for w in needed if w not in covered][:min(64,room)]}
        suffixes=sorted(tests,key=order)
        sig={s:tuple(memory[s+t][len(s):] for t in suffixes) for s in table}
        reps={}
        for s in sorted(access,key=order):reps.setdefault(sig[s],s)
        unrepresented=next((s for s in sorted(table,key=order) if sig[s] not in reps),None)
        if unrepresented is not None:
            access.add(unrepresented);continue
        split=None
        for s in sorted(access,key=order):
            r=reps[sig[s]]
            for a in alphabet:
                x,y=sig[s+(a,)],sig[r+(a,)]
                if x!=y:
                    i=next(i for i in range(len(x)) if x[i]!=y[i])
                    split=(a,)+suffixes[i];break
            if split:break
        if split:
            tests.add(split);continue
        representatives=list(reps.values());ids={sig[s]:i for i,s in enumerate(representatives)}
        states=[{'access':list(s),'on':{a:[memory[s+(a,)][-1],ids[sig[s+(a,)]]] for a in alphabet}}
                for s in representatives]
        counterexample=None
        for w in sorted(memory,key=order):
            state=0
            for i,a in enumerate(w):
                result,state=states[state]['on'][a]
                if result!=memory[w][i]:
                    counterexample=w[:i+1];break
            if counterexample:break
        if counterexample:
            access.update(counterexample[:i] for i in range(1,len(counterexample)+1));continue
        return {'status':'consistent-model','initial':0,'states':states,'distinguishing_tests':[list(w) for w in suffixes],
                'observed_prefixes':len(memory)-1}

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('experiment',type=Path,help='JSON command alphabet and ordered reset traces')
    args=parser.parse_args()
    try:result=infer(json.loads(args.experiment.read_bytes()))
    except (KeyError,TypeError,OSError,ValueError) as exc:parser.exit(2,str(exc)+'\n')
    print(json.dumps(result,indent=2,sort_keys=True))
