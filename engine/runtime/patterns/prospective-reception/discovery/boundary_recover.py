"""Choose a minimum acquisition bill for disjoint, currently usable source coverage."""
import argparse
from functools import lru_cache
import json
from pathlib import Path
from boundary_messages import boundary, read, require, validate, Reopen, Limit


def describe(msg, fee, witness):
    return dict(space=msg['space'],members=msg['members'],ports=msg['ports'],
                message=msg['seal'],fee=fee,witness=witness)


def receive(h,card,msg):
    require(msg['seal']==card['message'] and msg['members']==card['members'] and
            msg['ports']==card['ports'] and msg['space']==card['space'],'received a different offer')
    validate(h,msg)
    return msg


def choose(job):
    h=job['catalog'];cards=job['cards'];purpose=job.get('purpose','quote')
    require(purpose in ('quote','award') and isinstance(cards,dict) and len(cards)<=1024,'invalid recovery menu')
    owners=sorted(h['factors']);incidence={k:[] for k in owners}
    eligible={};rejected={}
    for k,card in cards.items():
        require(isinstance(k,str) and k and type(card['fee']) is int and 0<=card['fee']<=1000000 and
                type(card['witness']) is bool and isinstance(card['members'],dict) and bool(card['members']) and
                isinstance(card['ports'],list) and len(set(card['ports']))==len(card['ports']) and
                isinstance(card['message'],str) and bool(card['message']),'malformed acquisition offer')
        members=frozenset(card['members'])
        if card['space']!=h['space'] or any(o not in h['factors'] or h['factors'][o]['pin']!=card['members'][o] for o in members):
            rejected[k]='source-or-authority-changed';continue
        if not set(card['ports'])<={v for o in members for v in h['factors'][o]['scope']}:
            rejected[k]='field-outside-group';continue
        if not set(boundary(h,members))<=set(card['ports']):
            rejected[k]='new-use-of-forgotten-field';continue
        if purpose=='award' and not card['witness']:
            rejected[k]='no-return-route';continue
        eligible[k]=members
        for o in members:incidence[o].append(k)
    limit=job.get('state_limit',200000)
    require(type(limit) is int and 1<=limit<=2000000,'invalid search limit')
    count=0
    @lru_cache(None)
    def solve(remaining):
        nonlocal count
        if count>=limit:raise Limit('cover search limit')
        count+=1
        if not remaining:return 0,()
        options=min(([k for k in incidence[o] if eligible[k]<=remaining] for o in sorted(remaining)),key=len)
        best=None
        for k in sorted(options):
            tail=solve(remaining-eligible[k])
            if tail is None:continue
            candidate=(cards[k]['fee']+tail[0],tuple(sorted((k,*tail[1]))))
            if best is None or candidate<best:best=candidate
        return best
    try:answer=solve(frozenset(owners))
    except Limit:return dict(status='resource-limit',states=count,rejected=rejected)
    if answer is None:return dict(status='no-disjoint-cover',states=count,rejected=rejected)
    return dict(status='covered',fee=answer[0],cards=list(answer[1]),states=count,rejected=rejected)


if __name__=='__main__':
    cli=argparse.ArgumentParser(description=__doc__);cli.add_argument('job',type=Path);a=cli.parse_args()
    try:out=choose(read(a.job))
    except (ValueError,KeyError,TypeError,OSError,AttributeError) as e:cli.exit(2,str(e)+'\n')
    print(json.dumps(out,indent=2,sort_keys=True))
