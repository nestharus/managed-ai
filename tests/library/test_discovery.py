"""Receiving failures: incidental matches, useful ranking, and stale preparation."""

import copy
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.dont_write_bytecode = True
_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))
from tests._paths import ENGINE  # noqa: E402

# isort: split
from managed_ai_information.library import catalogue  # noqa: E402
from managed_ai_information.library import format as lib_format  # noqa: E402
from managed_ai_information.library.catalogue import (  # noqa: E402
    MAX_BATCH_INPUT,
    MAX_BATCH_QUERIES,
    MAX_BATCH_RESULT,
    MAX_INDEX,
    Library,
)

FTS5_AVAILABLE = True
NO_SQLITE_CLI = """
import importlib.abc
import runpy
import sys
class NoSQLiteBinding(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in {'_sqlite3', 'sqlite3'}:
            raise ModuleNotFoundError('simulated Python without sqlite3')
sys.meta_path.insert(0, NoSQLiteBinding())
source, library, backend = sys.argv[1:]
sys.argv = [source, '--library', library, 'discover', '--query', 'Complete', '--backend', backend]
runpy.run_path(source, run_name='__main__')
"""


class DiscoveryTest(unittest.TestCase):
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
            h = lib_format.digest(body)
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
            "format": lib_format.FORMAT,
            "edition": name,
            "bodies": rows,
            "modules": modules,
            "provenance": {},
        }
        (directory / "catalogue.json").write_bytes(lib_format.encoded(manifest))
        return Library(directory)

    @staticmethod
    def ids(result):
        return [r["pin"]["id"] for r in result["bodies"]]

    def test_whole_tokens_keep_meaningful_short_words(self):
        lib = self.make_library(
            "tokens",
            {"one.md": b"Explain rain and living things.", "two.md": b"AI inference is useful."},
        )
        self.assertEqual(self.ids(lib.discover("AI")), ["two.md"])
        self.assertEqual(lib.discover("a in the")["coverage"]["matched_bodies"], 0)
        self.assertEqual(lib.discover("a in the")["coverage"]["examined_bodies"], 0)
        self.assertTrue(lib.discover("a in the")["query"]["empty_after_filtering"])
        self.assertEqual(lib.discover("AI", backend="literal")["coverage"]["matched_bodies"], 2)

    def test_rare_term_can_beat_incidental_common_vocabulary(self):
        bodies = {f"doc/{i}.md": b"shared state change " * 30 for i in range(8)}
        bodies["target.md"] = b"quasar calibration"
        lib = self.make_library("rare", bodies)
        self.assertEqual(self.ids(lib.discover("shared state change quasar"))[0], "target.md")

    def test_ordered_phrase_does_not_cross_fields_or_match_substrings(self):
        lib = self.make_library(
            "phrase",
            {
                "first.md": b"source changed meaning",
                "second.md": b"meaning changed source",
                "third.md": b"sources changed meaningfully",
            },
        )
        self.assertEqual(self.ids(lib.discover('"source changed"')), ["first.md"])
        self.assertEqual(self.ids(lib.discover('"body source"')), [])
        with self.assertRaisesRegex(ValueError, "unclosed"):
            lib.discover('"source')

    def test_known_identity_is_direct_but_does_not_erase_omitted_scope(self):
        lib = self.make_library("identity", {"a": b"One", "later/body.md": b"Two"})
        self.assertEqual(self.ids(lib.discover("a")), ["a"])
        self.assertEqual(self.ids(lib.discover("later/body.md"))[0], "later/body.md")
        bounded = lib.discover("later/body.md", scan_limit=1)
        self.assertNotIn("later/body.md", self.ids(bounded))
        self.assertEqual(bounded["coverage"]["omitted_body_ids"], ["later/body.md"])
        self.assertEqual(lib.show("later/body.md")["text"], "Two")

    def assert_ordinary_search_rechecks_source(self, backend):
        lib = self.make_library("fresh-" + backend, {"a.md": b"Original useful source."})
        first = lib.discover("useful", backend=backend)
        self.assertFalse(first["preparation"]["reused"])
        warmed = lib.discover("useful", backend=backend)
        self.assertTrue(warmed["preparation"]["reused"])
        self.assertEqual(warmed["coverage"]["verified_bodies"], 1)
        (lib.directory / lib.bodies["a.md"]["object"]).write_bytes(b"Changed source bytes.")
        with self.assertRaisesRegex(ValueError, "differs from declared source"):
            lib.discover("useful", backend=backend)
        lib.close()

    def test_reused_index_still_rejects_changed_source_bytes(self):
        self.assert_ordinary_search_rechecks_source("bm25")

    @unittest.skipUnless(FTS5_AVAILABLE, "optional SQLite FTS5 unavailable")
    def test_fts_reused_index_still_rejects_changed_source_bytes(self):
        self.assert_ordinary_search_rechecks_source("fts5")

    def test_changed_scope_replaces_preparation(self):
        self.library.discover("Complete", scan_limit=1)
        full = self.library.discover("Complete")
        self.assertEqual(full["coverage"]["examined_bodies"], 2)
        self.assertFalse(full["preparation"]["reused"])
        self.assertTrue(self.library.discover("Complete")["preparation"]["reused"])
        self.library.close()

    @unittest.skipUnless(FTS5_AVAILABLE, "optional SQLite FTS5 unavailable")
    def test_fts_backend_replaces_preparation(self):
        self.library.discover("Complete")
        self.assertFalse(self.library.discover("Complete", backend="fts5")["preparation"]["reused"])
        self.library.close()

    def test_new_catalogue_metadata_changes_its_index_binding(self):
        self.library.discover("needle")
        manifest = copy.deepcopy(self.library.catalogue)
        manifest["bodies"][1]["title"] = "needle"
        (self.library.directory / "catalogue.json").write_bytes(lib_format.encoded(manifest))
        current = Library(self.library.directory)
        result = current.discover("needle")
        self.assertNotEqual(current.sha256, self.library.sha256)
        self.assertEqual(self.ids(result), ["meaning/b.md"])
        self.assertFalse(result["preparation"]["reused"])

    @unittest.skipUnless(FTS5_AVAILABLE, "optional SQLite FTS5 unavailable")
    def test_fts_stems_and_treats_query_operators_as_data(self):
        lib = self.make_library(
            "fts",
            {
                "one.md": b"Corrected results from semantic junctions.",
                "two.md": b"Unrelated revisions.",
            },
        )
        self.assertEqual(self.ids(lib.discover("correcting", backend="fts5")), ["one.md"])
        self.assertEqual(self.ids(lib.discover('"semantic junction"', backend="fts5")), ["one.md"])
        result = lib.discover("correcting OR nonexistent", backend="fts5")
        self.assertEqual(self.ids(result), ["one.md"])
        self.assertEqual(result["query"]["terms"], ["correcting", "nonexistent"])
        self.assertEqual(result["bodies"][0]["matched_terms"], ["correcting"])
        lib.close()

    def test_cli_without_sqlite_binding_runs_both_bm25_and_fts5(self):
        source = str((ENGINE / "library/cli.py").resolve())
        for backend in ("bm25", "fts5"):
            result = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    "-c",
                    NO_SQLITE_CLI,
                    source,
                    str(self.library.directory),
                    backend,
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            returned = json.loads(result.stdout)["libraries"][0]
            self.assertEqual(returned["coverage"]["matched_bodies"], 2)
            self.assertEqual(returned["query"]["backend"], backend)

    def test_prepared_contentless_index_preserves_rank_phrases_and_module_results(self):
        lib = self.make_library(
            "prepared",
            {
                "one.md": b"Corrected results from semantic junctions.",
                "two.md": b"semantic evidence and useful sources",
                "three.md": b"AI explanation",
            },
        )
        output = self.root / "prepared.idx"
        receipt = lib.prepare_index(output)
        for query in [
            "correcting",
            '"semantic junction"',
            "semantic evidence",
            "AI",
            "one.md",
            "base",
            "a in the",
        ]:
            expected = lib.discover(query, backend="fts5")
            actual = lib.discover(
                query, backend="fts5", index=output, index_sha256=receipt["index"]["sha256"]
            )
            self.assertEqual(actual["bodies"], expected["bodies"])
            self.assertEqual(actual["modules"], expected["modules"])
            self.assertEqual(
                actual["coverage"]["omitted_body_ids"], expected["coverage"]["omitted_body_ids"]
            )
            self.assertEqual(actual["coverage"]["verified_bodies"], 0)
            self.assertEqual(actual["coverage"]["examined_body_bytes"], 0)
        raw_index = output.read_bytes()
        self.assertFalse(raw_index.startswith(b"SQLite format 3\x00"))
        self.assertEqual(lib_format.digest(raw_index), receipt["index"]["sha256"])

    @unittest.skipUnless(FTS5_AVAILABLE, "optional SQLite FTS5 unavailable")
    def test_prepared_nomination_does_not_claim_source_verification(self):
        output = self.root / "source-change.sqlite"
        receipt = self.library.prepare_index(output)
        row = self.library.bodies["meaning/a.md"]
        (self.library.directory / row["object"]).write_bytes(b"Changed! A\n")
        with patch.object(
            self.library, "body", side_effect=AssertionError("nomination read canonical body")
        ):
            result = self.library.discover(
                "Complete", backend="fts5", index=output, index_sha256=receipt["index"]["sha256"]
            )
        self.assertEqual(result["coverage"]["verified_bodies"], 0)
        self.assertIn("meaning/a.md", self.ids(result))
        for operation in [
            lambda: self.library.show("meaning/a.md"),
            self.library.verify,
            lambda: self.library.discover("Complete", backend="fts5"),
        ]:
            with self.assertRaisesRegex(ValueError, "differs from declared source"):
                operation()

    @unittest.skipUnless(FTS5_AVAILABLE, "optional SQLite FTS5 unavailable")
    def test_prepared_index_requires_expected_bytes_and_exact_basis(self):
        output = self.root / "basis.sqlite"
        receipt = self.library.prepare_index(output, scan_limit=1)
        args = {"backend": "fts5", "index": output, "index_sha256": receipt["index"]["sha256"]}
        self.assertEqual(
            self.library.discover("Complete", scan_limit=1, **args)["coverage"]["omitted_body_ids"],
            ["meaning/b.md"],
        )
        with self.assertRaisesRegex(ValueError, "basis differs"):
            self.library.discover("Complete", **args)
        with (
            patch.object(catalogue, "SEARCH_POLICY", "changed-policy"),
            self.assertRaisesRegex(ValueError, "basis differs"),
        ):
            self.library.discover("Complete", scan_limit=1, **args)
        manifest = copy.deepcopy(self.library.catalogue)
        manifest["bodies"][0]["title"] = "Changed metadata"
        (self.library.directory / "catalogue.json").write_bytes(lib_format.encoded(manifest))
        current = Library(self.library.directory)
        with self.assertRaisesRegex(ValueError, "basis differs"):
            current.discover("Complete", scan_limit=1, **args)
        data = bytearray(output.read_bytes())
        data[-1] ^= 1
        output.write_bytes(data)
        with self.assertRaisesRegex(ValueError, "expected digest"):
            self.library.discover("Complete", scan_limit=1, **args)

    @unittest.skipUnless(FTS5_AVAILABLE, "optional SQLite FTS5 unavailable")
    def test_prepared_build_is_fresh_outside_library_and_failure_has_no_output(self):
        with self.assertRaisesRegex(ValueError, "outside the library"):
            self.library.prepare_index(self.library.directory / "index.sqlite")
        sibling = self.root / "sibling"
        sibling.mkdir()
        alias = sibling / ".." / self.library.directory.name
        with self.assertRaisesRegex(ValueError, "outside the library"):
            self.library.prepare_index(alias / "index.sqlite")
        with self.assertRaisesRegex(ValueError, "outside the library"):
            Library(alias).prepare_index(self.library.directory / "index.sqlite")
        self.assertFalse((self.library.directory / "index.sqlite").exists())
        output = self.root / "fresh.sqlite"
        self.library.prepare_index(output)
        before = output.read_bytes()
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.library.prepare_index(output)
        self.assertEqual(output.read_bytes(), before)
        row = self.library.bodies["meaning/a.md"]
        (self.library.directory / row["object"]).write_bytes(b"broken")
        with self.assertRaisesRegex(ValueError, "differs from declared source"):
            self.library.prepare_index(self.root / "failed.sqlite")
        self.assertFalse((self.root / "failed.sqlite").exists())
        self.assertFalse(list(self.root.glob(".knowledge-index-*")))

    def test_prepared_index_rejects_legacy_sqlite_and_explicit_options(self):
        for options in [
            {"index": "not-read"},
            {"index_sha256": "0" * 64},
            {"index": "not-read", "index_sha256": "0" * 64},
        ]:
            with self.assertRaises(ValueError):
                self.library.discover("Complete", **options)
        legacy_sql = self.root / "legacy.sqlite"
        legacy_bytes = b"SQLite format 3\x00" + b"\x00" * 128
        legacy_sql.write_bytes(legacy_bytes)
        with self.assertRaisesRegex(ValueError, "legacy SQLite"):
            self.library.discover(
                "Complete",
                backend="fts5",
                index=legacy_sql,
                index_sha256=lib_format.digest(legacy_bytes),
            )
        self.assertEqual(self.library.discover("Complete")["coverage"]["verified_bodies"], 2)

    def test_empty_query_does_not_assert_an_unread_index_was_verified(self):
        result = self.library.discover(
            "a in the", backend="fts5", index=self.root / "absent.sqlite", index_sha256="0" * 64
        )
        self.assertFalse(result["preparation"]["index_verified"])
        self.assertFalse(result["preparation"]["reused"])
        self.assertEqual(result["preparation"]["index_read_bytes"], 0)
        self.assertEqual(result["coverage"]["examined_bodies"], 0)

    def test_bounded_read_consumes_short_reads_and_rejects_growth(self):
        file = self.root / "read.bin"
        file.write_bytes(b"x")

        class ShortReads(io.BytesIO):
            def read(self, size=-1):
                return super().read(min(size, 17))

        payload = b"retained bytes" * 100
        with patch.object(Path, "open", return_value=ShortReads(payload)):
            self.assertEqual(lib_format.read_bytes(file, len(payload)), payload)
        # The on-disk stat still reports one byte: simulate growth after stat.
        with (
            patch.object(Path, "open", return_value=ShortReads(payload)),
            self.assertRaisesRegex(ValueError, "grew beyond byte bound"),
        ):
            lib_format.read_bytes(file, len(payload) - 1)


