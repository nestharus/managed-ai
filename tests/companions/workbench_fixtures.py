"""Temporary src/managed_ai/library source layouts for workbench binding and replay tests."""
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def search_layout(root):
    """Build a hermetically pinned src/managed_ai/library lexical fixture under root."""
    engine = REPO_ROOT / 'src/managed_ai'
    lib = root / 'src/managed_ai/library'
    (lib / 'search').mkdir(parents=True, exist_ok=True)
    if (engine / 'library/lexical.py').is_file():
        copies = (
            (engine / 'library/lexical.py', lib / 'lexical.py'),
            (engine / 'library/config.py', lib / 'config.py'),
            (engine / 'library/search/lexical_search.py', lib / 'search/lexical_search.py'),
        )
        for src, dst in copies:
            dst.write_bytes(src.read_bytes())
        return lib, lib / 'lexical.py'

    for sub in ('reader', 'bundles'):
        (lib / sub).mkdir(parents=True, exist_ok=True)
    if (engine / 'library/reader/knowledge.py').is_file():
        copies = (
            (engine / 'library/reader/knowledge.py', lib / 'reader/knowledge.py'),
            (engine / 'library/reader/library_format.py', lib / 'reader/library_format.py'),
            (engine / 'library/search/lexical_search.py', lib / 'search/lexical_search.py'),
            (engine / 'library/bundles/product_bundle.py', lib / 'bundles/product_bundle.py'),
        )
        for src, dst in copies:
            dst.write_bytes(src.read_bytes())
    else:
        src_dir = engine / 'runtime/source_library'
        knowledge_text = (src_dir / 'knowledge.py').read_text(encoding='utf-8')
        knowledge_text = knowledge_text.replace(
            'from .lexical_search import', 'from ..search.lexical_search import',
        ).replace(
            'from .product_bundle import', 'from ..bundles.product_bundle import',
        )
        lexical_text = (src_dir / 'lexical_search.py').read_text(encoding='utf-8').replace(
            'from .library_format import', 'from ..reader.library_format import',
        )
        bundle_text = (src_dir / 'product_bundle.py').read_text(encoding='utf-8').replace(
            'from .library_format import', 'from ..reader.library_format import',
        )
        (lib / 'reader/knowledge.py').write_bytes(knowledge_text.encode('utf-8'))
        (lib / 'reader/library_format.py').write_bytes((src_dir / 'library_format.py').read_bytes())
        (lib / 'search/lexical_search.py').write_bytes(lexical_text.encode('utf-8'))
        (lib / 'bundles/product_bundle.py').write_bytes(bundle_text.encode('utf-8'))
    return lib, lib / 'reader/knowledge.py'


def change_query_parser(path):
    previous = path.read_bytes()
    changed = previous.replace(b'    need(backend in', b"    query = 'changed'\n    need(backend in", 1)
    if changed == previous:
        raise AssertionError('Fixture must change the selected parser')
    path.write_bytes(changed)
    return previous
