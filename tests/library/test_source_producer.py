"""Native semantics and source qualification for the retained source producer."""

import copy
import os
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

sys.dont_write_bytecode = True
_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))
from managed_ai_information.library import catalogue  # noqa: E402
from managed_ai_information.library import format as lib_format  # noqa: E402
from managed_ai_information.library.catalogue import Library  # noqa: E402
from managed_ai_information.library.producer import Producer  # noqa: E402


def has_fts5():
    if catalogue.sqlite3 is None:
        return False
    try:
        with closing(catalogue.sqlite3.connect(":memory:")) as connection:
            connection.execute("CREATE VIRTUAL TABLE availability USING fts5(body)")
        return True
    except catalogue.SQLITE_ERRORS:
        return False


FTS5_AVAILABLE = has_fts5()
NEEDS_FTS5 = unittest.skipUnless(FTS5_AVAILABLE, "optional SQLite FTS5 unavailable")


class SourceProducerTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name) / "library"
        self.directory.mkdir()
        (self.directory / "objects").mkdir()
        supplied = {
            "a": b"Corrected results from semantic junctions. Complete source.\n",
            "notes/later.md": b"Useful complete source about shared state and evidence.\n",
            "notes/short.md": b"AI evidence.\n",
        }
        rows = []
        for ident, body in supplied.items():
            sha256 = lib_format.digest(body)
            object_path = "objects/" + sha256 + ".txt"
            (self.directory / object_path).write_bytes(body)
            rows.append(
                {
                    "id": ident,
                    "title": ident,
                    "summary": "Complete supplied body",
                    "object": object_path,
                    "sha256": sha256,
                    "bytes": len(body),
                    "source": {"path": ident, "sha256": sha256},
                    "link_base": ident,
                    "references": [],
                }
            )
        modules = [
            {
                "id": "and",
                "title": "Source qualification",
                "purpose": "Source acceptance and exact acquisition",
                "members": ["notes/later.md", "a"],
                "requires": [],
                "parents": [],
                "external_participation": ["A receiver interprets complete sources."],
            },
            {
                "id": "checks",
                "title": "Source evidence",
                "purpose": "Complete source checks",
                "members": ["notes/short.md"],
                "requires": ["and"],
                "parents": [],
                "external_participation": [],
            },
        ]
        self.manifest = {
            "format": lib_format.FORMAT,
            "edition": "test-source-producer",
            "bodies": rows,
            "modules": modules,
            "provenance": {},
        }
        self.publish(self.manifest)
        self.oracle = Library(self.directory)
        self.addCleanup(self.oracle.close)
        self.pin = self.oracle.sha256
        self.producer = Producer(self.directory)
        self.addCleanup(self.producer.close)

    def publish(self, manifest):
        lib_format.check_manifest(manifest)
        data = lib_format.encoded(manifest)
        (self.directory / "catalogue.json").write_bytes(data)
        return lib_format.digest(data)

    def request(self, operation, *, use="current", selected=None, **fields):
        request = dict(operation=operation, **fields)
        if operation != "describe":
            request.update(use=use, library_sha256=selected or self.pin)
        return self.producer.handle(request)

    def assert_ok(self, response, selected=None):
        self.assertEqual(response["status"], "ok", response)
        self.assertEqual(response["library_sha256"], selected or self.pin)

    def assert_refused(self, response):
        self.assertEqual(response["status"], "refused", response)
        self.assertIsInstance(response["error"], str)
        self.assertTrue(response["error"].strip())

    def assert_discovery(self, response, expected):
        self.assert_ok(response, expected["library_sha256"])
        actual = response["result"]
        for key in ("format", "library_sha256", "bodies", "modules", "query", "scope"):
            self.assertEqual(actual[key], expected[key], key)
        for key in (
            "examined_bodies",
            "matched_bodies",
            "omitted_body_count",
            "omitted_body_ids",
            "body_results_truncated",
            "module_results_truncated",
            "limits",
        ):
            self.assertEqual(actual["coverage"][key], expected["coverage"][key], key)

    def assert_read_account(self, response, calls, catalogue_reads, body_reads):
        paths = [Path(call.args[0]) for call in calls]
        catalogues = [path for path in paths if path.name == "catalogue.json"]
        objects = [path for path in paths if path.parent.name == "objects"]
        self.assertEqual(len(catalogues), catalogue_reads)
        self.assertEqual(len(objects), body_reads)
        observation = response["source_observation"]
        self.assertEqual(observation["catalogue_observed_on_this_request"], bool(catalogues))
        self.assertEqual(
            observation["catalogue_read_bytes"], sum(path.stat().st_size for path in catalogues)
        )
        self.assertEqual(observation["canonical_bodies_read"], len(objects))
        self.assertEqual(
            observation["canonical_body_read_bytes"], sum(path.stat().st_size for path in objects)
        )

    def test_describe_and_direct_exact_body_acquisition(self):
        description = self.request("describe")
        self.assert_ok(description)
        self.assertEqual(set(description["operations"]), {"describe", "discover", "show"})
        self.assertTrue(description["source_guarantees"])
        expected = self.oracle.show("notes/later.md")
        for use in ("current", "historical", "current"):
            with self.subTest(use=use):
                with patch.object(catalogue, "read_bytes", wraps=catalogue.read_bytes) as reads:
                    response = self.request("show", id="notes/later.md", use=use)
                self.assert_ok(response)
                self.assertEqual(response["result"], expected)
                self.assert_read_account(response, reads.call_args_list, int(use == "current"), 1)

    @NEEDS_FTS5
    def test_native_fts_order_parsing_modules_and_filtered_identities(self):
        for query in (
            "correcting",
            '"semantic junction"',
            "correcting OR nonexistent",
            "AI",
            "source",
            "a in the",
            "!!!",
            '""',
            "a",
            "and",
            "notes/later.md",
        ):
            with self.subTest(query=query):
                expected = self.oracle.discover(query, backend="fts5")
                self.assert_discovery(self.request("discover", query=query), expected)

    @NEEDS_FTS5
    def test_discovery_reads_bodies_once_for_the_retained_scope(self):
        for use, query, expected_body_reads in (
            ("current", "source", 3),
            ("historical", "source", 0),
            ("current", "correcting", 0),
        ):
            expected = self.oracle.discover(query, backend="fts5")
            with patch.object(catalogue, "read_bytes", wraps=catalogue.read_bytes) as reads:
                response = self.request("discover", query=query, use=use)
            self.assert_discovery(response, expected)
            self.assert_read_account(
                response, reads.call_args_list, int(use == "current"), expected_body_reads
            )
            self.assertEqual(response["result"]["coverage"]["verified_bodies"], expected_body_reads)

    @NEEDS_FTS5
    def test_changed_scopes_preserve_native_coverage_and_result_limits(self):
        short_bytes = self.oracle.bodies["notes/short.md"]["bytes"]
        scopes = [
            {"query": "source"},
            {"query": "source", "limit": 1},
            {"query": "source", "scan_limit": 1},
            {"query": "source"},
            {"query": "AI", "max_bytes": short_bytes},
            {"query": "a", "max_bytes": short_bytes},
            {"query": "notes/later.md", "scan_limit": 1},
            {"query": "and", "max_bytes": 1},
            {"query": "source"},
        ]
        for fields in scopes:
            with self.subTest(fields=fields):
                expected = self.oracle.discover(backend="fts5", **fields)
                self.assert_discovery(self.request("discover", **fields), expected)
        self.assertEqual(
            self.request("show", id="notes/later.md")["result"], self.oracle.show("notes/later.md")
        )

    @NEEDS_FTS5
    def test_current_transition_and_historical_catalogue_qualification(self):
        old_discovery = self.oracle.discover("source", backend="fts5")
        old_body = self.oracle.show("a")
        self.assert_discovery(
            self.request("discover", query="source", use="historical"), old_discovery
        )
        changed = copy.deepcopy(self.manifest)
        changed["edition"] = "test-source-producer-next"
        changed["bodies"][0]["title"] = "New qualification marker"
        current_pin = self.publish(changed)
        self.assert_refused(self.request("discover", query="source"))
        self.assert_refused(self.request("show", id="a"))
        self.assert_ok(self.request("describe"), current_pin)
        with closing(Library(self.directory)) as current:
            self.assert_discovery(
                self.request("discover", query="qualification", selected=current_pin),
                current.discover("qualification", backend="fts5"),
            )
            self.assertEqual(
                self.request("show", id="a", selected=current_pin)["result"], current.show("a")
            )
        self.assert_discovery(
            self.request("discover", query="source", use="historical"), old_discovery
        )
        self.assertEqual(self.request("show", id="a", use="historical")["result"], old_body)

    @NEEDS_FTS5
    def test_unavailable_current_catalogue_preserves_historical_use(self):
        expected = self.oracle.discover("source", backend="fts5")
        self.assert_discovery(self.request("discover", query="source", use="historical"), expected)
        catalogue = self.directory / "catalogue.json"
        original = catalogue.read_bytes()
        for state in ("missing", "invalid"):
            with self.subTest(state=state):
                if state == "missing":
                    catalogue.unlink()
                else:
                    catalogue.write_bytes(b"{}\n")
                self.assert_refused(self.request("describe"))
                self.assert_refused(self.request("discover", query="source"))
                self.assert_refused(self.request("show", id="a"))
                self.assert_discovery(
                    self.request("discover", query="source", use="historical"), expected
                )
                self.assertEqual(
                    self.request("show", id="a", use="historical")["result"], self.oracle.show("a")
                )
                catalogue.write_bytes(original)

    @NEEDS_FTS5
    def test_show_refuses_changed_bytes_while_retained_discovery_can_still_nominate(self):
        expected = self.oracle.discover("source", backend="fts5")
        original_body = self.oracle.show("a")
        self.assert_discovery(self.request("discover", query="source", use="historical"), expected)
        self.assertEqual(self.request("show", id="a")["result"], original_body)
        path = self.directory / self.oracle.bodies["a"]["object"]
        data = path.read_bytes()
        stamp = path.stat()
        path.write_bytes(bytes([data[0] ^ 1]) + data[1:])
        os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        for use in ("current", "historical"):
            self.assert_refused(self.request("show", id="a", use=use))
        retained = self.request("discover", query="source", use="historical")
        self.assert_discovery(retained, expected)
        self.assertEqual(retained["source_observation"]["canonical_bodies_read"], 0)
        path.unlink()
        self.assert_refused(self.request("show", id="a"))
        path.write_bytes(data)
        self.assertEqual(self.request("show", id="a")["result"], original_body)

    def test_native_bounds_unknown_pins_and_invalid_requests_are_refused(self):
        base = {
            "operation": "discover",
            "query": "source",
            "use": "current",
            "library_sha256": self.pin,
        }
        invalid = [
            {"query": ""},
            {"query": '"unclosed'},
            {"query": "é" * 1025},
            {"query": " ".join("unit" + str(i) for i in range(129))},
            {"limit": 0},
            {"limit": 101},
            {"limit": True},
            {"scan_limit": 0},
            {"scan_limit": lib_format.MAX_BODIES + 1},
            {"max_bytes": 0},
            {"max_bytes": lib_format.MAX_PACKAGE + 1},
            {"max_bytes": 1.0},
            {"use": "latest"},
            {"library_sha256": "invalid"},
            {"unknown": True},
        ]
        for fields in invalid:
            with self.subTest(fields=fields):
                self.assert_refused(self.producer.handle({**base, **fields}))
        for use in ("current", "historical"):
            self.assert_refused(
                self.request("discover", query="source", use=use, selected="0" * 64)
            )
            self.assert_refused(self.request("show", id="a", use=use, selected="0" * 64))
        self.assert_refused(self.request("show", id="missing.md"))
        self.assert_refused(self.producer.handle({"operation": "show"}))
        self.assert_refused(self.producer.handle({"operation": "unknown"}))
        self.assert_refused(self.producer.handle(None))

    def test_returned_body_entry_cannot_modify_retained_catalogue(self):
        expected = self.oracle.show("a")
        returned = self.request("show", id="a")["result"]
        returned["entry"]["sha256"] = "0" * 64
        returned["entry"]["references"].append(
            {"target": "changed.md", "anchor": "", "kind": "source"}
        )
        self.assertEqual(self.request("show", id="a", use="historical")["result"], expected)

    @NEEDS_FTS5
    def test_close_releases_native_indexes_and_new_instance_reconstructs(self):
        connections = []
        connect = catalogue.sqlite3.connect

        def observed_connect(*args, **kwargs):
            connection = connect(*args, **kwargs)
            connections.append(connection)
            return connection

        with patch.object(catalogue.sqlite3, "connect", side_effect=observed_connect):
            self.assert_ok(self.request("discover", query="source"))
            self.assert_ok(self.request("discover", query="source", scan_limit=1))
        self.assertTrue(connections)
        self.producer.close()
        self.producer.close()
        for connection in connections:
            with self.assertRaises(catalogue.sqlite3.ProgrammingError):
                connection.execute("SELECT 1")
        self.producer = Producer(self.directory)
        self.addCleanup(self.producer.close)
        expected = self.oracle.discover("source", backend="fts5")
        self.assert_discovery(self.request("discover", query="source"), expected)
        self.assertEqual(self.request("show", id="a")["result"], self.oracle.show("a"))


if __name__ == "__main__":
    unittest.main()
