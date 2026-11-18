"""Finite typed peer maps and constraints under declared relational coverage."""
from collections import Counter
from functools import lru_cache
from local_policy import name


def require(ok, message):
    if not ok:
        raise ValueError(message)


def named_map(d, limit, label, *, empty=True):
    require(isinstance(d,dict) and (empty or len(d)>0) and len(d)<=limit and all(name(k) for k in d),label)
    return d


def integer(n,lo,hi):
    require(type(n) is int and lo<=n<=hi,'integer outside supported range')
    return n


def bits(mask):
    while mask:
        bit=mask & -mask
        yield bit.bit_length()-1
        mask^=bit


class Limit(Exception):
    pass


class Space:
    def __init__(self,spec):
        rules=named_map(spec['relations'],32,'invalid relation declarations')
        require(all(x in ('closed','positive') for x in rules.values()),'a relation needs explicit coverage')
        self.closed=sum(1<<i for i,k in enumerate(sorted(rules)) if rules[k]=='closed')
        self.bijective=spec.get('shape','bijection')=='bijection'
        require(spec.get('shape','bijection') in ('bijection','embedding'),'unknown map shape')
        def parse(g):
            types=named_map(g['nodes'],192,'invalid nodes',empty=False)
            require(all(name(t) for t in types.values()),'missing node type')
            ids=sorted(types);rank={k:i for i,k in enumerate(ids)}
            rel=g['relations']
            require(isinstance(rel,dict) and rel.keys()==rules.keys(),'missing relation declaration')
            matrix=[[0]*len(ids) for _ in ids];arcs=[[] for _ in ids]
            for n,k in enumerate(sorted(rules)):
                require(isinstance(rel[k],list) and len(rel[k])<=20000,'invalid edge list')
                for e in rel[k]:
                    require(isinstance(e,list) and len(e)==2 and all(v in rank for v in e),'invalid edge')
                    i,j=map(rank.get,e);b=1<<n
                    require(not(matrix[i][j]&b),'duplicate edge')
                    matrix[i][j]|=b
                    arcs[i].append((n,0,j));arcs[j].append((n,1,i))
            return ids,rank,[types[k] for k in ids],matrix,arcs
        self.L,self.li,lt,self.la,le=parse(spec['left'])
        self.R,self.ri,rt,self.ra,re=parse(spec['right'])
        nl,nr=len(self.L),len(self.R)
        self.steps=0;self.limit=integer(spec.get('work_limit',100000),1,1000000)
        self.facts=named_map(spec.get('anchors',{}),192,'invalid anchors')
        require(all(k in self.li and v in self.ri for k,v in self.facts.items()),'unknown anchor endpoint')
        self.readings=named_map(spec.get('readings',{}),128,'invalid receiving readings')
        require('@id' not in self.readings,'reserved reading name')
        for label,row in self.readings.items():
            named_map(row,192,'invalid receiver field')
            require(row.keys()<=self.ri.keys() and all(name(v) for v in row.values()),'invalid field values')
        self._fields={'@id':self.R}
        self._fields.update({k:[row.get(r) for r in self.R] for k,row in self.readings.items()})
        def field_allowed(field,i):
            return field in self._fields and all(self._fields[field][r] is not None for r in range(nr) if rt[r]==lt[i])
        self.uses={}
        for k,row in named_map(spec['uses'],128,'invalid receiving uses',empty=False).items():
            items=row['left'];field=row.get('read','@id');form=row.get('form','ordered')
            require(isinstance(items,list) and 1<=len(items)<=256 and all(v in self.li for v in items)
                    and form in ('ordered','bag'),'invalid use items or form')
            idx=[self.li[v] for v in items]
            require(all(field_allowed(field,i) for i in idx),'missing observable value for a receiving use')
            self.uses[k]=(idx,field,form)
        self.probes={}
        for q,row in named_map(spec.get('probes',{}),192,'invalid offered probes').items():
            i=self.li[row['left']];f=row.get('read','@id')
            require(field_allowed(f,i),'probe missing a possible response')
            self.probes[q]=(i,f,integer(row['cost'],1,1000000))
        self.known=named_map(spec.get('known',{}),192,'invalid observations')
        require(self.known.keys()<=self.probes.keys() and all(name(v) for v in self.known.values()),'unknown probe')
        self.refinement=0
        # Equal colors are only a necessary condition for a closed bijection.
        colors={k:i for i,k in enumerate(sorted(set(lt+rt)))}
        cl=[colors[t] for t in lt];cr=[colors[t] for t in rt]
        full_closed=self.bijective and all(v=='closed' for v in rules.values()) and nl==nr
        if full_closed:
            while True:
                def signatures(c,arcs):
                    return [(c[i],tuple(sorted((k,d,c[j]) for k,d,j in v))) for i,v in enumerate(arcs)]
                sl=signatures(cl,le);sr=signatures(cr,re)
                book={v:i for i,v in enumerate(sorted(set(sl+sr)))}
                a=[book[v] for v in sl];b=[book[v] for v in sr]
                self.refinement+=1
                before=len(set(cl+cr));cl,cr=a,b
                if len(book)==before or Counter(cl)!=Counter(cr):break
        self.base=tuple(sum(1<<j for j in range(nr) if cl[i]==cr[j] and self.matches(self.la[i][i],self.ra[j][j]))
                        for i in range(nl))
        if self.bijective and Counter(lt)!=Counter(rt):self.base=(0,)*nl
        for k,v in self.facts.items():
            i=self.li[k];d=list(self.base);d[i]&=1<<self.ri[v];self.base=tuple(d)
        self.full=(1<<nr)-1
        self.compat=lru_cache(maxsize=20000)(self._compat)

    def tick(self):
        if self.steps>=self.limit:raise Limit()
        self.steps+=1

    def matches(self,left,right):
        return left&right==left and (left^right)&self.closed==0

    def _compat(self,forward,back,r):
        return sum(1<<s for s in range(len(self.R)) if s!=r and self.matches(forward,self.ra[r][s])
                   and self.matches(back,self.ra[s][r]))

    def domains(self,facts):
        ds=list(self.base)
        for q,value in facts.items():
            i,f,_=self.probes[q]
            ds[i]&=sum(1<<r for r in bits(ds[i]) if self._fields[f][r]==value)
        return tuple(ds)

    def propagate(self,ds):
        ds=list(ds);applied=set()
        while True:
            if not all(ds):return None
            fixed={i:d.bit_length()-1 for i,d in enumerate(ds) if d.bit_count()==1}
            if len(set(fixed.values()))!=len(fixed):return None
            changed=False
            for i,r in fixed.items():
                if i in applied:continue
                applied.add(i)
                for k,d in enumerate(ds):
                    if i==k:continue
                    n=d & self.compat(self.la[i][k],self.la[k][i],r)
                    if not n:return None
                    if n!=d:ds[k]=n;changed=True
            if not changed:
                if self.bijective:
                    once=twice=0
                    for d in ds:twice|=once&d;once|=d
                    if once!=self.full:return None
                    singles=once & ~twice
                    for i,d in enumerate(ds):
                        must=d&singles
                        if must and must!=d:
                            if must.bit_count()!=1:return None
                            ds[i]=must;changed=True
                        elif must.bit_count()>1:return None
                if not changed:return tuple(ds)

    def effects(self,image):
        def one(items,field,form):
            seq=[self._fields[field][image[i]] for i in items]
            return seq if form=='ordered' else sorted(seq)
        return {k:one(*parts) for k,parts in self.uses.items()}

    def certified_effect(self,ds,reference):
        # A saturated cell contributes a fixed multiset when every member is requested equally often.
        for key,(items,field,form) in self.uses.items():
            values=self._fields[field]
            if form=='ordered':
                for i,wanted in zip(items,reference[key]):
                    if any(values[r]!=wanted for r in bits(ds[i])):return False
            else:
                mult=Counter(items);cells={}
                for i,d in enumerate(ds):cells.setdefault(d,[]).append(mult[i])
                result=[]
                for d,weights in cells.items():
                    if not any(weights):continue
                    labels=[values[r] for r in bits(d)]
                    if len(set(labels))==1:result.extend(labels[:1]*sum(weights))
                    elif len(weights)==d.bit_count() and len(set(weights))==1:result.extend(labels*weights[0])
                    else:return False
                if sorted(result)!=reference[key]:return False
        return True

    def valid(self,image,ds):
        if len(image)!=len(ds) or len(set(image))!=len(ds):return False
        if not all(ds[i]&(1<<r) for i,r in enumerate(image)):return False
        return all(self.matches(self.la[i][j],self.ra[r][s]) for i,r in enumerate(image) for j,s in enumerate(image))

    def witness(self,ds,other=None):
        focus={i for nodes,_,_ in self.uses.values() for i in nodes}
        def visit(ds):
            self.tick()
            ds=self.propagate(ds)
            if ds is None or other is not None and self.certified_effect(ds,other):return None
            # A cheap full suggestion is accepted only after checking every named relation.
            used=0;guess=[0]*len(ds)
            for i in sorted(range(len(ds)),key=lambda k:ds[k].bit_count()):
                choices=ds[i]&~used
                if not choices:break
                bit=choices & -choices;guess[i]=bit.bit_length()-1;used|=bit
            else:
                if self.valid(guess,ds) and (other is None or self.effects(guess)!=other):return tuple(guess)
            open_vars=[i for i,d in enumerate(ds) if d.bit_count()>1]
            if not open_vars:return None
            i=min(open_vars,key=lambda k:(k not in focus if other is not None else False,ds[k].bit_count(),k))
            for r in bits(ds[i]):
                child=list(ds);child[i]=1<<r
                trial=visit(child)
                if trial is not None:return trial
            return None
        return visit(ds)

    def answer(self,image,q):
        i,f,_=self.probes[q];return self._fields[f][image[i]]

    def display(self,image):
        return {k:self.R[r] for k,r in zip(self.L,image)}
