"""Nominate prior observed investigations from a live prospect in the same custody journal."""
import argparse
import json
from pathlib import Path
import re
import sqlite3
import sys

from turn import MAX_PACKET, digest, encode, read, require


def shortened(value, limit):
    return value if len(value) <= limit else value[:limit - 1] + '…'


def pin(entry):
    result = {key: entry[key] for key in ('owner_sha256', 'sha256', 'start', 'end')}
    if 'pin' in entry:
        result['native'] = entry['pin']
    if 'shard' in entry:
        result['shard'] = entry['shard']
    return result


def index_new_records(db):
    db.execute('CREATE TABLE IF NOT EXISTS recall_v1_cursor (id INTEGER PRIMARY KEY CHECK(id=1), through INTEGER NOT NULL)')
    db.execute('INSERT OR IGNORE INTO recall_v1_cursor VALUES(1,0)')
    db.execute('CREATE VIRTUAL TABLE IF NOT EXISTS recall_v1_text USING fts5(intent, decision, materials, finding, tokenize="porter unicode61")')
    db.execute('CREATE TABLE IF NOT EXISTS recall_v1_uses (seq INTEGER PRIMARY KEY, issue TEXT, use TEXT, evidence TEXT, verdict TEXT)')
    db.execute('CREATE INDEX IF NOT EXISTS recall_v1_shared ON recall_v1_uses(issue,evidence,use)')
    db.execute('CREATE INDEX IF NOT EXISTS recall_v1_outcomes ON recall_v1_uses(issue,use,seq)')
    db.execute('CREATE TABLE IF NOT EXISTS recall_v1_edges (parent TEXT, relation TEXT, PRIMARY KEY(parent,relation))')
    through, = db.execute('SELECT through FROM recall_v1_cursor WHERE id=1').fetchone()
    initial = through
    for through, ident, raw in db.execute('SELECT rowid,id,body FROM records WHERE rowid>? ORDER BY rowid', (initial,)):
        require(digest(raw) == ident, 'altered custody record')
        record = json.loads(raw)
        if record['kind'] == 'issue' and record.get('relation') in ('split', 'reopen'):
            db.execute('INSERT OR IGNORE INTO recall_v1_edges VALUES(?,?)', (record['parent'], record['relation']))
        if record['kind'] != 'observation':
            continue
        issue = read(db, record['issue'], 'issue')
        use = next(u for u in issue['uses'] if u['name'] == record['use'])
        intent = '\n'.join(issue[k] for k in ('target', 'gap', 'prediction', 'rival'))
        materials = '\n'.join([issue['partial']['text'], use['authority']['text'], record['evidence']['text']] +
                              [s['role'] + '\n' + s['text'] for s in issue['sources']])
        db.execute('INSERT INTO recall_v1_text(rowid,intent,decision,materials,finding) VALUES(?,?,?,?,?)',
                   (through, intent, use['name'] + '\n' + use['decision'], materials, record['result']))
        db.execute('INSERT INTO recall_v1_uses VALUES(?,?,?,?,?)', (through, record['issue'], record['use'],
                   digest(encode(pin(record['evidence']))), record['verdict']))
    db.execute('UPDATE recall_v1_cursor SET through=? WHERE id=1', (through,))
    return through - initial


def candidate(db, seq, ident, snippet, prospect):
    obs = read(db, ident, 'observation')
    issue = read(db, obs['issue'], 'issue')
    use = next(u for u in issue['uses'] if u['name'] == obs['use'])
    later = db.execute('SELECT r.id,u.verdict FROM recall_v1_uses u JOIN records r ON r.rowid=u.seq '
        'WHERE u.issue=? AND u.use=? AND u.seq>? AND u.verdict<>? ORDER BY u.seq DESC LIMIT 1',
        (obs['issue'], obs['use'], seq, obs['verdict'])).fetchone()
    co = db.execute('SELECT r.id,u.use FROM recall_v1_uses u JOIN records r ON r.rowid=u.seq '
        'WHERE u.issue=? AND u.evidence=? AND u.use<>? GROUP BY u.use ORDER BY MAX(u.seq) DESC LIMIT 2',
        (obs['issue'], digest(encode(pin(obs['evidence']))), obs['use'])).fetchall()
    return dict(observation=ident, issue=obs['issue'], use=shortened(use['name'], 160), decision=shortened(use['decision'], 280),
                target=shortened(issue['target'], 240), verdict=obs['verdict'], result=shortened(obs['result'], 320),
                lexical_match=shortened(snippet, 220), evidence=pin(obs['evidence']), authority=pin(use['authority']),
                sources=[{'role': shortened(s['role'], 100), **pin(s)} for s in issue['sources']],
                same_partial=prospect['partial'] == issue['partial'],
                recorded_branches=[r[0] for r in db.execute('SELECT relation FROM recall_v1_edges WHERE parent=? ORDER BY relation',(ident,))],
                later_different_verdict=dict(observation=later[0], verdict=later[1]) if later else None,
                other_observed_uses_of_same_evidence=[dict(observation=r[0],use=shortened(r[1],160)) for r in co])


def recall(state, prospect_id, limit=3):
    require(type(limit) is int and 1 <= limit <= 5, 'limit must be 1..5')
    state = Path(state)
    require(state.is_file(), 'select an existing journal')
    with sqlite3.connect(state) as db:
        db.execute('BEGIN IMMEDIATE')
        prospect = read(db, prospect_id, 'prospect')
        new_records = index_new_records(db)
        # Quote individual lexical units. The prospect cannot inject SQLite FTS syntax.
        units = list(dict.fromkeys(re.findall(r'[^\W_]+', prospect['query'].casefold())))[:24]
        require(bool(units), 'query has no searchable units')
        expr = ' OR '.join('"' + unit + '"' for unit in units)
        total, = db.execute('SELECT COUNT(*) FROM recall_v1_uses').fetchone()
        matched, = db.execute('SELECT COUNT(*) FROM recall_v1_text WHERE recall_v1_text MATCH ?', (expr,)).fetchone()
        rows = db.execute('SELECT t.rowid,r.id,snippet(recall_v1_text,-1,\'[\',\']\',\'…\',12) '
            'FROM recall_v1_text t JOIN records r ON r.rowid=t.rowid WHERE recall_v1_text MATCH ? '
            'ORDER BY bm25(recall_v1_text),t.rowid DESC LIMIT ?', (expr,limit)).fetchall()
        result = dict(prospect=prospect_id, query=prospect['query'], archive='selected journal observations',
                      observations=total, matched=matched, new_records_indexed=new_records, candidates=[])
        for row in rows:
            item = candidate(db, *row, prospect)
            if len(encode({**result,'candidates':[*result['candidates'],item],'matches_unissued':matched}))+1 > MAX_PACKET:
                break
            result['candidates'].append(item)
        result['matches_unissued'] = matched - len(result['candidates'])
        return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('state')
    parser.add_argument('prospect')
    parser.add_argument('--limit',type=int,default=3)
    args=parser.parse_args()
    try:
        print(encode(recall(args.state,args.prospect,args.limit)).decode())
    except (ValueError, OSError, sqlite3.Error) as exc:
        sys.exit(str(exc))
