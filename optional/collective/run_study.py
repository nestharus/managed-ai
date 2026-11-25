#!/usr/bin/env python3
"""Retain a bounded 12-process mechanical study; no model interpretation is simulated."""
from __future__ import annotations

import argparse
import json
import multiprocessing
import os
from pathlib import Path

from collective import Collective, PRODUCT

COUNT = 12
AFFECTED = 5


def checked(result):
    if result['status'] != 'accepted':
        raise RuntimeError(json.dumps(result))
    return result


def request(actor, command, operation, **args):
    return {'actor': actor, 'command': command, 'op': operation, **args}


def participant(database, index, ready, release, returns):
    store = Collective(database)
    actor, target = f'peer-{index:02}', f'edge-{index:02}'
    context = checked(store.call(request(actor, 'capture-original', 'capture',
                                        id=f'context-{index:02}-original', target=target,
                                        sources=['shared-source'])))['context']
    proposal = checked(store.call(request(actor, 'retain-original', 'propose',
                                         id=f'proposal-{index:02}-original', context=context['id'],
                                         body={'edge': index, 'contribution': 'scripted local fixture'})))['proposal']
    ready.put({'index': index, 'pid': os.getpid(), 'context': context['id'],
               'proposal': proposal['id'], 'source_basis': context['basis']})
    if not release.wait(30):
        raise RuntimeError('Mechanical study did not release prepared participants')
    publication = request(actor, 'publish-original', 'publish', proposal=proposal['id'],
                          account='Mechanical fixture admission; this is not a model judgment.')
    result = store.call(publication)
    returns.put({'index': index, 'pid': os.getpid(), 'request': publication, 'result': result})


def run(directory):
    directory = Path(directory).expanduser().resolve()
    if directory.exists() or directory.is_relative_to(PRODUCT):
        raise ValueError('Select a new study directory outside the product tree')
    directory.mkdir(parents=True)
    database = directory / 'collective.sqlite'
    Collective.initialize(database)
    store = Collective(database)
    source = checked(store.call(request('source-owner', 'source', 'create', id='shared-source',
                                        body={'text': 'These source bytes remain unchanged throughout the study.'})))['artifact']
    for index in range(1, COUNT + 1):
        checked(store.call(request(f'peer-{index:02}', 'seed', 'create', id=f'edge-{index:02}',
                                   body={'edge': index, 'state': 'initial'}, reconsider_from=['finding-reader'])))
    multiprocessing_context = multiprocessing.get_context('spawn')
    ready, returns = multiprocessing_context.Queue(), multiprocessing_context.Queue()
    release = multiprocessing_context.Event()
    processes = [multiprocessing_context.Process(target=participant,
                 args=(str(database), index, ready, release, returns)) for index in range(1, COUNT + 1)]
    for process in processes:
        process.start()
    try:
        prepared = [ready.get(timeout=30) for _ in processes]
        target, owner = f'edge-{AFFECTED:02}', f'peer-{AFFECTED:02}'
        impact_request = request('finding-reader', 'new-relation', 'offer', target=target, basis=[source],
                                 account='Scripted mechanical fixture: a newly addressed interpretation reaches one use.',
                                 body={'kind': 'scripted-fixture', 'semantic_discovery_claimed': False,
                                       'relation': {'source': 'shared-source', 'affected_use': target}})
        impact = checked(store.call(impact_request))
        acknowledgment = checked(store.call(request(owner, 'delivery-arrived', 'acknowledge', notice=impact['notice'],
                                                     account='Delivery observed; this alone does not fulfill the receiving duty.')))
        release.set()
        outcomes = [returns.get(timeout=30) for _ in processes]
    finally:
        release.set()
        for process in processes:
            process.join(30)
            if process.is_alive():
                process.terminate()
                process.join()
            if process.exitcode != 0:
                raise RuntimeError(f'Worker failed: {process.exitcode}')
    accepted = [x for x in outcomes if x['result']['status'] == 'accepted']
    refused = [x for x in outcomes if x['result']['status'] == 'refused']
    assert len(accepted) == COUNT - 1, outcomes
    assert len(refused) == 1 and refused[0]['index'] == AFFECTED, outcomes
    assert refused[0]['result']['code'] == 'reconsideration-required'
    assert acknowledgment['state'] == 'pending'
    assert store.notice(impact['notice'])['receipt'] is None
    assert source == {key: store.show('shared-source')[key] for key in source}
    original = store.retained('proposal', f'proposal-{AFFECTED:02}-original')
    current_context = checked(store.call(request(owner, 'capture-reception', 'capture',
                                                id='affected-reception-context', target=target,
                                                sources=['shared-source'])))['context']
    current = checked(store.call(request(owner, 'retain-reception', 'propose',
                                         id='affected-reception-proposal', context=current_context['id'],
                                         body=original['body'])))['proposal']
    publication = request(owner, 'accept-reception', 'publish', proposal=current['id'],
                          treatments=[{'notice': impact['notice'], 'disposition': 'retained-adequate',
                                       'account': 'Fixture receiving account keeps this body; actual semantic interpretation is a separate study.'}],
                          account='Mechanical fixture checks revision and notice reception only.')
    reception = checked(store.call(publication))
    recovered = Collective(database).call(publication)
    assert recovered == reception
    assert store.show(target)['revision'] == 2
    assert current['sha256'] == original['sha256']
    report = {
        'format': 'g60-collective-mechanical-study/1',
        'scope': 'Twelve OS processes exercise local concurrency. No independent semantic agents, inferred relevance, or whole-product adequacy are claimed.',
        'source': source,
        'prepared': sorted(prepared, key=lambda x: x['index']),
        'impact_request': impact_request,
        'impact': impact,
        'delivery_acknowledgment': acknowledgment,
        'first_publications': sorted(outcomes, key=lambda x: x['index']),
        'affected_original_proposal': original,
        'receiving_context': current_context,
        'receiving_proposal': current,
        'receiving_publication': {'request': publication, 'result': reception},
        'recovered_exact_result': recovered,
        'final_notice': store.notice(impact['notice']),
        'checks': {
            'distinct_worker_processes': len({x['pid'] for x in prepared}),
            'prepared_contributions_before_impact': len(prepared),
            'independent_publications_while_impact_pending': len(accepted),
            'only_affected_old_publication_refused': len(refused) == 1,
            'same_source_revision_and_bytes': True,
            'delivery_alone_left_obligation_pending': True,
            'original_proposal_retained': True,
            'unchanged_contribution_can_be_received_again': True,
            'exact_response_recovery_did_not_repeat_mutation': True,
            'whole_composition_semantic_acceptance': 'not attempted by this mechanical exercise'
        },
        'database': str(database)
    }
    report_path = directory / 'study.json'
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    return {'report': str(report_path), 'database': str(database), 'checks': report['checks']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(run(args.directory), indent=2))


if __name__ == '__main__':
    main()
