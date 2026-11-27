"""Bind a workspace, freeze packet inputs, and commit prepared contexts."""

import errno
import os
import shutil
import uuid
from collections.abc import Callable, Generator
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import TYPE_CHECKING, Any

from managed_ai_information.platform import (
    check_unfollowed_ancestors,
    is_reparse_or_symlink,
)

from ..ownership import (
    EffectAuthority,
    NativeLayout,
    StandaloneAuthority,
    external_root,
)
from ..ownership import (
    packet_id as _id,
)
from ..ownership import (
    relative_path as _relative,
)
from ..storage import (
    ObjectStore,
    encode,
    exclusive,
    now,
    open_directory,
    read_json,
    sha,
    sync_dir,
    write_json,
)
from ..storage import (
    atomic_bytes as _atomic_bytes,
)
from ..storage import (
    read_bytes as _bytes,
)
from .transport.packet_transport import PacketTransport
from .workspace_hash import (
    hash_workspace_dirfd,
    hash_workspace_no_dirfd,
    memo_seen,
    memo_store,
    render_packet_context,
)

MAX_PACKET_BYTES = 8 * 1024 * 1024


class PacketContext:
    """One cooperative owner; the caller serializes mutations."""

    if TYPE_CHECKING:

        def validate_return(
            self, directory: Path, record: dict[str, Any], **kwargs: Any
        ) -> None: ...

    def __init__(self, root: Path, *, authority: EffectAuthority | None = None):
        self.root = external_root(root)
        if not (self.root / "workspace.json").is_file():
            raise ValueError("initialize the external exchange first")

        binding = read_json(self.root / "workspace.json")
        version = binding.get("version")
        if type(version) is not int or version not in (1, 2):
            raise ValueError("unsupported exchange workspace version")

        if version == 1:
            workspace = binding.get("workspace")
            if not isinstance(workspace, str) or not Path(workspace).is_absolute():
                raise ValueError("absolute workspace required for exchange version 1")
            self.workspace = check_unfollowed_ancestors(
                workspace, "workspace", include_target=True
            ).resolve()
        else:
            if binding.get("workspace_relative") != "../work":
                raise ValueError("unsupported portable native workspace binding")
            descriptor = NativeLayout(self.root.parent).read_descriptor()
            if (
                not binding.get("instance_id")
                or descriptor.get("instance_id") != binding["instance_id"]
            ):
                raise ValueError("portable workspace belongs to a different instance")
            self.workspace = check_unfollowed_ancestors(
                self.root / binding["workspace_relative"],
                "workspace",
                include_target=True,
            ).resolve()

        self.objects = ObjectStore(self.root / "objects")
        self._authority = StandaloneAuthority() if authority is None else authority
        self.transport = PacketTransport(self.root / "carrier", self.workspace, self._authority)

    @staticmethod
    def initialize(
        root: Path, workspace: Path, *, instance_id: str | None = None
    ) -> dict[str, Any]:
        root = external_root(root)
        workspace = external_root(workspace, "workspace")
        if not workspace.is_dir():
            raise ValueError("workspace and exchange must be disjoint directories")

        if root == workspace or root.is_relative_to(workspace) or workspace.is_relative_to(root):
            raise ValueError("workspace and exchange must be disjoint directories")

        root.mkdir(parents=True, exist_ok=True)
        with exclusive(root / "owner.lock"):
            if instance_id is not None:
                if workspace != root.parent / "work" or root.name != "exchange":
                    raise ValueError("portable binding requires the native store layout")
                record: dict[str, Any] = {
                    "version": 2,
                    "workspace_relative": "../work",
                    "instance_id": instance_id,
                }
            else:
                record = {"version": 1, "workspace": str(workspace)}

            path = root / "workspace.json"
            if path.exists():
                if encode(read_json(path)) != encode(record):
                    raise ValueError("exchange already belongs to another workspace")
            else:
                if any(p.name != "owner.lock" for p in root.iterdir()):
                    raise ValueError("new exchange directory must be empty")
                (root / "objects").mkdir()
                (root / "packets").mkdir()
                write_json(path, record)

        return record

    def path(self, value: str) -> Path:
        rel = _relative(value)
        cursor = self.workspace

        for part in rel.split("/"):
            cursor = cursor / part
            try:
                info = os.lstat(cursor)
            except FileNotFoundError, NotADirectoryError:
                break
            if is_reparse_or_symlink(info):
                raise ValueError("workspace symlinks are not supported")

        resolved = self.workspace / rel
        if not resolved.is_relative_to(self.workspace):
            raise ValueError("path is outside the workspace")
        return resolved

    @contextmanager
    def open_workspace_dir(self, relative: str = "") -> Generator[int]:
        """Open a validated workspace directory using O_NOFOLLOW at every step."""
        parts = _relative(relative).split("/") if relative else []
        opened: list[int] = []

        try:
            try:
                cur = open_directory(self.workspace)
            except OSError as exc:
                if exc.errno == errno.ELOOP:
                    raise ValueError("workspace symlinks are not supported") from None
                raise

            opened.append(cur)
            for part in parts:
                try:
                    nxt = open_directory(part, dir_fd=cur)
                except OSError as exc:
                    if exc.errno == errno.ELOOP:
                        raise ValueError("workspace symlinks are not supported") from None
                    raise
                opened.append(nxt)
                cur = nxt

            yield cur
        finally:
            for fd in reversed(opened):
                os.close(fd)

    def hash_workspace_paths(
        self, paths: list[str], *, max_workers: int = 16
    ) -> dict[str, str | None]:
        """Deduplicate and cryptographically hash workspace files via unfollowed traversal."""
        if not paths:
            return {}

        unique = list(dict.fromkeys(_relative(p) for p in paths))
        if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
            return hash_workspace_no_dirfd(self.workspace, unique, max_workers=max_workers)
        return hash_workspace_dirfd(self.open_workspace_dir, unique, max_workers=max_workers)

    def put(self, data: bytes) -> str:
        return self.objects.put(data)

    def get(self, key: str) -> bytes:
        return self.objects.read(key)

    def snapshot(self, value: str, *, optional: bool = False) -> dict[str, Any]:
        path = self.path(value)
        if not path.exists() and optional:
            return {"path": _relative(value), "sha256": None, "bytes": 0}
        data = _bytes(path)
        return {"path": _relative(value), "sha256": self.put(data), "bytes": len(data)}

    def directory(self, packet: str) -> Path:
        return self.root / "packets" / _id(packet)

    def load(self, packet: str) -> tuple[Path, dict[str, Any]]:
        with self.objects.pack_session():
            return self._load_directory(self.directory(packet))

    def _verify_packet_objects(
        self,
        request: dict[str, Any],
        *,
        verified_objects: dict[str, int] | set[str] | None,
        objects_fd: int | None,
        memo_lock: Any,
    ) -> None:
        direct_objects = type(self).get is PacketContext.get and "get" not in self.__dict__
        entries = [
            request["brief"],
            *request["methods"],
            *request["materials"],
            *request["targets"],
            *request["returns"],
            *request.get("artifacts", []),
        ]
        guard = memo_lock if memo_lock is not None else nullcontext()

        with self.objects.pack_session():
            for entry in entries:
                key = entry["sha256"]
                if key is None or memo_seen(verified_objects, guard, key):
                    continue
                raw_obj = (
                    self.objects.read(key, dir_fd=objects_fd)
                    if direct_objects and objects_fd is not None
                    else self.get(key)
                )
                memo_store(verified_objects, guard, key, len(raw_obj))

    def _load_validated_return(
        self,
        packet_name: str,
        *,
        verified_objects: dict[str, int] | set[str] | None,
        validated_returns: dict[str, dict[str, Any]] | None,
        objects_fd: int | None,
        memo_lock: Any,
        request_shas: dict[str, str] | None,
    ) -> dict[str, Any]:
        guard = memo_lock if memo_lock is not None else nullcontext()
        if validated_returns is not None:
            with guard:
                cached = validated_returns.get(packet_name)
            if cached is not None:
                return cached

        prior_dir = self.directory(packet_name)
        prior = read_json(prior_dir / "return.json")
        prior_req_sha = None
        if request_shas is not None:
            with guard:
                prior_req_sha = request_shas.get(packet_name)

        self.validate_return(
            prior_dir,
            prior,
            request_sha256=prior_req_sha,
            _verified_objects=verified_objects,
            _objects_fd=objects_fd,
            _memo_lock=memo_lock,
        )

        if validated_returns is not None:
            with guard:
                validated_returns[packet_name] = prior
        return prior

    def _load_directory(
        self,
        directory: Path,
        *,
        _verified_objects: dict[str, int] | set[str] | None = None,
        _validated_returns: dict[str, dict[str, Any]] | None = None,
        _objects_fd: int | None = None,
        _memo_lock: Any = None,
        _request_shas: dict[str, str] | None = None,
    ) -> tuple[Path, dict[str, Any]]:
        binding = read_json(directory / "binding.json")
        if binding.get("format") not in (None, "managed-ai-packet-binding/1"):
            raise ValueError("unsupported packet binding format")

        request = read_json(directory / "request.json")
        request_sha = sha(encode(request))
        if (
            request_sha != binding["request_sha256"]
            or sha(_bytes(directory / "context.md", MAX_PACKET_BYTES)) != binding["context_sha256"]
        ):
            raise ValueError("frozen request/context changed")

        if _request_shas is not None:
            with _memo_lock if _memo_lock is not None else nullcontext():
                _request_shas[directory.name] = request_sha

        self._verify_packet_objects(
            request,
            verified_objects=_verified_objects,
            objects_fd=_objects_fd,
            memo_lock=_memo_lock,
        )

        for entry in request["returns"]:
            origin = entry["origin"]
            prior = self._load_validated_return(
                origin["packet"],
                verified_objects=_verified_objects,
                validated_returns=_validated_returns,
                objects_fd=_objects_fd,
                memo_lock=_memo_lock,
                request_shas=_request_shas,
            )
            if (
                prior["return_sha256"] != origin["return_sha256"]
                or prior["reply_sha256"] != entry["sha256"]
            ):
                raise ValueError("prior return correspondence changed")

        for entry in request.get("artifacts", []):
            origin = entry["origin"]
            prior = self._load_validated_return(
                origin["packet"],
                verified_objects=_verified_objects,
                validated_returns=_validated_returns,
                objects_fd=_objects_fd,
                memo_lock=_memo_lock,
                request_shas=_request_shas,
            )
            expected = next(
                (p for p in prior["proposals"] if p["path"] == origin["proposal_path"]), None
            )
            if (
                prior["return_sha256"] != origin["return_sha256"]
                or expected is None
                or entry["sha256"] != expected["sha256"]
                or entry["bytes"] != expected["bytes"]
            ):
                raise ValueError("prior proposal correspondence changed")

        return directory, request

    @staticmethod
    def return_origin(record: dict[str, Any]) -> dict[str, Any]:
        return {
            k: record[k]
            for k in ("packet", "consumer", "producer", "request_sha256", "return_sha256")
        } | {"transport_kind": record["transport"]["kind"]}

    def context(self, packet: str) -> dict[str, Any]:
        """Recover the validated originating context without a current effect check."""
        directory, request = self.load(packet)
        binding = read_json(directory / "binding.json")
        data = _bytes(directory / "context.md", MAX_PACKET_BYTES)

        if (
            sha(data) != binding["context_sha256"]
            or sha(encode(request)) != binding["request_sha256"]
        ):
            raise ValueError("frozen request/context changed")

        return {
            "packet": packet,
            "consumer": request["consumer"],
            "request_sha256": binding["request_sha256"],
            "context_sha256": binding["context_sha256"],
            "context_bytes": len(data),
            "context": data.decode("utf-8"),
            "scope": (
                "Captured originating material; no current effect authority or semantic acceptance."
            ),
        }

    def _collect_prior_materials(
        self,
        returns: list[str],
        artifacts: list[str] | None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        return_entries: list[dict[str, Any]] = []
        for prior in returns:
            olddir, _ = self.load(prior)
            record = read_json(olddir / "return.json")
            self.validate_return(olddir, record)
            return_entries.append(
                {
                    "path": "prior-return:" + prior,
                    "sha256": record["reply_sha256"],
                    "bytes": record["reply_bytes"],
                    "origin": self.return_origin(record),
                    "available_proposals": [
                        {"path": p["path"], "sha256": p["sha256"], "bytes": p["bytes"]}
                        for p in record["proposals"]
                    ],
                }
            )

        artifact_entries: list[dict[str, Any]] = []
        selected_artifacts: set[tuple[str, str]] = set()

        for selection in artifacts or []:
            prior, sep, target = selection.partition(":")
            if not sep:
                raise ValueError("returned artifact selection must be PACKET:TARGET")
            _id(prior)
            target = _relative(target)
            if (prior, target) in selected_artifacts:
                raise ValueError("duplicate returned artifact selection")
            selected_artifacts.add((prior, target))

            olddir, _ = self.load(prior)
            record = read_json(olddir / "return.json")
            self.validate_return(olddir, record)
            proposal = next((p for p in record["proposals"] if p["path"] == target), None)
            if proposal is None:
                raise ValueError("returned artifact does not name a recorded proposal")

            origin = self.return_origin(record) | {"proposal_path": target}
            if proposal.get("source") == "caller-nominated-file":
                origin["proposal_source"] = proposal["source"]
            artifact_entries.append(
                {
                    "path": "prior-proposal:" + prior + ":" + target,
                    "sha256": proposal["sha256"],
                    "bytes": proposal["bytes"],
                    "origin": origin,
                }
            )

        return return_entries, artifact_entries

    def prepare(
        self,
        packet: str,
        consumer: str,
        brief: str,
        methods: list[str],
        materials: list[str],
        targets: list[str],
        returns: list[str],
        artifacts: list[str] | None = None,
        *,
        staged: bool = False,
    ) -> dict[str, Any]:
        _id(packet)
        if not consumer.strip():
            raise ValueError("a named receiving interest/consumer is required")

        directory = self.directory(packet)
        if directory.exists() or self.prepared_directory(packet).exists():
            raise ValueError("packet identity already used or reserved")
        if len(set(map(_relative, targets))) != len(targets):
            raise ValueError("duplicate publication target")

        return_entries, artifact_entries = self._collect_prior_materials(returns, artifacts)
        request: dict[str, Any] = {
            "version": 2,
            "packet": packet,
            "consumer": consumer,
            "created_at": now(),
            "brief": self.snapshot(brief),
            "methods": [self.snapshot(p) for p in methods],
            "materials": [self.snapshot(p) for p in materials],
            "targets": [self.snapshot(p, optional=True) for p in targets],
            "returns": return_entries,
            "artifacts": artifact_entries,
        }

        entries: list[tuple[str, dict[str, Any]]] = [("brief", request["brief"])]
        entries += [
            (role, row)
            for role in ("methods", "materials", "targets", "returns", "artifacts")
            for row in request[role]
        ]
        if sum(row["bytes"] for _, row in entries) > MAX_PACKET_BYTES:
            raise ValueError(
                "selected material exceeds total packet limit; construct a narrower view"
            )

        context_bytes = render_packet_context(
            self.objects,
            self.get,
            packet,
            consumer,
            entries,
            max_packet_bytes=MAX_PACKET_BYTES,
        )
        request_sha = sha(encode(request))
        context_sha = sha(context_bytes)

        staging = self.root / "packets" / (".prepare-" + uuid.uuid4().hex)
        staging.mkdir()
        try:
            write_json(staging / "request.json", request)
            _atomic_bytes(staging / "context.md", context_bytes)
            write_json(
                staging / "binding.json",
                {
                    "format": "managed-ai-packet-binding/1",
                    "request_sha256": request_sha,
                    "context_sha256": context_sha,
                },
            )
            os.rename(staging, self.prepared_directory(packet) if staged else directory)
            sync_dir(directory.parent)
        finally:
            if staging.exists():
                shutil.rmtree(staging)

        return {
            "packet": packet,
            "request": str(directory / "request.json"),
            "context": str(directory / "context.md"),
            "request_sha256": request_sha,
            "selected_bytes": sum(r["bytes"] for _, r in entries),
            **({"context_sha256": context_sha} if staged else {}),
        }

    def prepared_directory(self, packet: str) -> Path:
        return self.root / "packets" / (".sealed-" + _id(packet))

    def commit_prepared(
        self,
        packet: str,
        request_sha256: str,
        context_sha256: str,
        before_publish: Callable[[], None],
    ) -> None:
        """Verify frozen input, then let the owner commit its part before public visibility."""
        pending = self.prepared_directory(packet)
        directory = self.directory(packet)
        if directory.exists():
            raise ValueError("packet identity already used")

        _, request = self._load_directory(pending)
        binding = read_json(pending / "binding.json")
        if (
            request["packet"] != packet
            or binding["request_sha256"] != request_sha256
            or binding["context_sha256"] != context_sha256
        ):
            raise ValueError("sealed packet differs from originating preparation")

        before_publish()
        os.rename(pending, directory)
        sync_dir(directory.parent)
