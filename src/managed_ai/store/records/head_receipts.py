"""Optional record-owner attestations for verified scope scans of large mutable heads."""
from __future__ import annotations

import errno
import hmac
import os

from managed_ai.core.storage import atomic_bytes, encode, is_ordinary_file_stat, sha
from managed_ai.platform import set_private_file_mode, sha256_file, strict_json_loads

ATTRIBUTE = 'user.g60.scope.v1'
DOMAIN = b'g60/verified-record-scope/1\0'
MIN_BYTES = 64 * 1024


class HeadReceipts:
    """Only the exchange owner issues projections. Missing evidence uses the ordinary reader."""

    def __init__(self, owner):
        self.key_path = owner / 'head-scope.key'
        self._xattr_unsupported = not hasattr(os, 'getxattr')

    def _create_key(self, existing_info) -> None:
        raw_key = os.urandom(32)
        if existing_info is None:
            try:
                fd = os.open(self.key_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                return
            with os.fdopen(fd, 'wb') as key:
                key.write(raw_key)
                key.flush()
                os.fsync(key.fileno())
            return

        # Repair a 0-byte key left by an interrupted prior creation.
        atomic_bytes(self.key_path, raw_key)
        try:
            set_private_file_mode(self.key_path)
        except OSError:
            pass

    def _key(self, create: bool = False) -> bytes | None:
        try:
            try:
                info = self.key_path.lstat()
            except FileNotFoundError:
                info = None

            if info is not None and not is_ordinary_file_stat(info):
                return None

            if create and (info is None or info.st_size == 0):
                self._create_key(info)

            # Kept in the exchange, outside the mutable record/artifact workspace.
            try:
                info = self.key_path.lstat()
            except FileNotFoundError:
                return None

            if not is_ordinary_file_stat(info):
                return None

            with self.key_path.open('rb') as key:
                value = key.read(33)
            return value if len(value) == 32 else None
        except OSError:
            return None

    def issue(self, path, record, raw: bytes) -> bool:
        if len(raw) < MIN_BYTES or not hasattr(os, 'setxattr') or self._xattr_unsupported:
            return False

        # Very long user-defined kinds still work through the standard strict reader.
        keys = ('format', 'id', 'kind', 'sha256', 'revision', 'basis', 'active')
        if any(k not in record for k in keys):
            return False

        summary = {k: record[k] for k in keys}
        payload = encode([summary, sha(raw)])
        if len(payload) > 2048:
            return False

        key = self._key(create=True)
        if key is None:
            return False

        try:
            mac = hmac.digest(key, DOMAIN + payload, 'sha256')
            os.setxattr(path, ATTRIBUTE, mac + payload, follow_symlinks=False)
            return True
        except OSError as exc:
            if exc.errno in (errno.EOPNOTSUPP, errno.ENOTSUP, errno.ENOSYS):
                self._xattr_unsupported = True
            return False

    def projection(self, path, ident, record_format, *, size: int | None = None):
        if self._xattr_unsupported:
            return None
        if size is not None and size < MIN_BYTES:
            return None

        try:
            raw = os.getxattr(path, ATTRIBUTE, follow_symlinks=False)
        except OSError as exc:
            if exc.errno in (errno.EOPNOTSUPP, errno.ENOTSUP, errno.ENOSYS):
                self._xattr_unsupported = True
            return None

        key = self._key()
        if key is None or len(raw) > 2080:
            return None

        tag, payload = raw[:32], raw[32:]
        if not hmac.compare_digest(tag, hmac.digest(key, DOMAIN + payload, 'sha256')):
            return None

        try:
            decoded = strict_json_loads(payload, 'head receipt', require_object=False)
        except ValueError:
            return None

        if not isinstance(decoded, list) or len(decoded) != 2:
            return None

        summary, raw_digest = decoded
        if not isinstance(summary, dict) or not isinstance(raw_digest, str):
            return None

        if (summary.get('format'), summary.get('id')) == (record_format, ident):
            return summary, raw_digest
        return None

    @staticmethod
    def matches(path, expected: str) -> bool:
        """Stream-verify the file digest without a contiguous file-sized allocation."""
        try:
            digest = sha256_file(path, follow_symlinks=False)
        except (OSError, ValueError):
            return False
        return hmac.compare_digest(digest, expected)
