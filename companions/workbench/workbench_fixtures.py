"""Temporary source layouts for workbench binding and replay tests."""
from pathlib import Path


def search_layout(root):
    engine = Path(__file__).resolve().parents[2] / 'engine'
    runtime = root / 'engine/runtime/source_library'
    runtime.mkdir(parents=True)
    for name in ('knowledge.py', 'library_format.py', 'lexical_search.py', 'product_bundle.py'):
        (runtime / name).write_bytes((engine / 'runtime/source_library' / name).read_bytes())
    return runtime, runtime / 'knowledge.py'


def change_query_parser(path):
    previous = path.read_bytes()
    changed = previous.replace(b'    need(backend in', b"    query = 'changed'\n    need(backend in", 1)
    if changed == previous:
        raise AssertionError('Fixture must change the selected parser')
    path.write_bytes(changed)
    return previous
