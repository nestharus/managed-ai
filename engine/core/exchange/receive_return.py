"""Dispatch a packet, receive its attributed return and validate the retained receipt."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from ..storage import encode, now, read_bytes as _bytes, read_json, sha, write_json
from .carrier_receipts import retain_transport, transport_outcome, transport_view
from .proposal_files import _proposal_bytes, _proposal_directory, _run_nominations, proposal_mappings


class PacketReceiving:
    def dispatch(self, packet: str, argv: list[str], *, proposals: list[str] | None = None,
                 producer: str | None = None) -> dict[str, Any]:
        directory, request = self.load(packet)
        if (directory / 'return.json').exists():
            raise ValueError('packet already has a received return')
        if (directory / 'transport-outcome.json').exists():
            transport_outcome(self, directory, request)
            raise ValueError('terminal transport outcome recorded; prepare a fresh occasion')
        nominations = None
        if proposals:
            mappings = proposal_mappings(request, proposals)
            if producer is not None:
                if not producer.strip():
                    raise ValueError('producer attribution is required')
                nominations = _run_nominations(producer, mappings)
        return self.transport.dispatch(packet, directory, request, argv, nominations=nominations)

    def receive(self, packet: str, reply: Path | None, producer: str | None,
                proposals: list[str], *, wait: float = 0, run_nominations: bool = False) -> dict[str, Any]:
        directory, request = self.load(packet)
        if (directory / 'return.json').exists():
            raise ValueError('return already recorded; preserve it and prepare another occasion')
        if (directory / 'transport-outcome.json').exists():
            transport_outcome(self, directory, request)
            raise ValueError('terminal transport outcome recorded; prepare a fresh occasion')
        if run_nominations and (reply is not None or proposals or producer is not None):
            raise ValueError('original run nominations carry their own files, carrier and producer')
        if not run_nominations and (not producer or not producer.strip()):
            raise ValueError('producer attribution is required; it is caller-asserted, not authenticated')
        mappings = proposal_mappings(request, proposals)
        transport: dict[str, Any]
        origin = None
        bound = False
        if reply is None:
            marker = self.transport.bound_marker(packet, directory, request)
            if 'run_nominations' in marker:
                saved = marker['run_nominations']
                if saved['version'] != 1 or not saved['producer'].strip():
                    raise ValueError('invalid original run nominations')
                if producer is not None and producer != saved['producer']:
                    raise ValueError('receive producer differs from original bounded run')
                producer = saved['producer']
                if mappings and _run_nominations(producer, mappings) != saved:
                    raise ValueError('receive nominations differ from original bounded run')
                bound = True
            elif run_nominations:
                raise ValueError('original run nominations unavailable; use the original explicit receive')
            data, view = self.transport.collect_marked(marker, wait)
            pending = {'packet': packet, 'received': False, 'carrier': view}
            if bound:
                pending['run_nominations'] = True
            if data is None:
                return pending
            if bound:
                proposals = [target + '=' + os.fsdecode(bytes.fromhex(raw)) for target, raw in saved['files']]
                mappings = proposal_mappings(request, proposals)
                origin = saved['origin']
        else:
            if self.transport.has_attempt(directory):
                raise ValueError('a dispatched packet must receive its actual carrier return')
            data = _bytes(reply)
            transport = {'kind': 'caller-import', 'source_path': str(reply.resolve()),
                         'scope': 'caller-asserted producing occurrence; not independently authenticated'}
        expected = {r['path']: r for r in request['targets']}
        changes = []
        with _proposal_directory(origin) as source_directory:
            for target, source in mappings.items():
                try:
                    body = _proposal_bytes(source, source_directory)
                except (OSError, ValueError) as exc:
                    if reply is not None:
                        raise
                    # Diagnostics are Unicode text. Recovery carries the original argument bytes.
                    display = source.encode('utf-8', 'backslashreplace').decode('utf-8')
                    error = {'target': target, 'file': display,
                             'error': str(exc).encode('utf-8', 'backslashreplace').decode('utf-8')}
                    if display != source:
                        error['file_bytes_hex'] = os.fsencode(source).hex()
                    return dict(pending, proposal_error=error)
                proposal = {'path': target, 'sha256': self.put(body), 'bytes': len(body),
                            'base_sha256': expected[target]['sha256']}
                if reply is None:
                    proposal['source'] = 'caller-nominated-file'
                changes.append(proposal)
        reply_sha256 = self.put(data)
        if reply is None:
            transport = retain_transport(self, view)
        record = {'version': 1, 'packet': packet, 'consumer': request['consumer'],
                  'request_sha256': sha(encode(request)), 'producer': producer, 'received_at': now(),
                  'reply_sha256': reply_sha256, 'reply_bytes': len(data), 'proposals': changes, 'transport': transport}
        record['return_sha256'] = sha(encode(record))
        write_json(directory / 'return.json', record)
        return record

    def validate_return(self, directory: Path, record: dict[str, Any]) -> None:
        if (directory / 'transport-outcome.json').exists():
            raise ValueError('packet contains both a return and a terminal outcome')
        if record['return_sha256'] != sha(encode({k: v for k, v in record.items() if k != 'return_sha256'})):
            raise ValueError('return record changed')
        request = read_json(directory / 'request.json')
        if record['request_sha256'] != sha(encode(request)):
            raise ValueError('return belongs to another request')
        if type(record['reply_bytes']) is not int or record['reply_bytes'] < 0:
            raise ValueError('invalid reply length')
        self.objects.read(record['reply_sha256'], expected_bytes=record['reply_bytes'])
        for row in record['proposals']:
            if type(row['bytes']) is not int or row['bytes'] < 0:
                raise ValueError('invalid proposal length')
            self.objects.read(row['sha256'], expected_bytes=row['bytes'])
        view = transport_view(self, record['transport'])
        if view is not None:
            stdout = view['streams']['stdout']
            if (stdout['sha256'] != record['reply_sha256']
                    or stdout['byte_count'] != record['reply_bytes']):
                raise ValueError('transport stdout does not match the reply')
