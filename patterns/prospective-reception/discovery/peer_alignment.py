"""Qualify what a peer can do under every translation consistent with relational evidence."""
import argparse
from functools import lru_cache
import json
from pathlib import Path
from alignment_space import Space, Limit, bits, require
from local_policy import unique_object


def construct(spec):
    if spec.get('format')=='network':
        from alignment_network import analyze
        return analyze(spec)
    require(spec.get('format','graph') in ('graph','facts'),'unknown representation')
    if spec.get('format')=='facts':
        from alignment_facts import expand
        spec=expand(spec)
    space=Space(spec)
    mode=spec.get('mode','qualify')
    require(mode in ('qualify','panel','policy'),'unknown alignment operation')
    known=space.known.copy()
    proposed=spec.get('reference')
    if proposed is not None:
        require(isinstance(proposed,dict) and proposed.keys()==space.li.keys(),'reference must translate every node')
        proposed=tuple(space.ri[proposed[i]] for i in space.L)
        require(space.valid(proposed,space.domains(known)),'reference violates supplied evidence')
    def pack(image):
        return dict(mapping=space.display(image),effects=space.effects(image))
    def checked(ds,reference):
        return space.witness(ds,space.effects(reference))
    @lru_cache(None)
    def first(facts):
        return space.witness(space.domains(dict(facts)))
    @lru_cache(None)
    def adverse(facts):
        baseline=first(facts)
        return None if baseline is None else checked(space.domains(dict(facts)),baseline)
    def key(facts):return tuple(sorted(facts.items()))
    facts=key(known)
    def certify():
        reference=proposed if proposed is not None else first(facts)
        if reference is None:return dict(status='inconsistent')
        bad=checked(space.domains(known),reference)
        if bad is None:return dict(status='qualified',effects=space.effects(reference),example=space.display(reference))
        if mode=='qualify':
            return dict(status='ambiguous',examples=[pack(reference),pack(bad)])
        if mode=='policy':return policy()
        return panel(reference,bad)

    def panel(ref,initial_bad):
        # This objective concerns the outcome where the designated reference's answers are observed.
        offered=sorted(space.probes.keys()-known.keys())
        answers={q:space.answer(ref,q) for q in offered}
        target=space.effects(ref)
        def counter(names):
            observations=known|{q:answers[q] for q in names}
            return space.witness(space.domains(observations),target)
        clauses=[];witnesses=[]
        upper=None;incumbent=None;lower=0
        def record(bad):
            cut=tuple(q for q in offered if space.answer(bad,q)!=answers[q])
            clauses.append(cut)
            witnesses.append(dict(alternative=pack(bad),distinguished_by=list(cut)))
        def cheapest():
            minimal=[]
            for c in sorted(set(clauses),key=lambda c:(len(c),c)):
                if not any(set(v)<=set(c) for v in minimal):minimal.append(c)
            @lru_cache(None)
            def solve(groups):
                space.tick()
                if not groups:return 0,()
                forced={c[0] for c in groups if len(c)==1}
                if forced:
                    price,rest=solve(tuple(c for c in groups if not forced.intersection(c)))
                    return price+sum(space.probes[q][2] for q in forced),tuple(sorted((*rest,*forced)))
                best=None
                for q in min(groups,key=lambda x:(len(x),x)):
                    price,rest=solve(tuple(c for c in groups if q not in c))
                    candidate=(price+space.probes[q][2],tuple(sorted((*rest,q))))
                    if best is None or candidate<best:best=candidate
                return best
            return solve(tuple(minimal))
        try:
            bad=counter(offered)
            if bad is not None:return dict(status='inseparable',examples=[pack(ref),pack(bad)],
                                  shared_answers=answers)
            upper=sum(space.probes[q][2] for q in offered);incumbent=offered
            record(initial_bad)
            while lower<upper:
                lower,choice=cheapest()
                if lower==upper:break
                bad=counter(choice)
                if bad is None:
                    incumbent=list(choice);upper=lower;break
                record(bad)
        except Limit:
            return dict(status='resource-limit',lower=lower,upper=upper,probes=incumbent)
        return dict(status='certified-panel',cost=upper,probes=incumbent,
                    required_answers={q:answers[q] for q in incumbent},effects=target,
                    reference=space.display(ref),witnesses=witnesses)

    def policy():
        require(len(space.probes.keys()-known.keys())<=14,'adaptive planning supports 14 open probes')
        nodes={};infeasible=[]
        @lru_cache(None)
        def visit(f):
            space.tick()
            ref=first(f)
            if ref is None:return None
            base=space.effects(ref);bad=adverse(f)
            if bad is None:
                nodes[f]=dict(effects=base)
                return 0
            ds=space.propagate(space.domains(dict(f)));remaining=sorted(space.probes.keys()-dict(f).keys())
            if all(space.answer(ref,q)==space.answer(bad,q) for q in remaining):
                if not infeasible:infeasible.append(dict(examples=[pack(ref),pack(bad)],
                        shared_answers={q:space.answer(ref,q) for q in space.probes}))
                return None
            best=None
            for q in remaining:
                i,field,cost=space.probes[q]
                if best is not None and cost>best[0]:continue
                candidates=sorted({space._fields[field][r] for r in bits(ds[i])})
                children={}
                for answer in candidates:
                    child=key(dict(f)|{q:answer})
                    if first(child) is not None:children[answer]=child
                if len(children)<=1:continue
                peak=0
                for child in children.values():
                    v=visit(child)
                    if v is None:break
                    peak=max(peak,v)
                else:
                    if best is None or (cost+peak,q)<best[:2]:best=(cost+peak,q,children)
            if best is None:return None
            value,q,children=best;nodes[f]=dict(ask=q,next=children)
            return value
        cost=visit(facts)
        if cost is None:
            require(bool(infeasible),'missing unresolved pair')
            return dict(status='inseparable',**infeasible[0])
        exported={};labels={}
        def export(f):
            if f in labels:return labels[f]
            n=labels[f]='n'+str(len(labels));v=nodes[f]
            exported[n]=v if 'effects' in v else dict(ask=v['ask'],next={a:export(g) for a,g in v['next'].items()})
            return n
        start=export(facts)
        return dict(status='policy',worst_cost=cost,root=start,nodes=exported)
    try:
        out=certify()
    except Limit:
        out=dict(status='resource-limit')
    out.update(search_states=space.steps,refinement_rounds=space.refinement)
    return out


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('commission',type=Path)
    args=p.parse_args()
    try:result=construct(json.loads(args.commission.read_bytes(),object_pairs_hook=unique_object))
    except (ValueError,KeyError,TypeError,AttributeError,OSError,RecursionError) as err:p.exit(2,str(err)+'\n')
    print(json.dumps(result,sort_keys=True,indent=2))
