"""Retain and recover carrier evidence and account for terminal attempts."""
from __future__ import annotations

import base64
from copy import deepcopy
from pathlib import Path
from typing import Any

from ..formats import TRANSPORT_FORMAT
from ..ownership import NativeLayout
from ..storage import MAX_BYTES, encode, now, read_json, sha, write_json


def carrier_materials(view: dict[str, Any]):
    """Visit the Collector's material slots without changing their observations."""
    required = (view['origin_request']['origin_brief'],
                view['streams']['stdout'], view['streams']['stderr'])
    for material in required:
        if material.get('present') is not True:
            raise ValueError('required transport material is absent')
        yield material
    if 'current_brief' in view:
        yield view['current_brief']


def utf8(data: bytes) -> str | None:
    try:
        return data.decode('utf-8')
    except UnicodeDecodeError:
        return None


def transport_body(exchange, material: dict[str, Any], *, retained: bool) -> bytes | None:
    if type(material['present']) is not bool:
        raise ValueError('invalid transport material presence')
    if not material['present']:
        return None
    count = material['byte_count']
    if type(count) is not int or count < 0:
        raise ValueError('invalid transport material length')
    if retained:
        if 'base64' in material or 'utf8_text' in material:
            raise ValueError('compact transport contains inline material')
        data = exchange.objects.read(material['sha256'], retained=True, expected_bytes=count)
    else:
        data = base64.b64decode(material['base64'], validate=True)
        if (material['base64'] != base64.b64encode(data).decode('ascii')
                or material['utf8_text'] != utf8(data)):
            raise ValueError('transport text differs from captured bytes')
    if sha(data) != material['sha256'] or len(data) != material['byte_count']:
        raise ValueError('transport material identity or length changed')
    return data


def retain_transport(exchange, view: dict[str, Any]) -> dict[str, Any]:
    """Retain each raw body once, durably, before the receipt is published."""
    compact = deepcopy(view)
    for material in carrier_materials(compact):
        data = transport_body(exchange, material, retained=False)
        if data is not None:
            # Context has the packet bound. Finite captured stderr may exceed
            # the ordinary object bound. Reply/proposal limits stay separate.
            exchange.objects.put_retained(data, max_bytes=len(data))
            del material['base64']
            del material['utf8_text']
    return {'kind': 'local-command', 'carrier_format': TRANSPORT_FORMAT, 'carrier': compact}


def transport_view(exchange, transport: dict[str, Any], *, recover: bool = False) -> dict[str, Any] | None:
    if transport['kind'] == 'caller-import':
        return None
    if transport['kind'] != 'local-command':
        raise ValueError('unknown return transport')
    representation = transport.get('carrier_format')
    if representation not in (None, TRANSPORT_FORMAT):
        raise ValueError('unknown captured transport format')
    view = deepcopy(transport['carrier']) if recover else transport['carrier']
    for material in carrier_materials(view):
        data = transport_body(exchange, material, retained=representation == TRANSPORT_FORMAT)
        if recover and representation == TRANSPORT_FORMAT and data is not None:
            material['base64'] = base64.b64encode(data).decode('ascii')
            material['utf8_text'] = utf8(data)
    return view


def terminal_reason(view: dict[str, Any]) -> str:
    if view.get('stream_integrity') != 'verified':
        raise ValueError('a verified terminal command observation is required')
    result = view.get('result', {})
    if view.get('status') == result.get('status') == 'launch-failed':
        if result.get('returncode') is not None or result.get('signal') is not None:
            raise ValueError('launch failure cannot contain a child exit')
        return 'launch-failed'
    if (view.get('status') == result.get('status') == 'completed'
            and view['streams']['stdout']['byte_count'] > MAX_BYTES):
        return 'reply-limit-exceeded'
    raise ValueError('terminal closure requires launch failure or an oversized completed reply')


def transport_outcome(exchange, directory: Path, request: dict[str, Any]) -> dict[str, Any] | None:
    path = directory / 'transport-outcome.json'
    if not path.exists():
        return None
    record = read_json(path)
    if (directory / 'return.json').exists():
        raise ValueError('packet contains both a return and a terminal outcome')
    if (type(record['version']) is not int or record['version'] != 1
            or record['packet'] != request['packet']
            or record['request_sha256'] != sha(encode(request))
            or record['outcome_sha256'] != sha(encode({k: v for k, v in record.items() if k != 'outcome_sha256'}))):
        raise ValueError('terminal transport outcome binding changed')
    if not isinstance(record['account'], str) or not record['account'].strip():
        raise ValueError('terminal transport outcome requires an owner account')
    view = transport_view(exchange, record['transport'])
    if view is None or record['reason'] != terminal_reason(view):
        raise ValueError('terminal transport reason differs from captured evidence')
    return record


class TransportReceipts:
    def recover_transport(self, packet: str) -> dict[str, Any] | None:
        """Recover the exact captured carrier view, using only retained evidence.

        Old inline local-command records remain readable. Caller-import returns
        have no carrier view. This read grants no effect authority or retry.
        """
        directory, request = self.load(packet)
        if (directory / 'return.json').exists():
            record = read_json(directory / 'return.json')
            self.validate_return(directory, record)
        else:
            record = transport_outcome(self, directory, request)
            if record is None:
                raise ValueError('no retained return or terminal transport outcome')
        return transport_view(self, record['transport'], recover=True)

    def close_transport(self, packet: str, account: str) -> dict[str, Any]:
        """Record an owner-accounted terminal observation without a contribution."""
        directory, request = self.load(packet)
        self._authority.authorize_dispatch(NativeLayout.owner_of(self.workspace), packet)
        if not isinstance(account, str) or not account.strip():
            raise ValueError('an explicit transport closure account is required')
        if (directory / 'return.json').exists():
            raise ValueError('packet already has a received return')
        prior = transport_outcome(self, directory, request)
        if prior is not None:
            if prior['account'] != account:
                raise ValueError('terminal transport outcome already has a different account')
            return prior
        _, view = self.transport.collect(packet, directory, request, wait=0)
        reason = terminal_reason(view)
        record = {'version': 1, 'packet': packet, 'request_sha256': sha(encode(request)),
                  'reason': reason, 'account': account, 'recorded_at': now(),
                  'transport': retain_transport(self, view)}
        record['outcome_sha256'] = sha(encode(record))
        write_json(directory / 'transport-outcome.json', record)
        return record