class DiscoveryBatchTest(unittest.TestCase):
    make_library = DiscoveryTest.make_library

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.library = self.make_library(
            "batch",
            {
                "one.md": b"Corrected results from semantic junctions.",
                "two.md": "A café has useful sources and evidence.".encode(),
                "three.md": b"AI explanation and independent evidence.",
            },
        )

    def tearDown(self):
        self.library.close()
        self.temporary.cleanup()

    def prepare(self, **scope):
        output = self.root / "prepared.sqlite"
        receipt = self.library.prepare_index(output, **scope)
        return dict(index=output, index_sha256=receipt["index"]["sha256"], **scope)

    @unittest.skipUnless(FTS5_AVAILABLE, "optional SQLite FTS5 unavailable")
    def test_matches_separate_questions_and_acquires_one_image(self):
        args = self.prepare(scan_limit=2)
        queries = [
            "correcting",
            '"semantic junction"',
            "café",
            "one.md",
            "base",
            "a in the",
            "correcting",
        ]
        expected = [self.library.discover(q, backend="fts5", **args) for q in queries]
        actual_read = catalogue.read_bytes
        with (
            patch.object(self.library, "body", side_effect=AssertionError("read canonical body")),
            patch.object(catalogue, "read_bytes", wraps=actual_read) as observed,
        ):
            batch = self.library.discover_batch(queries, **args)
        self.assertEqual(observed.call_count, 1)
        self.assertEqual(observed.call_args.args, (args["index"], MAX_INDEX))
        self.assertEqual(batch["preparation"]["index_read_bytes"], args["index"].stat().st_size)
        self.assertEqual(batch["preparation"]["index_opens"], 1)
        self.assertEqual(batch["preparation"]["canonical_body_read_bytes"], 0)
        for separate, result in zip(expected, batch["results"], strict=False):
            self.assertEqual(
                {k: v for k, v in result.items() if k != "preparation"},
                {k: v for k, v in separate.items() if k != "preparation"},
            )
            self.assertEqual(result["preparation"]["index_read_bytes"], 0)
            self.assertEqual(
                result["preparation"]["index_verified"], separate["preparation"]["index_verified"]
            )
        self.assertEqual(batch["results"][0]["coverage"]["omitted_body_ids"], ["three.md"])
        self.assertEqual(
            batch["results"][5]["coverage"]["omitted_body_ids"], list(self.library.bodies)
        )

    def test_filtered_questions_do_not_open_or_verify_an_image(self):
        with patch.object(catalogue, "PreparedIndex", side_effect=AssertionError("opened index")):
            result = self.library.discover_batch(
                ["a in the", '""'], index=self.root / "absent", index_sha256="0" * 64
            )
        self.assertFalse(result["preparation"]["index_verified"])
        self.assertEqual(result["preparation"]["index_opens"], 0)
        self.assertEqual(result["preparation"]["index_read_bytes"], 0)
        self.assertTrue(all(not x["preparation"]["index_verified"] for x in result["results"]))
        self.assertTrue(all(x["coverage"]["examined_bodies"] == 0 for x in result["results"]))

    def test_invalid_batch_is_rejected_before_index_acquisition(self):
        invalid = [
            [],
            "question",
            {},
            [None],
            [""],
            ["  "],
            ['"unfinished'],
            ["x"] * (MAX_BATCH_QUERIES + 1),
            ["😀" * 600],
            ["x\x00" * 1024] * MAX_BATCH_QUERIES,
        ]
        with patch.object(catalogue, "PreparedIndex", side_effect=AssertionError("opened index")):
            for queries in invalid:
                with self.subTest(queries=str(queries)[:50]), self.assertRaises(ValueError):
                    self.library.discover_batch(
                        queries, index=self.root / "absent", index_sha256="0" * 64
                    )
            for options in [
                {"limit": 0},
                {"scan_limit": 0},
                {"max_bytes": 0},
                {"max_result_bytes": 0},
                {"max_result_bytes": MAX_BATCH_RESULT + 1},
            ]:
                with self.assertRaises(ValueError):
                    self.library.discover_batch(
                        ["useful"], index=self.root / "absent", index_sha256="0" * 64, **options
                    )

    @unittest.skipUnless(FTS5_AVAILABLE, "optional SQLite FTS5 unavailable")
    def test_digest_and_basis_remain_required(self):
        args = self.prepare(scan_limit=1)
        with self.assertRaisesRegex(ValueError, "basis differs"):
            self.library.discover_batch(["useful"], **{**args, "scan_limit": 2})
        with (
            patch.object(catalogue, "SEARCH_POLICY", "changed-policy"),
            self.assertRaisesRegex(ValueError, "basis differs"),
        ):
            self.library.discover_batch(["useful"], **args)
        manifest = copy.deepcopy(self.library.catalogue)
        manifest["bodies"][0]["title"] = "Changed metadata"
        (self.library.directory / "catalogue.json").write_bytes(lib_format.encoded(manifest))
        current = Library(self.library.directory)
        try:
            with self.assertRaisesRegex(ValueError, "basis differs"):
                current.discover_batch(["useful"], **args)
        finally:
            current.close()
        with self.assertRaisesRegex(ValueError, "expected digest"):
            self.library.discover_batch(["useful"], **{**args, "index_sha256": "0" * 64})

    @unittest.skipUnless(FTS5_AVAILABLE, "optional SQLite FTS5 unavailable")
    def test_image_is_a_snapshot_and_next_batch_reacquires(self):
        args = self.prepare()
        before = args["index"].read_bytes()
        expected = self.library.discover_batch(["correcting", "evidence"], **args)
        original_search = catalogue.PreparedIndex.search
        changed = False

        def change_after_read(prepared, table, parsed):
            nonlocal changed
            if not changed:
                corrupted = bytearray(before)
                corrupted[-1] ^= 1
                args["index"].write_bytes(corrupted)
                changed = True
            return original_search(prepared, table, parsed)

        with patch.object(catalogue.PreparedIndex, "search", change_after_read):
            actual = self.library.discover_batch(["correcting", "evidence"], **args)
        self.assertEqual(actual, expected)
        with self.assertRaisesRegex(ValueError, "expected digest"):
            self.library.discover_batch(["correcting"], **args)

    @unittest.skipUnless(FTS5_AVAILABLE, "optional SQLite FTS5 unavailable")
    def test_exact_result_envelope_bound_includes_omissions_and_closes_image(self):
        args = self.prepare(scan_limit=1)
        queries = ["correcting", "a in the", "café"]
        result = self.library.discover_batch(queries, **args)
        bound = len(lib_format.encoded(result))
        # The numeric bound is itself in the envelope. Reach its exact fixed
        # point, then verify that removing one byte refuses the whole result.
        for _ in range(5):
            result = self.library.discover_batch(queries, max_result_bytes=bound, **args)
            size = len(lib_format.encoded(result))
            if size == bound:
                break
            bound = size
        self.assertEqual(size, bound)
        opened = []

        class TrackedIndex(catalogue.PreparedIndex):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                opened.append(self)

        with (
            patch.object(catalogue, "PreparedIndex", TrackedIndex),
            self.assertRaisesRegex(ValueError, "JSON byte bound"),
        ):
            self.library.discover_batch(queries, max_result_bytes=bound - 1, **args)
        self.assertEqual(len(opened), 1)
        self.assertIsNone(opened[0].db)
        self.assertIsNone(opened[0].temporary)
        self.assertIn("three.md", result["results"][0]["coverage"]["omitted_body_ids"])

    def test_query_failure_closes_image_without_partial_return(self):
        args = self.prepare()
        opened = []

        class FailingIndex(catalogue.PreparedIndex):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                opened.append(self)
                self.calls = 0

            def search(self, table, parsed):
                self.calls += 1
                if self.calls == 3:
                    raise RuntimeError("simulated query interruption")
                return super().search(table, parsed)

        with (
            patch.object(catalogue, "PreparedIndex", FailingIndex),
            self.assertRaisesRegex(RuntimeError, "simulated query interruption"),
        ):
            self.library.discover_batch(["correcting", "evidence"], **args)
        self.assertEqual(len(opened), 1)
        self.assertIsNone(opened[0].db)
        self.assertIsNone(opened[0].temporary)

    @unittest.skipUnless(FTS5_AVAILABLE, "optional SQLite FTS5 unavailable")
    def test_cli_batch_returns_ordered_results_and_refuses_oversized_input(self):
        args = self.prepare()
        query_file = self.root / "questions.json"
        queries = ['"semantic junction"', "café", "a in the"]
        query_file.write_bytes(lib_format.encoded(queries))
        command = [
            sys.executable,
            "-B",
            str((ENGINE / "library/cli.py").resolve()),
            "--library",
            str(self.library.directory),
            "discover-batch",
            "--queries",
            str(query_file),
            "--index",
            str(args["index"]),
            "--index-sha256",
            args["index_sha256"],
        ]
        result = subprocess.run(command, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), self.library.discover_batch(queries, **args))
        self.assertLessEqual(len(result.stdout), MAX_BATCH_RESULT)
        query_file.write_bytes(b" " * MAX_BATCH_INPUT + b"[]")
        refused = subprocess.run(command, capture_output=True)
        self.assertEqual(refused.returncode, 2)
        self.assertEqual(refused.stdout, b"")
        self.assertEqual(json.loads(refused.stderr)["status"], "refused")


if __name__ == "__main__":
    unittest.main()
