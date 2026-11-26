"""Temporary library source layouts for workbench binding and replay tests."""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]


def search_layout(root):
    """Build a hermetically pinned src/managed_ai_information/library lexical fixture under root."""
    engine = REPO_ROOT / "src/managed_ai_information"
    lib = root / "src/managed_ai_information/library"
    lib.mkdir(parents=True, exist_ok=True)
    copies = (
        (engine / "library/lexical.py", lib / "lexical.py"),
        (engine / "library/config.py", lib / "config.py"),
    )
    for src, dst in copies:
        dst.write_bytes(src.read_bytes())
    return lib, lib / "lexical.py"


def change_query_parser(path):
    previous = path.read_bytes()
    changed = previous.replace(
        b"    need(backend in", b"    query = 'changed'\n    need(backend in", 1
    )
    if changed == previous:
        raise AssertionError("Fixture must change the selected parser")
    path.write_bytes(changed)
    return previous
