"""Accepted project membership and verified reads shared by capture and discovery."""
from pathlib import Path

from managed_ai.core.ownership import relative_path
from managed_ai.core.storage import is_reparse_or_symlink, sha
from managed_ai.store.protocol.inputs import bounded_source_bytes


class ProjectSelection:
    def __init__(self):
        self.current = None

    def view(self):
        if self.current is None:
            return None
        return {k: v for k, v in self.current.items() if k != 'project_files'}

    def body(self, row):
        name = relative_path(row['path'])
        root = Path(self.current['project_root'])
        if is_reparse_or_symlink(root.lstat()):
            raise ValueError('accepted project root is a symbolic link')
        path = root
        for part in Path(name).parts:
            if part == '..' or Path(part).is_absolute():
                raise ValueError('invalid accepted-project member path')
            path = path / part
            if is_reparse_or_symlink(path.lstat()):
                raise ValueError('accepted project source contains a symbolic link: ' + name)
        if not path.is_file():
            raise ValueError('ordinary accepted project source required: ' + name)
        body = bounded_source_bytes(path, row['bytes'])
        if len(body) != row['bytes'] or sha(body) != row['sha256']:
            raise ValueError('accepted project source binding changed: ' + name)
        return body

    def capture(self, directory, identities, write_input):
        if self.current is None:
            return {'paths': [], 'selected': [], 'outside': []}
        entries = {r['path']: r for r in self.current['project_files']}
        paths, selected = [], []
        for name in identities:
            if name not in entries:
                raise ValueError('source is outside the accepted project membership: ' + name)
            row = entries[name]
            if row['bytes'] > 2 * 1024 * 1024:
                raise ValueError('project source exceeds the 2 MiB context object bound: ' + name)
            path = write_input(directory, 'project/' + name, self.body(row))
            paths.append(path)
            selected.append(dict(row, captured_path=path))
        return {'paths': paths, 'selected': selected,
                'outside': [name for name in entries if name not in identities]}
