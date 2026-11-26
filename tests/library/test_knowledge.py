import copy
import importlib.util
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

sys.dont_write_bytecode = True
_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))
from managed_ai.library.bundle import context as bundle_context  # noqa: E402
from managed_ai.library.bundle import export, import_bundle, inspect_bundle, product  # noqa: E402
from managed_ai.library.catalogue import Library  # noqa: E402
from managed_ai.library.config import LibraryConfig  # noqa: E402
from managed_ai.library.contract import LibraryReader, SearchBackend, SourceProducerService  # noqa: E402
from managed_ai.library.format import (  # noqa: E402
    FORMAT,
    SELECTION,
    check_manifest,
    decoded,
    digest,
    encoded,
)
from managed_ai.library.index import SearchIndex  # noqa: E402
from managed_ai.library.lexical import search_query  # noqa: E402
from managed_ai.library.producer import Producer  # noqa: E402


def _load_publish_module():
    """Load scripts/publish_library.py directly."""
    script = _REPO / "scripts" / "publish_library.py"
    spec = importlib.util.spec_from_file_location("publish_library_script", str(script))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


publish = _load_publish_module()


class KnowledgeTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.library = self.make_library(
            "first", {"meaning/a.md": b"Complete A\n", "meaning/b.md": b"Complete B\n"}
        )

    def tearDown(self):
        self.temporary.cleanup()

    def make_library(self, name, bodies):
        directory = self.root / name
        directory.mkdir()
        (directory / "objects").mkdir()
        rows = []
        for ident, body in bodies.items():
            h = digest(body)
            obj = "objects/" + h + ".txt"
            (directory / obj).write_bytes(body)
            rows.append(
                {
                    "id": ident,
                    "title": ident,
                    "summary": "Complete supplied body",
                    "object": obj,
                    "sha256": h,
                    "bytes": len(body),
                    "source": {"path": ident, "sha256": h},
                    "link_base": ident,
                    "references": [
                        {"target": "outside/history.md", "anchor": "", "kind": "source"}
                    ],
                }
            )
        modules = [
            {
                "id": "base",
                "title": "Base",
                "purpose": "Supply first body",
                "members": [next(iter(bodies))],
                "requires": [],
                "parents": [
                    {
                        "id": "outside-parent",
                        "role": "Historical relation, not required participation",
                    }
                ],
                "external_participation": [
                    "Capable receiver interprets the complete source and supplies authority."
                ],
            }
        ]
        if len(bodies) > 1:
            modules.append(
                {
                    "id": "joined",
                    "title": "Joined use",
                    "purpose": "Supply both declared contributions",
                    "members": [list(bodies)[1]],
                    "requires": ["base"],
                    "parents": [],
                    "external_participation": ["Actual receiving host."],
                }
            )
        manifest = {
            "format": FORMAT,
            "edition": name,
            "bodies": rows,
            "modules": modules,
            "provenance": {},
        }
        (directory / "catalogue.json").write_bytes(encoded(manifest))
        return Library(directory)

    def selection(self, library=None, module="joined"):
        library = library or self.library
        return {
            "format": SELECTION,
            "purpose": "Actual complete-body receiving test",
            "bodies": [],
            "modules": [library.pin(library.modules[module], "module")],
        }

    def test_required_closure_and_full_data_roundtrip(self):
        selection = self.selection()
        bundle = self.root / "out.zip"
        context = bundle_context([self.library], selection)
        self.assertEqual({x["text"] for x in context["bodies"]}, {"Complete A\n", "Complete B\n"})
        result = export([self.library], selection, "joined-product", bundle)
        self.assertEqual((result["bodies"], result["modules"]), (2, 2))
        self.assertTrue(
            any(
                x["target"] == "outside/history.md"
                for x in result["provenance"]["outside_reference_routes"]
            )
        )
        store = self.root / "store"
        store.mkdir()
        first = import_bundle(bundle, store)
        second = import_bundle(bundle, store)
        self.assertEqual(first["status"], "imported-data")
        self.assertEqual(second["status"], "already-present-verified")
        received = Library(first["library"])
        received.verify()
        self.assertEqual(received.body("meaning/a.md"), b"Complete A\n")
        self.assertNotIn("outside-parent", received.modules)
        self.assertIn("No source owner", first["authority"])

    def test_stale_pin_and_complete_body_budget_refuse(self):
        stale = self.selection()
        stale["modules"][0]["sha256"] = "0" * 64
        with self.assertRaises(ValueError):
            bundle_context([self.library], stale)
        with self.assertRaises(ValueError):
            bundle_context([self.library], self.selection(), max_bytes=1)
        self.assertEqual(bundle_context([self.library], self.selection())["body_bytes"], 22)

    def test_colliding_logical_id_needs_explicit_version_selection(self):
        other = self.make_library("second", {"meaning/a.md": b"Different source A\n"})
        selection = {
            "format": SELECTION,
            "purpose": "Compare both versions",
            "modules": [],
            "bodies": [
                self.library.pin(self.library.bodies["meaning/a.md"]),
                other.pin(other.bodies["meaning/a.md"]),
            ],
        }
        self.assertEqual(len(bundle_context([self.library, other], selection)["bodies"]), 2)
        with self.assertRaisesRegex(ValueError, "collision"):
            product([self.library, other], selection, "ambiguous")

    def test_missing_required_member_and_unsupported_schema_refuse(self):
        malformed = copy.deepcopy(self.library.catalogue)
        malformed["modules"][0]["members"].append("missing.md")
        with self.assertRaises(ValueError):
            check_manifest(malformed)
        malformed = copy.deepcopy(self.library.catalogue)
        malformed["format"] = "knowledge-library/99"
        with self.assertRaises(ValueError):
            check_manifest(malformed)
        malformed = copy.deepcopy(self.library.catalogue)
        malformed["modules"][0]["members"] = [{}]
        with self.assertRaises(ValueError):
            check_manifest(malformed)
        with self.assertRaises(ValueError):
            decoded(b'{"same":1,"same":2}')
        with self.assertRaises(ValueError):
            decoded(b'{"numeric":NaN}')

    def test_import_rejects_unselected_members_before_store_mutation(self):
        good = self.root / "good.zip"
        export([self.library], self.selection(), "good", good)
        bad = self.root / "bad.zip"
        with zipfile.ZipFile(good) as source, zipfile.ZipFile(bad, "w") as target:
            for info in source.infolist():
                target.writestr(info, source.read(info.filename))
            target.writestr("../escape.txt", b"Never extracted")
        store = self.root / "store"
        store.mkdir()
        with self.assertRaises(ValueError):
            import_bundle(bad, store)
        self.assertEqual(list(store.iterdir()), [])
        self.assertFalse((self.root / "escape.txt").exists())

    def test_import_rejects_changed_object_and_symlink_input(self):
        good = self.root / "good.zip"
        export([self.library], self.selection(), "good", good)
        bad = self.root / "bad.zip"
        with zipfile.ZipFile(good) as source, zipfile.ZipFile(bad, "w") as target:
            for info in source.infolist():
                body = source.read(info.filename)
                target.writestr(
                    info, b"X" * len(body) if info.filename.startswith("objects/") else body
                )
        store = self.root / "store"
        store.mkdir()
        with self.assertRaises(ValueError):
            import_bundle(bad, store)
        self.assertEqual(list(store.iterdir()), [])
        link = self.root / "linked.zip"
        link.symlink_to(good)
        with self.assertRaises(ValueError):
            inspect_bundle(link)

    def test_discovery_reports_unexamined_bodies(self):
        result = self.library.discover("complete", scan_limit=1, max_bytes=1024)
        self.assertEqual(result["coverage"]["examined_bodies"], 1)
        self.assertEqual(result["coverage"]["omitted_body_ids"], ["meaning/b.md"])
        self.assertEqual(self.library.show("meaning/b.md")["text"], "Complete B\n")

    def test_current_owner_override_and_new_source_inventory_are_checked(self):
        project = self.root / "project"
        (project / "current").mkdir(parents=True)
        (project / "owners").mkdir()
        (project / "current/a.md").write_text("# Compatibility\n\nThis is only a stub.\n")
        (project / "owners/a.txt").write_text("# Complete source\n\nFull operating source.\n")
        spec = {
            "format": "knowledge-source-map/1",
            "families": ["current"],
            "additional": [],
            "modules": [],
            "overrides": {"current/a.md": {"owner": "owners/a.txt", "link_base": "current/a.md"}},
        }
        output = self.root / "published"
        first = publish.publish(project, None, spec, "current", output)
        again = publish.publish(project, None, spec, "current", output)
        self.assertEqual(first["status"], "published-data")
        self.assertEqual(again["status"], "already-present-verified")
        library = Library(output)
        self.assertIn(b"Full operating source", library.body("current/a.md"))
        self.assertEqual(publish.audit(project, None, library, spec)["status"], "same")
        (project / "current/new.md").write_text(
            "# New understanding\n\nA newly necessary contribution.\n"
        )
        audit = publish.audit(project, None, library, spec)
        self.assertEqual(audit["status"], "different")
        self.assertEqual(audit["current_inventory"]["missing_current_bodies"], ["current/new.md"])
        with self.assertRaises(ValueError):
            publish.publish(project, None, spec, "current", output)

    def test_library_config_overrides_and_env(self):
        cfg = LibraryConfig.from_dict(
            {
                "limits": {"max_bodies": 500},
                "search": {"default_limit": 7, "max_retained_catalogues": 3},
            }
        )
        self.assertEqual(cfg.limits.max_bodies, 500)
        self.assertEqual(cfg.search.default_limit, 7)
        self.assertEqual(cfg.search.max_retained_catalogues, 3)

        cfg_path = self.root / "lib-config.json"
        cfg_path.write_text(
            json.dumps({"search": {"max_retained_catalogues": 2}}),
            encoding="utf-8",
        )
        from_file = LibraryConfig.from_json(cfg_path)
        self.assertEqual(from_file.search.max_retained_catalogues, 2)

        from_env = LibraryConfig.from_env({"MANAGED_AI_LIBRARY_MAX_RETAINED_CATALOGUES": "4"})
        self.assertEqual(from_env.search.max_retained_catalogues, 4)

        with self.assertRaises(ValueError):
            LibraryConfig.from_dict({"unknown_section": {}})
        with self.assertRaises(ValueError):
            LibraryConfig.from_dict({"search": {"max_retained_catalogues": 0}})

    def test_producer_lru_eviction_closes_old_catalogues_and_skips_reparse(self):
        lib_dir = self.library.directory
        manifest_path = lib_dir / "catalogue.json"

        def rewrite_edition(edition_name: str) -> str:
            manifest = copy.deepcopy(self.library.catalogue)
            manifest["edition"] = edition_name
            raw = encoded(manifest)
            manifest_path.write_bytes(raw)
            return digest(raw)

        with Producer(lib_dir, max_retained_catalogues=2) as producer:
            self.assertIsInstance(producer, SourceProducerService)
            sha1 = self.library.sha256

            # Unchanged current observation must hit SHA-256 fast-path without reparsing manifest.
            with patch(
                "managed_ai.library.format.check_manifest",
                side_effect=AssertionError("should not reparse"),
            ):
                desc = producer.handle({"operation": "describe"})
            self.assertEqual(desc["status"], "ok")
            self.assertEqual(desc["library_sha256"], sha1)

            # Prepare retained index on edition 1 so eviction has an open search handle to close.
            disc1 = producer.handle(
                {
                    "operation": "discover",
                    "query": "complete",
                    "library_sha256": sha1,
                    "use": "current",
                }
            )
            self.assertEqual(disc1["status"], "ok")
            evicted_lib = producer._libraries[sha1]
            self.assertIsNotNone(evicted_lib.prepared_scope)

            sha2 = rewrite_edition("second-edition")
            desc2 = producer.handle({"operation": "describe"})
            self.assertEqual(desc2["library_sha256"], sha2)

            sha3 = rewrite_edition("third-edition")
            desc3 = producer.handle({"operation": "describe"})
            self.assertEqual(desc3["library_sha256"], sha3)

            # Cap=2 must have evicted edition 1 and closed its retained SQLite connection.
            self.assertEqual(len(producer._libraries), 2)
            self.assertNotIn(sha1, producer._libraries)
            self.assertIsNone(evicted_lib.prepared_scope)

            refused = producer.handle(
                {
                    "operation": "show",
                    "id": "meaning/a.md",
                    "library_sha256": sha1,
                    "use": "historical",
                }
            )
            self.assertEqual(refused["status"], "refused")

    def test_search_index_lazy_phrase_surfaces_and_protocol_conformance(self):
        rows = [
            {
                "id": "a.md",
                "title": "Alpha",
                "summary": "first note",
                "text": "hello world exact phrase here",
            },
            {
                "id": "b.md",
                "title": "Beta",
                "summary": "second note",
                "text": "hello there other words",
            },
        ]
        index = SearchIndex(rows, "bm25")
        self.assertIsInstance(index, SearchBackend)
        self.assertIsInstance(self.library, LibraryReader)

        # P7-12: BM25 term search must NOT materialize duplicated phrase surfaces.
        term_hits = index.search(search_query("hello", "bm25"))
        self.assertEqual(set(term_hits), {"a.md", "b.md"})
        self.assertIsNone(index._fields)

        # Quoted phrase search materializes surfaces lazily and matches accurately.
        phrase_hits = index.search(search_query('"exact phrase"', "bm25"))
        self.assertEqual(set(phrase_hits), {"a.md"})
        self.assertIsNotNone(index._fields)

        # P7-12: Library precomputes module pins on initialization.
        self.assertIn("base", self.library._module_pins)
        self.assertEqual(
            self.library.pin(self.library.modules["base"], "module"),
            self.library._module_pins["base"],
        )

    def test_c48_1_library_cli_main_handles_store_mode_errors_cleanly(self):
        import io
        from contextlib import redirect_stderr

        from managed_ai.library import cli as lib_cli

        for argv in (
            ["show", "nonexistent-item-xyz"],
            ["--store", str(self.root / "missing-store"), "verify"],
            ["discover", "--limit", "0"],
        ):
            buf = io.StringIO()
            with (
                self.subTest(argv=argv),
                redirect_stderr(buf),
                self.assertRaises(SystemExit) as ctx,
            ):
                lib_cli.main(argv)
            self.assertEqual(ctx.exception.code, 2)
            payload = json.loads(buf.getvalue())
            self.assertEqual(payload["status"], "unresolved")
            self.assertTrue(payload["error"])


if __name__ == "__main__":
    unittest.main()
