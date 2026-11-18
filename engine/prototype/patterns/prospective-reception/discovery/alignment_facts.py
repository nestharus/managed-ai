"""Construct a shared incidence vocabulary for independently named predicates and argument positions."""
from local_policy import name
from alignment_space import require, named_map


def expand(spec):
    # References to generated predicate/position nodes are available to paid probes.
    # Entity names occupy their own namespace; callers supply schema classes, not crosswalks.
    def graph(side):
        nodes={};edges={r:[] for r in ('kind','of','place','value')}
        def put(key,typ):
            require(name(key) and name(typ) and key not in nodes,'duplicate, colliding or overlong fact identifier')
            nodes[key]=typ
            return key
        entities=named_map(side['nodes'],192,'invalid entities',empty=False)
        for k,t in entities.items():
            require(name(t),'invalid entity class')
            put(k,'entity/'+t)
        for pred,p in named_map(side['predicates'],24,'invalid predicates').items():
            relation=put('@predicate/'+pred,'predicate/'+str(len(p['slots']))+'/'+p.get('class','any'))
            slots=p['slots']
            require(isinstance(slots,list) and 1<=len(slots)<=4 and all(name(s) for s in slots) and len(set(slots))==len(slots),
                    'invalid local role list')
            classes=p.get('slot_classes',{})
            require(isinstance(classes,dict) and classes.keys()<=set(slots) and all(name(v) for v in classes.values()),
                    'invalid argument classes')
            roles=[put('@slot/'+pred+'/'+s,'slot/'+classes.get(s,'any')) for s in slots]
            edges['kind'] += [[r,relation] for r in roles]
            rows=p['rows']
            require(isinstance(rows,list) and len(rows)<=192,'invalid facts')
            seen=set()
            for j,row in enumerate(rows):
                require(isinstance(row,list) and len(row)==len(roles) and all(e in entities for e in row)
                        and tuple(row) not in seen,'malformed or repeated fact')
                seen.add(tuple(row))
                fact=put('@fact/'+pred+'/'+str(j),'fact');edges['kind'].append([fact,relation])
                for i,(role,e) in enumerate(zip(roles,row)):
                    occurrence=put('@arg/'+pred+'/'+str(j)+'/'+str(i),'argument')
                    edges['of'].append([occurrence,fact]);edges['place'].append([occurrence,role])
                    edges['value'].append([occurrence,e])
        require(len(nodes)<=192,'expanded chart exceeds the finite alignment limit')
        return dict(nodes=nodes,relations=edges)
    out=dict(spec,left=graph(spec['left']),right=graph(spec['right']))
    require('relations' not in spec,'incidence relations are generated from facts')
    # In embedding mode, each left occurrence must have an image; additional right facts may be present.
    out['relations']={k:'closed' for k in out['left']['relations']}
    return out
