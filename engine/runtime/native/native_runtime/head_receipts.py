"""Optional record-owner attestations for verified scope scans of large mutable heads."""
import errno
import hashlib
import hmac
import json
import os

from continuation_support.storage import encode, sha

ATTRIBUTE = 'user.g60.scope.v1'
DOMAIN = b'g60/verified-record-scope/1\0'
MIN_BYTES = 64 * 1024


class HeadReceipts:
    """Only the exchange owner issues projections. Missing evidence uses the ordinary reader."""

    def __init__(self, owner):
        self.key_path = owner / 'head-scope.key'
        self._xattr_unsupported = not hasattr(os, 'getxattr')

    def _key(self, create=False):
        try:
            if create and not self.key_path.exists():
                try:
                    fd = os.open(self.key_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                except FileExistsError:
                    pass
                else:
                    with os.fdopen(fd, 'wb') as key:
                        key.write(os.urandom(32))
            # Kept in the exchange, outside the mutable record/artifact workspace.
            if self.key_path.is_symlink() or not self.key_path.is_file():
                return None
            with self.key_path.open('rb') as key:
                value = key.read(33)
            return value if len(value) == 32 else None
        except OSError:
            return None

    def issue(self, path, record, raw):
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
            os.setxattr(path, ATTRIBUTE, hmac.digest(key, DOMAIN + payload, 'sha256') + payload, follow_symlinks=False)
            return True
        except OSError as exc:
            if exc.errno in (errno.EOPNOTSUPP, errno.ENOTSUP, errno.ENOSYS):
                self._xattr_unsupported = True
            return False

    def projection(self, path, ident, record_format, *, size=None):
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
        summary, raw_digest = json.loads(payload)
        if (summary['format'], summary['id']) == (record_format, ident):
            return summary, raw_digest
        return None

    @staticmethod
    def matches(path, expected):
        # Avoid a contiguous file-sized allocation. Existing canonical input and
        # checksum validation was attested by the owner that issued the projection.
        if path.is_symlink() or not path.is_file():
            return False
        digest = hashlib.sha256()
        with path.open('rb') as source:
            while chunk := source.read(64 * 1024):
                digest.update(chunk)
        return hmac.compare_digest(digest.hexdigest(), expected)
