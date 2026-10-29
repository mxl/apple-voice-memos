import json
import os
from pathlib import Path
import sqlite3
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from typing import Optional

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "apple-voice-memos" / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))
import voice_memos  # type: ignore[import-not-found]  # noqa: E402


CLI = SCRIPT_DIR / "voice-memos"


def atom(atom_type, payload=b"", size_mode="ordinary"):
    atom_type = atom_type.encode("ascii")
    if size_mode == "extended":
        return struct.pack(">I4sQ", 1, atom_type, 16 + len(payload)) + payload
    if size_mode == "zero":
        return struct.pack(">I4s", 0, atom_type) + payload
    return struct.pack(">I4s", 8 + len(payload), atom_type) + payload


def transcript_media(payload=None, tracks=None, extended=False, zero=False):
    if tracks is None:
        tracks = [] if payload is None else [payload]
    track_atoms = []
    for item in tracks:
        children = [] if item is None else [atom("udta", atom("tsrp", item, "extended" if extended else "ordinary"))]
        track_atoms.append(atom("trak", b"".join(children)))
    moov = atom("moov", b"".join(track_atoms), "zero" if zero else "ordinary")
    return atom("ftyp", b"synthetic") + moov


def json_transcript(*segments):
    values = []
    for index, (timestamp, text) in enumerate(segments):
        values.extend([text, {"timeRange": [timestamp, 1.0]}])
    return json.dumps({"attributedString": values}).encode("utf-8")


class VoiceMemosTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "recordings"
        self.root.mkdir()
        self.destination = Path(self.temp.name) / "exports"
        self.destination.mkdir()
        self.database = self.root / "CloudRecordings.db"
        connection = sqlite3.connect(self.database)
        connection.execute(
            """CREATE TABLE ZCLOUDRECORDING (
                ZUUID TEXT,
                ZENCRYPTEDTITLE TEXT,
                ZDATE REAL,
                ZDURATION REAL,
                ZPATH TEXT,
                ZDELETED INTEGER
            )"""
        )
        connection.commit()
        connection.close()

    def tearDown(self):
        self.temp.cleanup()

    def connect(self):
        return sqlite3.connect(self.database)

    def add(self, recording_id, title, date, duration, path, deleted: Optional[int] = 0):
        connection = self.connect()
        connection.execute(
            "INSERT INTO ZCLOUDRECORDING VALUES (?, ?, ?, ?, ?, ?)",
            (recording_id, title, date, duration, path, deleted),
        )
        connection.commit()
        connection.close()

    def run_cli(self, *arguments):
        return subprocess.run(
            [sys.executable, str(CLI), *arguments],
            text=True,
            capture_output=True,
            check=False,
        )

    def overrides(self):
        return ["--database", str(self.database), "--recordings-root", str(self.root)]

    def test_list_filters_dates_wildcards_and_negative_limit(self):
        self.add("old", "old meeting", voice_memos.date_to_core_data("2026-09-09"), 4, "old.m4a")
        self.add("percent", "100% planning", voice_memos.date_to_core_data("2026-09-10"), 5, "percent.m4a")
        self.add("underscore", "100X planning", voice_memos.date_to_core_data("2026-09-11"), 6, "underscore.m4a")
        Path(self.root / "old.m4a").write_bytes(transcript_media())
        Path(self.root / "percent.m4a").write_bytes(transcript_media())
        Path(self.root / "underscore.m4a").write_bytes(transcript_media())

        result = self.run_cli(*self.overrides(), "list", "--search", "100%", "--after", "2026-09-10", "--before", "2026-09-11", "--limit", "-1", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertEqual([row["recording_id"] for row in output["recordings"]], ["percent"])
        self.assertEqual(output["recordings"][0]["recorded_at"], "2026-09-10T00:00:00Z")
        self.assertNotIn("path", output["recordings"][0])

        invalid = self.run_cli(*self.overrides(), "list", "--offset", "-1")
        self.assertEqual(invalid.returncode, voice_memos.EXIT_INVALID_INPUT)
        self.assertEqual(json.loads(invalid.stdout)["error"]["code"], "invalid_input")

    def test_list_probe_isolated_and_reports_statuses(self):
        self.add("good", "good", 0, 1, "good.m4a")
        self.add("bad", "bad", 1, 2, "bad.m4a")
        self.add("missing", "missing", 2, 3, "missing.m4a")
        Path(self.root / "good.m4a").write_bytes(transcript_media(json_transcript((0, "We had had enough"))))
        Path(self.root / "bad.m4a").write_bytes(b"\x00\x00\x00\x01moov")
        result = self.run_cli(*self.overrides(), "list", "--limit", "-1")
        self.assertEqual(result.returncode, 0, result.stderr)
        statuses = {row["recording_id"]: row["transcript_availability"] for row in json.loads(result.stdout)["recordings"]}
        self.assertEqual(statuses, {"good": "yes", "bad": "malformed", "missing": "unavailable"})

    def test_export_is_byte_identical_and_no_clobber(self):
        data = transcript_media()
        (self.root / "plain.m4a").write_bytes(data)
        self.add("plain", "without transcript", 0, 7.5, "plain.m4a")
        result = self.run_cli(*self.overrides(), "export", "--id", "plain", "--to", str(self.destination))
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        published = Path(output["destination"])
        self.assertEqual(published.name, "plain.m4a")
        self.assertEqual(published.read_bytes(), data)
        published.write_bytes(b"sentinel")
        conflict = self.run_cli(*self.overrides(), "export", "--id", "plain", "--to", str(self.destination))
        self.assertEqual(conflict.returncode, voice_memos.EXIT_DESTINATION_CONFLICT)
        self.assertEqual(published.read_bytes(), b"sentinel")
        self.assertNotIn(str(self.root), conflict.stderr)

    def test_export_rejects_traversal_symlink_and_hardlink(self):
        outside = Path(self.temp.name) / "outside.m4a"
        outside.write_bytes(transcript_media())
        self.add("traversal", "traversal", 0, 1, "../outside.m4a")
        self.add("absolute", "absolute", 0, 1, str(outside))
        os.symlink(outside, self.root / "symlink.m4a")
        self.add("symlink", "symlink", 0, 1, "symlink.m4a")
        hardlinked = self.root / "hardlinked.m4a"
        os.link(outside, hardlinked)
        self.add("hardlink", "hardlink", 0, 1, "hardlinked.m4a")

        fifo = self.root / "fifo.m4a"
        os.mkfifo(fifo)
        self.add("fifo", "fifo", 0, 1, "fifo.m4a")

        for recording_id in ("traversal", "absolute", "symlink", "hardlink", "fifo"):
            result = self.run_cli(*self.overrides(), "export", "--id", recording_id, "--to", str(self.destination))
            self.assertEqual(result.returncode, voice_memos.EXIT_UNAVAILABLE, recording_id)
            self.assertNotIn(str(outside), result.stderr)

    def test_export_rejects_intermediate_destination_symlinks_and_inside_root(self):
        (self.root / "source.m4a").write_bytes(transcript_media())
        self.add("source", "source", 0, 1, "source.m4a")

        outside_parent = Path(self.temp.name) / "outside-parent"
        outside_parent.mkdir()
        (outside_parent / "nested").mkdir()
        destination_link = Path(self.temp.name) / "destination-link"
        os.symlink(outside_parent, destination_link)
        through_link = destination_link / "nested"
        symlink_result = self.run_cli(*self.overrides(), "export", "--id", "source", "--to", str(through_link))
        self.assertEqual(symlink_result.returncode, voice_memos.EXIT_UNAVAILABLE)
        self.assertFalse((outside_parent / "nested" / "source.m4a").exists())

        inside = self.root / "inside" / "nested"
        inside.mkdir(parents=True)
        inside_result = self.run_cli(*self.overrides(), "export", "--id", "source", "--to", str(inside))
        self.assertEqual(inside_result.returncode, voice_memos.EXIT_DESTINATION_CONFLICT)
        self.assertFalse((inside / "source.m4a").exists())

    def test_list_marks_duplicate_ids_when_twin_is_outside_page(self):
        (self.root / "duplicate-new.m4a").write_bytes(transcript_media())
        (self.root / "duplicate-old.m4a").write_bytes(transcript_media())
        (self.root / "unique.m4a").write_bytes(transcript_media())
        self.add("duplicate", "duplicate new", 30, 1, "duplicate-new.m4a")
        self.add("duplicate", "duplicate old", 10, 1, "duplicate-old.m4a")
        self.add("unique", "unique", 20, 1, "unique.m4a")

        result = self.run_cli(*self.overrides(), "list", "--limit", "1", "--offset", "0")
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertEqual(output["count"], 1)
        self.assertEqual(output["recordings"][0]["recording_id"], "duplicate")
        self.assertEqual(output["recordings"][0]["error_code"], "duplicate_recording_id")
        self.assertEqual(output["recordings"][0]["row_status"], "malformed")
        self.assertFalse(output["complete"])

    def test_database_is_query_only_and_reads_committed_wal(self):
        writer = self.connect()
        writer.execute("PRAGMA journal_mode = WAL")
        writer.execute(
            "INSERT INTO ZCLOUDRECORDING VALUES (?, ?, ?, ?, ?, ?)",
            ("wal", "wal", 0, 1, "wal.m4a", 0),
        )
        writer.commit()
        (self.root / "wal.m4a").write_bytes(transcript_media())

        reader = voice_memos.open_database(str(self.database))
        try:
            store = voice_memos.MetadataStore(reader)
            self.assertEqual(store.list_raw(None, None, None, -1, 0)[0]["__id"], "wal")
            with self.assertRaises(sqlite3.OperationalError):
                reader.execute("CREATE TABLE forbidden (value INTEGER)")
        finally:
            reader.close()
            writer.close()

    def test_export_detects_source_change_and_cleans_owned_files(self):
        source_path = self.root / "changing.m4a"
        source_path.write_bytes(transcript_media())
        self.add("changing", "changing", 0, 1, "changing.m4a")
        connection = voice_memos.open_database(str(self.database))
        root_fd = voice_memos._open_recordings_root(str(self.root))
        store = voice_memos.MetadataStore(connection)
        original_copy = voice_memos._copy_bounded

        def copy_then_change(source_fd, destination_fd, expected_size):
            copied = original_copy(source_fd, destination_fd, expected_size)
            with source_path.open("ab") as source:
                source.write(b"changed")
            return copied

        try:
            with mock.patch.object(voice_memos, "_copy_bounded", side_effect=copy_then_change):
                with self.assertRaises(voice_memos.SourceChangedError):
                    voice_memos.export_record(store, "changing", str(self.root), root_fd, str(self.destination))
        finally:
            os.close(root_fd)
            connection.close()

        self.assertEqual(list(self.destination.iterdir()), [])

    def test_transcript_atom_header_variants_and_finite_timestamps(self):
        payload = json_transcript((0, "extended"))
        (self.root / "extended.m4a").write_bytes(transcript_media(payload, extended=True))
        (self.root / "zero.m4a").write_bytes(transcript_media(payload, zero=True))
        (self.root / "nan.m4a").write_bytes(transcript_media(b'{"attributedString":["bad",{"timeRange":[NaN,1]}]}'))
        self.add("extended", "extended", 0, 1, "extended.m4a")
        self.add("zero", "zero", 0, 1, "zero.m4a")
        self.add("nan", "nan", 0, 1, "nan.m4a")
        for recording_id in ("extended", "zero"):
            result = self.run_cli(*self.overrides(), "transcript", "--id", recording_id)
            self.assertEqual(result.returncode, 0, result.stderr)
        malformed = self.run_cli(*self.overrides(), "transcript", "--id", "nan")
        self.assertEqual(malformed.returncode, voice_memos.EXIT_MALFORMED_MEDIA)

    def test_transcript_rejects_truncated_extended_headers_and_atom_boundaries(self):
        truncated = struct.pack(">I4s", 1, b"free")
        (self.root / "truncated.m4a").write_bytes(truncated)
        self.add("truncated", "truncated", 0, 1, "truncated.m4a")

        free_atoms = b"".join(atom("free") for _ in range(voice_memos.MAX_ATOM_COUNT - 2))
        at_limit = atom("ftyp", b"x") + atom("moov", free_atoms)
        over_limit = atom("ftyp", b"x") + atom("moov", free_atoms + atom("free"))
        (self.root / "atom-limit.m4a").write_bytes(at_limit)
        (self.root / "atom-over-limit.m4a").write_bytes(over_limit)
        self.add("atom-limit", "atom limit", 1, 1, "atom-limit.m4a")
        self.add("atom-over-limit", "atom over limit", 2, 1, "atom-over-limit.m4a")

        malformed = self.run_cli(*self.overrides(), "transcript", "--id", "truncated")
        self.assertEqual(malformed.returncode, voice_memos.EXIT_MALFORMED_MEDIA)
        at_limit_result = self.run_cli(*self.overrides(), "transcript", "--id", "atom-limit")
        self.assertEqual(at_limit_result.returncode, voice_memos.EXIT_UNAVAILABLE)
        self.assertEqual(json.loads(at_limit_result.stdout)["error"]["code"], "no_transcript")
        over_limit_result = self.run_cli(*self.overrides(), "transcript", "--id", "atom-over-limit")
        self.assertEqual(over_limit_result.returncode, voice_memos.EXIT_MALFORMED_MEDIA)

    def test_transcript_rejects_excessive_atom_depth_and_transcript_payload(self):
        payload = json_transcript((0, "within limits"))
        nested = atom("trak", atom("udta", atom("tsrp", payload)))
        for _ in range(voice_memos.MAX_ATOM_DEPTH - 2):
            nested = atom("moov", nested)
        (self.root / "depth-limit.m4a").write_bytes(atom("ftyp", b"x") + nested)
        self.add("depth-limit", "depth limit", 0, 1, "depth-limit.m4a")

        too_deep = atom("trak", atom("udta", atom("tsrp", payload)))
        for _ in range(voice_memos.MAX_ATOM_DEPTH - 1):
            too_deep = atom("moov", too_deep)
        (self.root / "depth-over-limit.m4a").write_bytes(atom("ftyp", b"x") + too_deep)
        self.add("depth-over-limit", "depth over limit", 1, 1, "depth-over-limit.m4a")

        padding = b" " * (voice_memos.MAX_TRANSCRIPT_PAYLOAD - len(payload))
        at_payload_limit = transcript_media(padding + payload)
        over_payload_limit = transcript_media(b" " + padding + payload)
        (self.root / "payload-limit.m4a").write_bytes(at_payload_limit)
        (self.root / "payload-over-limit.m4a").write_bytes(over_payload_limit)
        self.add("payload-limit", "payload limit", 2, 1, "payload-limit.m4a")
        self.add("payload-over-limit", "payload over limit", 3, 1, "payload-over-limit.m4a")

        depth_limit_result = self.run_cli(*self.overrides(), "transcript", "--id", "depth-limit")
        self.assertEqual(depth_limit_result.returncode, 0, depth_limit_result.stderr)
        depth_over_limit_result = self.run_cli(*self.overrides(), "transcript", "--id", "depth-over-limit")
        self.assertEqual(depth_over_limit_result.returncode, voice_memos.EXIT_MALFORMED_MEDIA)
        payload_limit_result = self.run_cli(*self.overrides(), "transcript", "--id", "payload-limit")
        self.assertEqual(payload_limit_result.returncode, 0, payload_limit_result.stderr)
        payload_over_limit_result = self.run_cli(*self.overrides(), "transcript", "--id", "payload-over-limit")
        self.assertEqual(payload_over_limit_result.returncode, voice_memos.EXIT_MALFORMED_MEDIA)

    def test_transcript_searches_all_tracks_and_preserves_raw_text(self):
        first = atom("trak", atom("udta", atom("free", b"not a transcript")))
        payload = json_transcript((0, "Umberto"), (1, "met"), (2, "Uhtred"), (3, "3."), (4, "14 at 12:30"), (5, "We had"), (6, " had enough"))
        media = atom("ftyp", b"x") + atom("moov", first + atom("trak", atom("udta", atom("tsrp", payload))))
        (self.root / "multi.m4a").write_bytes(media)
        self.add("multi", "multi", 0, 12, "multi.m4a")
        result = self.run_cli(*self.overrides(), "transcript", "--id", "multi")
        self.assertEqual(result.returncode, 0, result.stderr)
        transcript = json.loads(result.stdout)["transcript"]
        segments = json.loads(result.stdout)["segments"]
        self.assertIn("Umberto met Uhtred", transcript)
        self.assertIn("3.14 at 12:30", transcript)
        self.assertIn("We had had enough", transcript)
        self.assertEqual("".join(segment["text"] for segment in segments), "UmbertometUhtred3.14 at 12:30We had had enough")

    def test_transcript_limits_bound_segment_and_text_amplification(self):
        too_many_segments = json_transcript(
            *((0, "x") for _ in range(voice_memos.MAX_TRANSCRIPT_SEGMENTS + 1))
        )
        (self.root / "too-many-segments.m4a").write_bytes(transcript_media(too_many_segments))
        self.add("too-many-segments", "too many segments", 0, 1, "too-many-segments.m4a")

        too_much_text = json_transcript(
            (0, "x" * (voice_memos.MAX_TRANSCRIPT_SOURCE_TEXT_BYTES + 1))
        )
        (self.root / "too-much-text.m4a").write_bytes(transcript_media(too_much_text))
        self.add("too-much-text", "too much text", 1, 1, "too-much-text.m4a")

        for recording_id in ("too-many-segments", "too-much-text"):
            result = self.run_cli(*self.overrides(), "transcript", "--id", recording_id)
            self.assertEqual(result.returncode, voice_memos.EXIT_MALFORMED_MEDIA, recording_id)

    def test_transcript_output_limit_bounds_formatting_amplification(self):
        segments = tuple(
            (1e308 * (1.0 + index * 1e-12), "x")
            for index in range(voice_memos.MAX_TRANSCRIPT_SEGMENTS * 3 // 5)
        )
        payload = json_transcript(*segments)
        self.assertLess(len(payload), voice_memos.MAX_TRANSCRIPT_PAYLOAD)
        (self.root / "output-amplification.m4a").write_bytes(transcript_media(payload))
        self.add("output-amplification", "output amplification", 0, 1, "output-amplification.m4a")

        result = self.run_cli(*self.overrides(), "transcript", "--id", "output-amplification")
        self.assertEqual(result.returncode, voice_memos.EXIT_MALFORMED_MEDIA)

    def test_eviction_date_maps_recently_deleted(self):
        alternate = self.root / "Eviction.db"
        connection = sqlite3.connect(alternate)
        connection.execute(
            """CREATE TABLE ZCLOUDRECORDING (
                ZUNIQUEID TEXT,
                ZENCRYPTEDTITLE TEXT,
                ZDATE REAL,
                ZDURATION REAL,
                ZPATH TEXT,
                ZEVICTIONDATE REAL
            )"""
        )
        connection.execute(
            "INSERT INTO ZCLOUDRECORDING VALUES (?, ?, ?, ?, ?, ?)",
            ("active", "active", 0, 1, "active.m4a", None),
        )
        connection.execute(
            "INSERT INTO ZCLOUDRECORDING VALUES (?, ?, ?, ?, ?, ?)",
            ("deleted", "deleted", 1, 1, "deleted.m4a", 2),
        )
        connection.commit()
        connection.close()
        for name in ("active.m4a", "deleted.m4a"):
            (self.root / name).write_bytes(transcript_media())

        result = self.run_cli(
            "--database", str(alternate),
            "--recordings-root", str(self.root),
            "list", "--limit", "-1",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        statuses = {
            row["recording_id"]: row["deletion_status"]
            for row in json.loads(result.stdout)["recordings"]
        }
        self.assertEqual(statuses, {"deleted": "deleted", "active": "active"})

    def test_malformed_and_empty_transcript_are_controlled(self):
        (self.root / "malformed.m4a").write_bytes(transcript_media(b"{"))
        (self.root / "empty.m4a").write_bytes(transcript_media(b""))
        (self.root / "absent.m4a").write_bytes(transcript_media())
        self.add("malformed", "malformed", 0, 1, "malformed.m4a")
        self.add("empty", "empty", 0, 1, "empty.m4a")
        self.add("absent", "absent", 0, 1, "absent.m4a")
        malformed = self.run_cli(*self.overrides(), "transcript", "--id", "malformed")
        empty = self.run_cli(*self.overrides(), "transcript", "--id", "empty")
        absent = self.run_cli(*self.overrides(), "transcript", "--id", "absent")
        self.assertEqual(malformed.returncode, voice_memos.EXIT_MALFORMED_MEDIA)
        self.assertEqual(json.loads(malformed.stdout)["error"]["code"], "malformed_media")
        self.assertEqual(empty.returncode, voice_memos.EXIT_UNAVAILABLE)
        self.assertEqual(json.loads(empty.stdout)["error"]["code"], "empty_transcript")
        absent_error = json.loads(absent.stdout)["error"]
        self.assertEqual(absent.returncode, voice_memos.EXIT_UNAVAILABLE)
        self.assertEqual(absent_error["code"], "no_transcript")
        self.assertEqual(absent_error["handoff"]["path"], str(self.root / "absent.m4a"))

    def test_unknown_deletion_status_blocks_export_but_list_is_explicit(self):
        (self.root / "unknown.m4a").write_bytes(transcript_media())
        self.add("unknown", "unknown", 0, 1, "unknown.m4a", deleted=None)
        listed = self.run_cli(*self.overrides(), "list")
        self.assertEqual(listed.returncode, 0)
        row = next(row for row in json.loads(listed.stdout)["recordings"] if row["recording_id"] == "unknown")
        self.assertEqual(row["deletion_status"], "unknown")
        result = self.run_cli(*self.overrides(), "export", "--id", "unknown", "--to", str(self.destination))
        self.assertEqual(result.returncode, voice_memos.EXIT_UNAVAILABLE)


if __name__ == "__main__":
    unittest.main()
