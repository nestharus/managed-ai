"""Bind one frozen packet to one selected carrier attempt and its verified reply."""
from __future__ import annotations

import base64
from pathlib import Path
import time
from typing import Any

from .collector import Collector
from .ownership import EffectAuthority, NativeLayout
from .storage import encode, exclusive, read_json, sha, sync_dir, write_json


class PacketTransport:
    def __init__(self, carrier_root: Path, workspace: Path, authority: EffectAuthority):
        self.carrier_root = carrier_root
        self.workspace = workspace
        self.authority = authority

    @staticmethod
    def _job_id(packet: str) -> str:
        return 'exchange-' + packet

    @staticmethod
    def _marker(directory: Path) -> Path:
        return directory / 'dispatch.json'

    def has_attempt(self, directory: Path) -> bool:
        return self._marker(directory).exists()

    def dispatch(self, packet: str, directory: Path, request: dict, argv: list[str],
                 *, nominations: dict | None = None) -> dict:
        if not any('{packet}' in x or '{context}' in x for x in argv):
            raise ValueError('adapter argv must explicitly receive {packet} or {context}')
        context = directory / 'context.md'
        argv = [x.replace('{packet}', str(directory / 'request.json')).replace('{context}', str(context)) for x in argv]
        job_id = self._job_id(packet)

        def check_owner(selected: str | None) -> None:
            if selected != job_id:
                raise ValueError('only the selected packet owns this launch')
            carrier_owner = NativeLayout.owner_of(carrier.root)
            workspace_owner = NativeLayout.owner_of(self.workspace.resolve())
            if carrier_owner and workspace_owner and carrier_owner != workspace_owner:
                raise ValueError('carrier and workspace belong to different native owners')
            self.authority.authorize_dispatch(carrier_owner or workspace_owner, packet)

        carrier = Collector(self.carrier_root, check_job_owner=check_owner)
        with exclusive(carrier.root / 'owner.lock'):
            path = self._marker(directory)
            marker = {'job_id': job_id, 'argv': argv, 'request_sha256': sha(encode(request))}
            if nominations is not None:
                marker['run_nominations'] = nominations
            recorded = path.exists()
            if recorded and read_json(path) != marker:
                raise ValueError('dispatch already bound; use receive for the original turn')
            # For a nominated run the packet owns the complete intent before a new
            # job becomes visible. A repeat may finish this exact admission only.
            def publish_marker():
                write_json(path, marker)

            bind = publish_marker if nominations is not None and not recorded else None
            if recorded and nominations is not None:
                sync_dir(directory)
            admission = carrier.admit(job_id, argv, self.workspace, context, request['consumer'],
                                      dispatch_binding_sha256=sha(encode(marker)) if nominations is not None else None,
                                      before_new_admission=bind)
            if not recorded and (nominations is None or admission['admission'] == 'already-recorded'):
                # Rebuild a missing nominated marker only after admit verified its
                # exact digest in the prior job. Legacy jobs carry no such digest.
                publish_marker()
            dispatches = carrier.resume(job_id=job_id)
        return {'admission': admission, 'dispatches': dispatches}

    def bound_marker(self, packet: str, directory: Path, request: dict) -> dict:
        marker = read_json(self._marker(directory))
        job_id = self._job_id(packet)
        if marker.get('job_id') != job_id or marker.get('request_sha256') != sha(encode(request)):
            raise ValueError('dispatch does not match the current packet')
        return marker

    def collect(self, packet: str, directory: Path, request: dict, wait: float) -> tuple[bytes | None, dict[str, Any]]:
        return self.collect_marked(self.bound_marker(packet, directory, request), wait)

    def collect_marked(self, marker: dict, wait: float) -> tuple[bytes | None, dict[str, Any]]:
        job_id = marker['job_id']
        carrier = Collector(self.carrier_root)
        deadline = time.monotonic() + max(0.0, wait)
        while True:
            with exclusive(carrier.root / 'owner.lock'):
                view = carrier.views(job_id=job_id)[0]
            if view.get('status') in {'completed', 'launch-failed'} or time.monotonic() >= deadline:
                break
            time.sleep(0.03)
        if 'origin_request' in view:
            binding = view['origin_request'].get('dispatch_binding_sha256')
            expected = sha(encode(marker)) if 'run_nominations' in marker else None
            if binding != expected:
                raise ValueError('carrier admission differs from the nominated dispatch')
        if view.get('status') != 'completed':
            return None, view
        if view.get('stream_integrity') != 'verified':
            raise ValueError('collected return is not integrity-verified')
        return base64.b64decode(view['streams']['stdout']['base64'], validate=True), view
