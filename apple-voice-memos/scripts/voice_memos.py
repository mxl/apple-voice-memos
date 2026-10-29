#!/usr/bin/env python3
# SPDX-License-Identifier: 0BSD
"""Safe, ID-based access to Apple Voice Memos recordings.

This module intentionally has no third-party dependencies.  The public entry
point is :func:`main`; the executable next to this file only imports it.
"""

from __future__ import annotations

import argparse
import base64
import datetime as _datetime
import hashlib
import json
import math
import os
from pathlib import Path
import secrets
import sqlite3
import struct
import sys
from dataclasses import dataclass
from typing import Any, Iterable, NoReturn, Optional, cast
from urllib.parse import quote


MINIMUM_PYTHON = (3, 10)
CORE_DATA_EPOCH = _datetime.datetime(2001, 1, 1, tzinfo=_datetime.timezone.utc)
DEFAULT_RECORDINGS_RELATIVE = (
    "Library",
    "Group Containers",
    "group.com.apple.VoiceMemos.shared",
    "Recordings",
)
DEFAULT_DATABASE_NAME = "CloudRecordings.db"

# These are deliberately conservative.  They bound reads and parser work, but
# do not impose a limit on the size of an unrelated mdat atom.
MAX_PATH_LENGTH = 4096
MAX_PATH_COMPONENTS = 128
MAX_ATOM_COUNT = 100_000
MAX_ATOM_DEPTH = 32
MAX_TRANSCRIPT_PAYLOAD = 16 * 1024 * 1024
MAX_TRANSCRIPT_SEGMENTS = 100_000
MAX_TRANSCRIPT_SOURCE_TEXT_BYTES = 8 * 1024 * 1024
MAX_TRANSCRIPT_OUTPUT_BYTES = 16 * 1024 * 1024
MAX_COPY_BYTES = 8 * 1024 * 1024 * 1024
COPY_CHUNK_SIZE = 1024 * 1024

EXIT_OK = 0
EXIT_INVALID_INPUT = 2
EXIT_DATABASE = 3
EXIT_UNAVAILABLE = 4
EXIT_DESTINATION_CONFLICT = 5
EXIT_MALFORMED_MEDIA = 6
EXIT_SOURCE_CHANGED = 7


class VoiceMemosError(Exception):
    """An expected, redacted command failure."""

    code = "error"
    exit_status = EXIT_DATABASE


class InvalidInputError(VoiceMemosError):
    code = "invalid_input"
    exit_status = EXIT_INVALID_INPUT


class DatabaseError(VoiceMemosError):
    code = "database_error"
    exit_status = EXIT_DATABASE


class SchemaError(DatabaseError):
    code = "schema_error"


class SourceUnavailableError(VoiceMemosError):
    code = "source_unavailable"
    exit_status = EXIT_UNAVAILABLE


class DestinationConflictError(VoiceMemosError):
    code = "destination_conflict"
    exit_status = EXIT_DESTINATION_CONFLICT


class MalformedMediaError(VoiceMemosError):
    code = "malformed_media"
    exit_status = EXIT_MALFORMED_MEDIA


class SourceChangedError(VoiceMemosError):
    code = "source_changed"
    exit_status = EXIT_SOURCE_CHANGED


class NoTranscriptError(VoiceMemosError):
    code = "no_transcript"
    exit_status = EXIT_UNAVAILABLE

    def __init__(self, handoff: Optional[dict[str, Any]] = None):
        super().__init__()
        self.handoff = handoff


class EmptyTranscriptError(VoiceMemosError):
    code = "empty_transcript"
    exit_status = EXIT_UNAVAILABLE


class UnsupportedTranscriptError(VoiceMemosError):
    code = "unsupported_transcript"
    exit_status = EXIT_UNAVAILABLE


class MediaMalformed(Exception):
    """Internal parser failure; callers choose the public error/status."""


class TranscriptMalformed(Exception):
    pass


class TranscriptUnsupported(Exception):
    pass


class TranscriptEmpty(Exception):
    pass


@dataclass(frozen=True)
class Atom:
    atom_type: bytes
    start: int
    header_size: int
    end: int

    @property
    def payload_start(self) -> int:
        return self.start + self.header_size

    @property
    def payload_size(self) -> int:
        return self.end - self.payload_start


@dataclass(frozen=True)
class TranscriptSegment:
    timestamp: float
    text: str


@dataclass(frozen=True)
class MetadataRecord:
    recording_id: str
    title: str
    recorded_at: str
    duration: float
    deletion_status: str
    path: str
    raw_id: Any


@dataclass(frozen=True)
class Schema:
    table: str
    title: str
    date: str
    duration: str
    path: str
    identifier: str
    deletion_flags: tuple[str, ...]
    deletion_dates: tuple[str, ...]


@dataclass(frozen=True)
class SourceSignature:
    device: int
    inode: int
    size: int
    mtime_ns: int
    ctime_ns: int


@dataclass(frozen=True)
class OpenSource:
    fd: int
    signature: SourceSignature
    extension: str


class ArgumentParser(argparse.ArgumentParser):
    """Argparse parser that never prints unredacted input in an error."""

    def error(self, message: str) -> NoReturn:  # pragma: no cover - exercised by CLI
        raise InvalidInputError()


def _quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _is_finite_number(value: Any) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(float(value))
    except (OverflowError, ValueError):
        return False


def _valid_timestamp(value: Any) -> float:
    if not _is_finite_number(value) or float(value) < 0:
        raise TranscriptMalformed()
    return float(value)


def core_data_to_iso(value: Any) -> str:
    if not _is_finite_number(value):
        raise ValueError
    try:
        date = CORE_DATA_EPOCH + _datetime.timedelta(seconds=float(value))
    except (OverflowError, ValueError):
        raise ValueError from None
    return date.astimezone(_datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def date_to_core_data(value: str) -> float:
    try:
        parsed = _datetime.datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=_datetime.timezone.utc)
    except ValueError:
        raise InvalidInputError from None
    return (parsed - CORE_DATA_EPOCH).total_seconds()


def _default_recordings_root() -> str:
    return os.path.join(str(Path.home()), *DEFAULT_RECORDINGS_RELATIVE)


def resolve_paths(database: Optional[str], recordings_root: Optional[str]) -> tuple[str, str]:
    if database is not None and ("\x00" in database or not database):
        raise InvalidInputError()
    if recordings_root is not None and ("\x00" in recordings_root or not recordings_root):
        raise InvalidInputError()

    if recordings_root is None:
        if database is not None:
            root = os.path.dirname(os.path.abspath(database))
        else:
            root = _default_recordings_root()
    else:
        root = recordings_root
    root = os.path.abspath(os.path.expanduser(root))

    if database is None:
        database = os.path.join(root, DEFAULT_DATABASE_NAME)
    database = os.path.abspath(os.path.expanduser(database))
    return database, root


def _validate_database_path(path: str) -> None:
    try:
        info = os.lstat(path)
    except OSError:
        raise DatabaseError from None
    if not os.path.isfile(path) or os.path.islink(path) or not stat_is_regular(info.st_mode):
        raise DatabaseError


def stat_is_regular(mode: int) -> bool:
    return (mode & 0o170000) == 0o100000


def open_database(path: str) -> sqlite3.Connection:
    _validate_database_path(path)
    # quote() prevents '?' or '#' in an explicitly supplied path from changing
    # URI parameters.  mode=ro is intentional; immutable=1 is not used because
    # live WAL data must remain visible.
    uri = "file:" + quote(path, safe="/") + "?mode=ro"
    connection: Optional[sqlite3.Connection] = None
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=0.2)
        connection.execute("PRAGMA query_only = ON")
    except (OSError, sqlite3.Error):
        if connection is not None:
            try:
                connection.close()
            except sqlite3.Error:
                pass
        raise DatabaseError from None
    return connection


class MetadataStore:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection
        self.schema = self._inspect_schema()

    def _inspect_schema(self) -> Schema:
        try:
            table_rows = self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND lower(name) = ?",
                ("zcloudrecording",),
            ).fetchall()
        except sqlite3.Error:
            raise SchemaError from None
        if len(table_rows) != 1:
            raise SchemaError
        table = str(table_rows[0][0])
        try:
            columns = self.connection.execute(
                "PRAGMA table_info(" + _quote_identifier(table) + ")"
            ).fetchall()
        except sqlite3.Error:
            raise SchemaError from None
        by_upper = {str(row[1]).upper(): str(row[1]) for row in columns}

        def required(name: str) -> str:
            if name not in by_upper:
                raise SchemaError
            return by_upper[name]

        # These mappings are the observed Voice Memos Core Data layout.  A
        # column's mere presence is not treated as proof for deletion unless it
        # is one of the explicit, semantically named fields below.
        identifier = None
        for candidate in ("ZUNIQUEID", "ZUUID"):
            if candidate in by_upper:
                identifier = by_upper[candidate]
                break
        if identifier is None:
            raise SchemaError

        flag_names = (
            "ZDELETED",
            "ZISDELETED",
            "ZTRASHED",
            "ZISTRASHED",
        )
        date_names = ("ZEVICTIONDATE", "ZDELETEDDATE", "ZTRASHEDDATE")
        return Schema(
            table=table,
            title=required("ZENCRYPTEDTITLE"),
            date=required("ZDATE"),
            duration=required("ZDURATION"),
            path=required("ZPATH"),
            identifier=identifier,
            deletion_flags=tuple(by_upper[name] for name in flag_names if name in by_upper),
            deletion_dates=tuple(by_upper[name] for name in date_names if name in by_upper),
        )

    def _select_sql(self, where: str = "", order: bool = False, duplicate_count: bool = False) -> str:
        s = self.schema
        fields = [
            _quote_identifier(s.identifier) + " AS __id",
            _quote_identifier(s.title) + " AS __title",
            _quote_identifier(s.date) + " AS __date",
            _quote_identifier(s.duration) + " AS __duration",
            _quote_identifier(s.path) + " AS __path",
        ]
        for index, column in enumerate(s.deletion_flags):
            fields.append(_quote_identifier(column) + f" AS __delete_flag_{index}")
        for index, column in enumerate(s.deletion_dates):
            fields.append(_quote_identifier(column) + f" AS __delete_date_{index}")
        if duplicate_count:
            fields.append(
                "COUNT(*) OVER (PARTITION BY "
                + _quote_identifier(s.identifier)
                + ") AS __duplicate_count"
            )
        query = "SELECT " + ", ".join(fields) + " FROM " + _quote_identifier(s.table)
        if where:
            query += " WHERE " + where
        if order:
            query += (
                " ORDER BY "
                + _quote_identifier(s.date)
                + " DESC, "
                + _quote_identifier(s.identifier)
                + " DESC, "
                + _quote_identifier(s.path)
                + " DESC"
            )
        return query

    def _filtered_query(
        self,
        search: Optional[str],
        after: Optional[str],
        before: Optional[str],
    ) -> tuple[str, list[Any]]:
        if after is not None:
            after_value = date_to_core_data(after)
        else:
            after_value = None
        if before is not None:
            before_value = date_to_core_data(before)
        else:
            before_value = None
        if after_value is not None and before_value is not None and after_value > before_value:
            raise InvalidInputError()

        s = self.schema
        clauses: list[str] = []
        params: list[Any] = []
        if search is not None:
            if "\x00" in search:
                raise InvalidInputError()
            # Deliberately do not escape % or _: they are documented SQLite LIKE
            # wildcards in this breaking interface.
            clauses.append(_quote_identifier(s.title) + " LIKE ?")
            params.append("%" + search + "%")
        if after_value is not None:
            clauses.append(_quote_identifier(s.date) + " >= ?")
            params.append(after_value)
        if before_value is not None:
            clauses.append(_quote_identifier(s.date) + " < ?")
            params.append(before_value)
        return " AND ".join(clauses), params

    def _row_values(self, row: sqlite3.Row | tuple[Any, ...], duplicate_count: bool = False) -> dict[str, Any]:
        # The connection intentionally does not rely on row_factory, keeping
        # this usable with synthetic sqlite connections in tests.
        names = ["__id", "__title", "__date", "__duration", "__path"]
        names.extend(f"__delete_flag_{i}" for i in range(len(self.schema.deletion_flags)))
        names.extend(f"__delete_date_{i}" for i in range(len(self.schema.deletion_dates)))
        if duplicate_count:
            names.append("__duplicate_count")
        return dict(zip(names, row))

    def list_raw(
        self,
        search: Optional[str],
        after: Optional[str],
        before: Optional[str],
        limit: int,
        offset: int,
        duplicate_count: bool = False,
    ) -> list[dict[str, Any]]:
        if offset < 0:
            raise InvalidInputError()
        where, params = self._filtered_query(search, after, before)
        if limit < 0:
            sql_limit = -1
        else:
            sql_limit = limit
        params.extend([sql_limit, offset])
        query = self._select_sql(where, order=True, duplicate_count=duplicate_count) + " LIMIT ? OFFSET ?"
        try:
            rows = self.connection.execute(query, params).fetchall()
        except sqlite3.Error:
            raise DatabaseError from None
        return [self._row_values(row, duplicate_count=duplicate_count) for row in rows]

    def exact_raw(self, recording_id: str) -> list[dict[str, Any]]:
        try:
            rows = self.connection.execute(
                self._select_sql(_quote_identifier(self.schema.identifier) + " = ?"),
                (recording_id,),
            ).fetchall()
        except sqlite3.Error:
            raise DatabaseError from None
        return [self._row_values(row) for row in rows]


def deletion_status(raw: dict[str, Any], schema: Schema) -> str:
    statuses: list[str] = []
    for index, _column in enumerate(schema.deletion_flags):
        value = raw.get(f"__delete_flag_{index}")
        if value is None:
            statuses.append("unknown")
        elif isinstance(value, bool):
            statuses.append("deleted" if value else "active")
        elif _is_finite_number(value):
            if float(value) in (0.0, 1.0):
                statuses.append("deleted" if float(value) else "active")
            else:
                statuses.append("unknown")
        elif isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"0", "false", "no", "active", "normal"}:
                statuses.append("active")
            elif normalized in {"1", "true", "yes", "deleted", "trashed"}:
                statuses.append("deleted")
            else:
                statuses.append("unknown")
        else:
            statuses.append("unknown")
    for index, _column in enumerate(schema.deletion_dates):
        value = raw.get(f"__delete_date_{index}")
        if value is None:
            statuses.append("active")
        elif _is_finite_number(value):
            statuses.append("deleted")
        else:
            statuses.append("unknown")
    if not statuses or "unknown" in statuses or len(set(statuses)) > 1:
        return "unknown"
    return statuses[0]


def normalize_row(raw: dict[str, Any], schema: Schema) -> MetadataRecord:
    identifier = raw.get("__id")
    title = raw.get("__title")
    date = raw.get("__date")
    duration = raw.get("__duration")
    path = raw.get("__path")
    if not isinstance(identifier, str) or not identifier or "\x00" in identifier:
        raise ValueError
    if not isinstance(title, str) or "\x00" in title:
        raise ValueError
    if not _is_finite_number(date):
        raise ValueError
    if not _is_finite_number(duration):
        raise ValueError
    duration_number = float(cast(float | int, duration))
    if duration_number < 0:
        raise ValueError
    if not isinstance(path, str) or not path or "\x00" in path:
        raise ValueError
    try:
        recorded_at = core_data_to_iso(date)
    except ValueError:
        raise ValueError from None
    return MetadataRecord(
        recording_id=identifier,
        title=title,
        recorded_at=recorded_at,
        duration=duration_number,
        deletion_status=deletion_status(raw, schema),
        path=path,
        raw_id=identifier,
    )


def record_output(record: MetadataRecord, transcript_status: str) -> dict[str, Any]:
    return {
        "recording_id": record.recording_id,
        "title": record.title,
        "recorded_at": record.recorded_at,
        "duration": record.duration,
        "deletion_status": record.deletion_status,
        "transcript_availability": transcript_status,
    }


def malformed_row_output(raw: dict[str, Any], transcript_status: str = "unavailable") -> dict[str, Any]:
    identifier = raw.get("__id")
    title = raw.get("__title")
    safe_id = identifier if isinstance(identifier, str) and "\x00" not in identifier else None
    safe_title = title if isinstance(title, str) and "\x00" not in title else None
    return {
        "recording_id": safe_id,
        "title": safe_title,
        "recorded_at": None,
        "duration": None,
        "deletion_status": "unknown",
        "transcript_availability": transcript_status,
        "row_status": "malformed",
        "error_code": "malformed_row",
    }


def _validate_relative_source_path(path: str) -> tuple[str, ...]:
    if not isinstance(path, str) or not path or "\x00" in path or len(path) > MAX_PATH_LENGTH:
        raise SourceUnavailableError()
    if os.path.isabs(path):
        raise SourceUnavailableError()
    # Apple stores POSIX relative paths.  Reject empty, dot, and dot-dot
    # components instead of normalizing them into a different selection.
    parts = tuple(path.split("/"))
    if not parts or len(parts) > MAX_PATH_COMPONENTS or any(part in {"", ".", ".."} for part in parts):
        raise SourceUnavailableError()
    return parts


def _macos_alias_path(path: str) -> str:
    if sys.platform != "darwin":
        return path
    for alias in ("tmp", "var"):
        prefix = "/" + alias
        if path == prefix or path.startswith(prefix + "/"):
            # These are root-owned macOS aliases.  Canonicalize only the known
            # system prefix; all remaining components are opened with
            # O_NOFOLLOW, avoiding a realpath-based security decision.
            return "/private" + path
    return path


def _open_absolute_directory(path: str) -> int:
    path = _macos_alias_path(path)
    if not os.path.isabs(path) or not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise SourceUnavailableError()
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    current: Optional[int] = None
    try:
        current = os.open(os.sep, flags)
        components = tuple(part for part in path.split(os.sep) if part)
        for component in components:
            try:
                next_fd = os.open(component, flags, dir_fd=current)
            except OSError:
                raise SourceUnavailableError from None
            old_fd = current
            current = next_fd
            try:
                os.close(old_fd)
            except OSError:
                pass
        try:
            info = os.fstat(current)
        except OSError:
            raise SourceUnavailableError from None
        if not stat_is_directory(info.st_mode):
            raise SourceUnavailableError()
        opened_fd = current
        current = None
        return opened_fd
    except VoiceMemosError:
        raise
    except OSError:
        raise SourceUnavailableError from None
    finally:
        if current is not None:
            try:
                os.close(current)
            except OSError:
                pass


def _open_recordings_root(path: str) -> int:
    return _open_absolute_directory(path)


def stat_is_directory(mode: int) -> bool:
    return (mode & 0o170000) == 0o040000


def _source_signature(info: os.stat_result) -> SourceSignature:
    return SourceSignature(
        device=info.st_dev,
        inode=info.st_ino,
        size=info.st_size,
        mtime_ns=info.st_mtime_ns,
        ctime_ns=info.st_ctime_ns,
    )


def open_source_at_root(root_fd: int, relative_path: str) -> OpenSource:
    parts = _validate_relative_source_path(relative_path)
    extension = os.path.splitext(parts[-1])[1]
    if extension.lower() != ".m4a":
        raise SourceUnavailableError()
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise SourceUnavailableError()

    current = os.dup(root_fd)
    try:
        for index, component in enumerate(parts):
            is_last = index == len(parts) - 1
            flags = os.O_RDONLY | os.O_NOFOLLOW
            if is_last:
                flags |= getattr(os, "O_NONBLOCK", 0)
            else:
                flags |= os.O_DIRECTORY
            try:
                next_fd = os.open(component, flags, dir_fd=current)
            except OSError:
                raise SourceUnavailableError from None
            os.close(current)
            current = next_fd
            info = os.fstat(current)
            if is_last:
                if not stat_is_regular(info.st_mode) or info.st_nlink != 1:
                    raise SourceUnavailableError()
                if info.st_size < 0 or info.st_size > MAX_COPY_BYTES:
                    raise SourceUnavailableError()
                return OpenSource(current, _source_signature(info), extension)
            if not stat_is_directory(info.st_mode):
                raise SourceUnavailableError()
    except BaseException:
        try:
            os.close(current)
        except OSError:
            pass
        raise
    raise SourceUnavailableError()


def _read_exact(fd: int, offset: int, length: int) -> bytes:
    try:
        value = os.pread(fd, length, offset)
    except OSError:
        raise SourceUnavailableError from None
    if len(value) != length:
        raise MediaMalformed()
    return value


class AtomWalker:
    def __init__(self, fd: int, file_size: int):
        self.fd = fd
        self.file_size = file_size
        self.atom_count = 0
        self.payloads: list[Atom] = []

    def header(self, offset: int, parent_end: int) -> Atom:
        if offset < 0 or parent_end < offset or parent_end > self.file_size:
            raise MediaMalformed()
        if parent_end - offset < 8:
            raise MediaMalformed()
        header = _read_exact(self.fd, offset, 8)
        size32 = struct.unpack(">I", header[:4])[0]
        atom_type = header[4:8]
        header_size = 8
        if size32 == 1:
            if parent_end - offset < 16:
                raise MediaMalformed()
            extended = _read_exact(self.fd, offset + 8, 8)
            size = struct.unpack(">Q", extended)[0]
            header_size = 16
        elif size32 == 0:
            size = parent_end - offset
        else:
            size = size32
        if size < header_size or size > parent_end - offset:
            raise MediaMalformed()
        end = offset + size
        if end > self.file_size or end <= offset:
            raise MediaMalformed()
        self.atom_count += 1
        if self.atom_count > MAX_ATOM_COUNT:
            raise MediaMalformed()
        return Atom(atom_type, offset, header_size, end)

    def walk_range(self, start: int, end: int, ancestry: tuple[bytes, ...] = (), depth: int = 0) -> None:
        if depth > MAX_ATOM_DEPTH or start < 0 or end < start or end > self.file_size:
            raise MediaMalformed()
        offset = start
        while offset < end:
            atom = self.header(offset, end)
            if atom.atom_type == b"tsrp" and b"trak" in ancestry and b"udta" in ancestry:
                if atom.payload_size > MAX_TRANSCRIPT_PAYLOAD:
                    raise MediaMalformed()
                self.payloads.append(atom)
            if atom.atom_type in {b"moov", b"trak", b"udta"}:
                self.walk_range(atom.payload_start, atom.end, ancestry + (atom.atom_type,), depth + 1)
            if atom.end <= offset:
                raise MediaMalformed()
            offset = atom.end
        if offset != end:
            raise MediaMalformed()

    def find_transcript_atoms(self) -> list[Atom]:
        self.walk_range(0, self.file_size)
        return self.payloads


def _parse_time_range(value: Any) -> float:
    if not isinstance(value, list) or len(value) != 2:
        raise TranscriptMalformed()
    start, length = value
    # Validate both values even though only the start is used for display.
    _valid_timestamp(start)
    if not _is_finite_number(length) or float(length) < 0:
        raise TranscriptMalformed()
    return float(start)


def _append_transcript_segment(
    segments: list[TranscriptSegment],
    timestamp: float,
    text: str,
    source_text_bytes: int,
) -> int:
    if len(segments) >= MAX_TRANSCRIPT_SEGMENTS:
        raise TranscriptMalformed()
    try:
        text_bytes = len(text.encode("utf-8"))
    except UnicodeEncodeError:
        raise TranscriptMalformed from None
    if source_text_bytes > MAX_TRANSCRIPT_SOURCE_TEXT_BYTES - text_bytes:
        raise TranscriptMalformed()
    segments.append(TranscriptSegment(timestamp, text))
    return source_text_bytes + text_bytes


def _parse_attributed_string(value: Any) -> list[TranscriptSegment]:
    segments: list[TranscriptSegment] = []
    source_text_bytes = 0
    if isinstance(value, list):
        current_time = 0.0
        for index, item in enumerate(value):
            if isinstance(item, str):
                if index + 1 < len(value):
                    following = value[index + 1]
                    if not isinstance(following, dict) or "timeRange" not in following:
                        raise TranscriptMalformed()
                    current_time = _parse_time_range(following["timeRange"])
                source_text_bytes = _append_transcript_segment(
                    segments, current_time, item, source_text_bytes
                )
            elif isinstance(item, dict):
                if "timeRange" not in item:
                    raise TranscriptMalformed()
                _parse_time_range(item["timeRange"])
            else:
                raise TranscriptMalformed()
        return segments

    if isinstance(value, dict):
        runs = value.get("runs")
        attributes = value.get("attributeTable")
        if not isinstance(runs, list) or not isinstance(attributes, list) or len(runs) % 2:
            raise TranscriptMalformed()
        attribute_times: list[float] = []
        for attribute in attributes:
            if not isinstance(attribute, dict) or "timeRange" not in attribute:
                raise TranscriptMalformed()
            attribute_times.append(_parse_time_range(attribute["timeRange"]))
        for index in range(0, len(runs), 2):
            text = runs[index]
            attribute_index = runs[index + 1]
            if not isinstance(text, str) or not isinstance(attribute_index, int) or isinstance(attribute_index, bool):
                raise TranscriptMalformed()
            if attribute_index < 0 or attribute_index >= len(attributes):
                raise TranscriptMalformed()
            timestamp = attribute_times[attribute_index]
            source_text_bytes = _append_transcript_segment(
                segments, timestamp, text, source_text_bytes
            )
        return segments

    raise TranscriptUnsupported()


def parse_transcript_json(data: bytes) -> list[TranscriptSegment]:
    try:
        text = data.decode("utf-8")
        value = json.loads(text, parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        raise TranscriptMalformed from None
    if not isinstance(value, dict) or "attributedString" not in value:
        raise TranscriptUnsupported()
    try:
        segments = _parse_attributed_string(value["attributedString"])
    except TranscriptUnsupported:
        raise
    except TranscriptMalformed:
        raise
    if not segments or not any(segment.text.strip() for segment in segments):
        raise TranscriptEmpty()
    return segments


def extract_transcript_from_fd(fd: int, file_size: Optional[int] = None) -> list[TranscriptSegment]:
    if file_size is None:
        try:
            file_size = os.fstat(fd).st_size
        except OSError:
            raise SourceUnavailableError from None
    if file_size < 0 or file_size > MAX_COPY_BYTES:
        raise SourceUnavailableError()
    walker = AtomWalker(fd, file_size)
    try:
        atoms = walker.find_transcript_atoms()
    except MediaMalformed:
        raise MalformedMediaError from None
    if not atoms:
        raise NoTranscriptError()

    malformed = False
    unsupported = False
    empty = False
    for atom in atoms:
        try:
            data = _read_exact(fd, atom.payload_start, atom.payload_size)
            if not data.strip():
                empty = True
                continue
            return parse_transcript_json(data)
        except TranscriptMalformed:
            malformed = True
        except TranscriptUnsupported:
            unsupported = True
        except TranscriptEmpty:
            empty = True
    if malformed:
        raise MalformedMediaError()
    if empty:
        raise EmptyTranscriptError()
    if unsupported:
        raise UnsupportedTranscriptError()
    raise NoTranscriptError()


def _timestamp_label(seconds: float) -> str:
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, seconds_part = divmod(remainder, 60)
    if hours:
        return f"[{hours}:{minutes:02d}:{seconds_part:02d}]"
    return f"[{minutes}:{seconds_part:02d}]"


def _needs_separator(left: str, right: str) -> bool:
    if not left or not right or left[-1].isspace() or right[0].isspace():
        return False
    if right[0] in ",.;:!?)]}" or (left[-1] in "([{" and right):
        return False
    # Keep numeric punctuation such as the two segments "3." and "14" raw.
    if left[-1] in ".,:;" and right[0].isdigit():
        return False
    return True


def format_raw_transcript(segments: Iterable[TranscriptSegment]) -> str:
    lines: list[tuple[float, str]] = []
    line_time: Optional[float] = None
    line_text = ""
    previous_time: Optional[float] = None
    source_text_bytes = 0
    segment_count = 0
    saw_text = False
    for segment in segments:
        segment_count += 1
        if segment_count > MAX_TRANSCRIPT_SEGMENTS:
            raise MalformedMediaError()
        try:
            text_bytes = len(segment.text.encode("utf-8"))
        except UnicodeEncodeError:
            raise MalformedMediaError from None
        if source_text_bytes > MAX_TRANSCRIPT_SOURCE_TEXT_BYTES - text_bytes:
            raise MalformedMediaError()
        source_text_bytes += text_bytes
        saw_text = saw_text or bool(segment.text.strip())
        if not line_text and not segment.text:
            line_time = segment.timestamp
            previous_time = segment.timestamp
            continue
        if line_time is None:
            line_time = segment.timestamp
        elif line_text and segment.timestamp - (previous_time or 0.0) >= 6.0:
            assert line_time is not None
            lines.append((line_time, line_text))
            line_time = segment.timestamp
            line_text = ""
        if line_text and _needs_separator(line_text, segment.text):
            line_text += " "
        line_text += segment.text
        previous_time = segment.timestamp
    if not saw_text:
        raise EmptyTranscriptError()
    if line_text:
        assert line_time is not None
        lines.append((line_time, line_text))
    result: list[str] = []
    output_bytes = 0
    previous_line_time: Optional[float] = None
    for timestamp, text in lines:
        if previous_line_time is not None and timestamp - previous_line_time >= 6.0:
            result.append("")
            output_bytes += 1
        line = _timestamp_label(timestamp) + " " + text
        try:
            line_bytes = len(line.encode("utf-8"))
        except UnicodeEncodeError:
            raise MalformedMediaError from None
        # Every output line is separated by one ASCII newline.  A blank line
        # inserted above has already accounted for its preceding newline.
        separator_bytes = 1 if result else 0
        if output_bytes > MAX_TRANSCRIPT_OUTPUT_BYTES - line_bytes - separator_bytes:
            raise MalformedMediaError()
        result.append(line)
        output_bytes += line_bytes + separator_bytes
        previous_line_time = timestamp
    return "\n".join(result)


def probe_transcript(root_fd: Optional[int], path: Any) -> str:
    if root_fd is None or not isinstance(path, str):
        return "unavailable"
    try:
        source = open_source_at_root(root_fd, path)
    except (SourceUnavailableError, OSError):
        return "unavailable"
    except Exception:
        return "malformed"
    try:
        try:
            extract_transcript_from_fd(source.fd, source.signature.size)
            return "yes"
        except NoTranscriptError:
            return "no"
        except (EmptyTranscriptError, UnsupportedTranscriptError, MalformedMediaError):
            return "malformed"
        except SourceUnavailableError:
            return "unavailable"
        except Exception:
            return "malformed"
    finally:
        try:
            os.close(source.fd)
        except OSError:
            pass


def _safe_destination_path(path: str) -> str:
    if not isinstance(path, str) or not path or "\x00" in path or len(path) > MAX_PATH_LENGTH:
        raise InvalidInputError()
    return os.path.abspath(os.path.expanduser(path))


def _destination_is_equal_or_beneath_root(destination_fd: int, root_fd: int) -> bool:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        root_info = os.fstat(root_fd)
        current = os.dup(destination_fd)
    except OSError:
        raise SourceUnavailableError from None

    try:
        for _ in range(MAX_PATH_LENGTH + 2):
            try:
                current_info = os.fstat(current)
            except OSError:
                raise SourceUnavailableError from None
            if (current_info.st_dev, current_info.st_ino) == (root_info.st_dev, root_info.st_ino):
                return True
            parent_fd: Optional[int] = None
            try:
                parent_fd = os.open("..", flags, dir_fd=current)
                parent_info = os.fstat(parent_fd)
            except OSError:
                if parent_fd is not None:
                    try:
                        os.close(parent_fd)
                    except OSError:
                        pass
                raise SourceUnavailableError from None
            if (parent_info.st_dev, parent_info.st_ino) == (current_info.st_dev, current_info.st_ino):
                return False
            old_fd = current
            current = parent_fd
            try:
                os.close(old_fd)
            except OSError:
                pass
        raise SourceUnavailableError()
    finally:
        try:
            os.close(current)
        except OSError:
            pass


def open_destination(path: str, root_path: str, root_fd: int) -> tuple[int, str]:
    del root_path
    destination = _safe_destination_path(path)
    fd = _open_absolute_directory(destination)
    try:
        if _destination_is_equal_or_beneath_root(fd, root_fd):
            raise DestinationConflictError()
        return fd, destination
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        raise


def _safe_output_stem(recording_id: str) -> str:
    if recording_id and len(recording_id) <= 128 and all(
        character.isascii() and (character.isalnum() or character in "._-") for character in recording_id
    ) and recording_id not in {".", ".."} and not recording_id.startswith("recording-"):
        return recording_id
    encoded = base64.urlsafe_b64encode(recording_id.encode("utf-8")).decode("ascii").rstrip("=")
    if len(encoded) <= 180:
        return "recording-" + encoded
    return "recording-" + hashlib.sha256(recording_id.encode("utf-8")).hexdigest()


def _temp_file(destination_fd: int) -> tuple[int, str, tuple[int, int]]:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    for _ in range(32):
        name = ".voice-memos-" + secrets.token_hex(12) + ".tmp"
        try:
            fd = os.open(name, flags, 0o600, dir_fd=destination_fd)
            info = os.fstat(fd)
            return fd, name, (info.st_dev, info.st_ino)
        except FileExistsError:
            continue
        except OSError:
            raise SourceUnavailableError from None
    raise SourceUnavailableError()


def _cleanup_temp(destination_fd: int, name: Optional[str], identity: Optional[tuple[int, int]]) -> None:
    if not name or identity is None:
        return
    try:
        info = os.stat(name, dir_fd=destination_fd, follow_symlinks=False)
        if (info.st_dev, info.st_ino) == identity:
            os.unlink(name, dir_fd=destination_fd)
    except OSError:
        pass


def _copy_bounded(source_fd: int, destination_fd: int, expected_size: int) -> int:
    if expected_size < 0 or expected_size > MAX_COPY_BYTES:
        raise SourceUnavailableError()
    remaining = expected_size
    copied = 0
    while remaining:
        try:
            data = os.read(source_fd, min(COPY_CHUNK_SIZE, remaining))
        except OSError:
            raise SourceUnavailableError from None
        if not data:
            raise SourceChangedError()
        position = 0
        while position < len(data):
            try:
                written = os.write(destination_fd, data[position:])
            except OSError:
                raise SourceUnavailableError from None
            if written <= 0:
                raise SourceUnavailableError()
            position += written
        copied += len(data)
        remaining -= len(data)
    return copied


def _same_record(before: MetadataRecord, after_raw: list[dict[str, Any]], schema: Schema) -> bool:
    if len(after_raw) != 1:
        return False
    if after_raw[0].get("__path") != before.path or after_raw[0].get("__id") != before.raw_id:
        return False
    try:
        after = normalize_row(after_raw[0], schema)
    except ValueError:
        return False
    return after.deletion_status == "active"


def export_record(store: MetadataStore, recording_id: str, root_path: str, root_fd: int, destination_path: str) -> dict[str, Any]:
    rows = store.exact_raw(recording_id)
    if len(rows) != 1:
        raise SourceUnavailableError()
    try:
        record = normalize_row(rows[0], store.schema)
    except ValueError:
        raise SourceUnavailableError from None
    if record.deletion_status != "active":
        raise SourceUnavailableError()

    destination_fd, destination = open_destination(destination_path, root_path, root_fd)
    source: Optional[OpenSource] = None
    temp_fd: Optional[int] = None
    temp_name: Optional[str] = None
    temp_identity: Optional[tuple[int, int]] = None
    output_name: Optional[str] = None
    succeeded = False
    try:
        source = open_source_at_root(root_fd, record.path)
        temp_fd, temp_name, temp_identity = _temp_file(destination_fd)
        copied = _copy_bounded(source.fd, temp_fd, source.signature.size)
        try:
            os.fsync(temp_fd)
        except OSError:
            raise SourceUnavailableError from None
        current_info = os.fstat(source.fd)
        if _source_signature(current_info) != source.signature:
            raise SourceChangedError()
        if not _same_record(record, store.exact_raw(recording_id), store.schema):
            raise SourceChangedError()
        extension = source.extension
        output_name = _safe_output_stem(record.recording_id) + extension
        try:
            os.link(
                temp_name,
                output_name,
                src_dir_fd=destination_fd,
                dst_dir_fd=destination_fd,
                follow_symlinks=False,
            )
        except FileExistsError:
            raise DestinationConflictError from None
        except OSError:
            raise SourceUnavailableError from None
        try:
            os.fsync(destination_fd)
        except OSError:
            raise SourceUnavailableError from None
        _cleanup_temp(destination_fd, temp_name, temp_identity)
        temp_name = None
        succeeded = True
        return {
            "recording_id": record.recording_id,
            "destination": os.path.join(destination, output_name),
            "bytes": copied,
        }
    finally:
        if temp_fd is not None:
            try:
                os.close(temp_fd)
            except OSError:
                pass
        if not succeeded:
            _cleanup_temp(destination_fd, output_name, temp_identity)
            _cleanup_temp(destination_fd, temp_name, temp_identity)
        if source is not None:
            try:
                os.close(source.fd)
            except OSError:
                pass
        try:
            os.close(destination_fd)
        except OSError:
            pass


def _close_quietly(fd: Optional[int]) -> None:
    if fd is not None:
        try:
            os.close(fd)
        except OSError:
            pass


def list_records(store: MetadataStore, root_fd: Optional[int], search: Optional[str], after: Optional[str], before: Optional[str], limit: int, offset: int) -> dict[str, Any]:
    # The window count is computed over the filtered rows before LIMIT/OFFSET,
    # so a duplicate whose twin is on another page is still marked here.
    raw_rows = store.list_raw(search, after, before, limit, offset, duplicate_count=True)
    result: list[dict[str, Any]] = []
    normalized: list[Optional[MetadataRecord]] = []
    for raw in raw_rows:
        try:
            record = normalize_row(raw, store.schema)
        except ValueError:
            normalized.append(None)
            result.append(malformed_row_output(raw))
            continue
        normalized.append(record)
        result.append(record_output(record, probe_transcript(root_fd, record.path)))

    partial = False
    for index, record in enumerate(normalized):
        duplicate_count = raw_rows[index].get("__duplicate_count")
        if record is not None and isinstance(duplicate_count, int) and duplicate_count > 1:
            result[index]["row_status"] = "malformed"
            result[index]["error_code"] = "duplicate_recording_id"
            partial = True
        elif result[index].get("row_status") == "malformed":
            partial = True
    payload: dict[str, Any] = {"recordings": result, "count": len(result), "complete": not partial}
    if partial:
        payload["warnings"] = ["malformed_row"]
    return payload


def _merge_option(namespace: argparse.Namespace, name: str) -> Optional[str]:
    command_name = "command_" + name
    value = getattr(namespace, command_name, None)
    if value is not None:
        return value
    return getattr(namespace, "global_" + name, None)


def build_parser() -> ArgumentParser:
    parser = ArgumentParser(
        prog="voice-memos",
        description="List, export, or extract Apple Voice Memos by opaque recording ID.",
        allow_abbrev=False,
    )
    parser.add_argument("--database", dest="global_database", help="advanced/testing: SQLite database override")
    parser.add_argument("--recordings-root", dest="global_recordings_root", help="advanced/testing: recordings root override")
    parser.add_argument("--json", action="store_true", help="JSON output (always enabled; retained for explicit scripts)")
    subparsers = parser.add_subparsers(dest="command", required=True)

    listing = subparsers.add_parser("list", help="list recordings", allow_abbrev=False)
    listing.add_argument("--database", dest="command_database", default=argparse.SUPPRESS, help="advanced/testing: SQLite database override")
    listing.add_argument("--recordings-root", dest="command_recordings_root", default=argparse.SUPPRESS, help="advanced/testing: recordings root override")
    listing.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="JSON output (always enabled)")
    listing.add_argument("--limit", type=int, default=10, help="maximum rows; negative means unlimited (default: 10)")
    listing.add_argument("--offset", type=int, default=0, help="rows to skip; must not be negative")
    listing.add_argument("--search", help="title LIKE filter; % and _ remain SQLite wildcards")
    listing.add_argument("--after", help="inclusive UTC date at midnight (YYYY-MM-DD)")
    listing.add_argument("--before", help="exclusive UTC date at midnight (YYYY-MM-DD)")

    exporting = subparsers.add_parser("export", help="copy one recording by ID", allow_abbrev=False)
    exporting.add_argument("--database", dest="command_database", default=argparse.SUPPRESS, help="advanced/testing: SQLite database override")
    exporting.add_argument("--recordings-root", dest="command_recordings_root", default=argparse.SUPPRESS, help="advanced/testing: recordings root override")
    exporting.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="JSON output (always enabled)")
    exporting.add_argument("--id", required=True, help="opaque recording ID")
    exporting.add_argument("--to", required=True, help="existing destination directory outside recordings root")

    transcript = subparsers.add_parser("transcript", help="extract raw transcript by ID", allow_abbrev=False)
    transcript.add_argument("--database", dest="command_database", default=argparse.SUPPRESS, help="advanced/testing: SQLite database override")
    transcript.add_argument("--recordings-root", dest="command_recordings_root", default=argparse.SUPPRESS, help="advanced/testing: recordings root override")
    transcript.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="JSON output (always enabled)")
    transcript.add_argument("--id", required=True, help="opaque recording ID")
    return parser


def _validate_namespace(namespace: argparse.Namespace) -> None:
    if namespace.command == "list":
        if namespace.offset < 0 or (namespace.search is not None and "\x00" in namespace.search):
            raise InvalidInputError()
        after_value = date_to_core_data(namespace.after) if namespace.after is not None else None
        before_value = date_to_core_data(namespace.before) if namespace.before is not None else None
        if after_value is not None and before_value is not None and after_value > before_value:
            raise InvalidInputError()
        return
    if not isinstance(namespace.id, str) or not namespace.id or "\x00" in namespace.id:
        raise InvalidInputError()
    if namespace.command == "export":
        _safe_destination_path(namespace.to)


def _run(namespace: argparse.Namespace) -> dict[str, Any]:
    _validate_namespace(namespace)
    database, root = resolve_paths(_merge_option(namespace, "database"), _merge_option(namespace, "recordings_root"))
    connection = open_database(database)
    root_fd: Optional[int] = None
    try:
        store = MetadataStore(connection)
        try:
            root_fd = _open_recordings_root(root)
        except SourceUnavailableError:
            # A missing or inaccessible media root is not a reason to lose valid
            # metadata rows from list.  Export/transcript fail when they need it.
            if namespace.command == "list":
                root_fd = None
            else:
                raise

        if namespace.command == "list":
            return list_records(store, root_fd, namespace.search, namespace.after, namespace.before, namespace.limit, namespace.offset)

        recording_id = namespace.id
        if not isinstance(recording_id, str) or not recording_id or "\x00" in recording_id:
            raise InvalidInputError()
        rows = store.exact_raw(recording_id)
        if len(rows) != 1:
            raise SourceUnavailableError()
        try:
            record = normalize_row(rows[0], store.schema)
        except ValueError:
            raise SourceUnavailableError from None
        if root_fd is None:
            raise SourceUnavailableError()
        if namespace.command == "export":
            return export_record(store, recording_id, root, root_fd, namespace.to)

        # transcript: deliberately ID-only, with the same descriptor-relative
        # source resolution as export.  Unlike export, an unknown deletion state
        # is still inspectable; the export operation is the authorization gate.
        source = open_source_at_root(root_fd, record.path)
        try:
            try:
                segments = extract_transcript_from_fd(source.fd, source.signature.size)
            except NoTranscriptError:
                raise NoTranscriptError(
                    {
                        "recording_id": record.recording_id,
                        "title": record.title,
                        "recorded_at": record.recorded_at,
                        "duration": record.duration,
                        "path": os.path.join(root, *record.path.split("/")),
                        "reason": "no embedded Apple transcript",
                    }
                ) from None
            except VoiceMemosError:
                raise
            return {
                "recording_id": record.recording_id,
                "title": record.title,
                "recorded_at": record.recorded_at,
                "duration": record.duration,
                "deletion_status": record.deletion_status,
                "transcript": format_raw_transcript(segments),
                "segments": [
                    {"timestamp": segment.timestamp, "text": segment.text}
                    for segment in segments
                ],
            }
        finally:
            _close_quietly(source.fd)
    finally:
        _close_quietly(root_fd)
        try:
            connection.close()
        except sqlite3.Error:
            pass


def emit_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    try:
        namespace = parser.parse_args(argv)
        payload = _run(namespace)
    except VoiceMemosError as error:
        error_object: dict[str, Any] = {"code": error.code}
        if isinstance(error, NoTranscriptError) and error.handoff is not None:
            error_object["handoff"] = error.handoff
        emit_json({"error": error_object})
        print("voice-memos: " + error.code, file=sys.stderr)
        return error.exit_status
    except (sqlite3.Error, OSError, ValueError, TypeError):
        # Do not surface exception text: it can contain paths, SQL, titles, or
        # data supplied by a recording.
        emit_json({"error": {"code": "database_error"}})
        print("voice-memos: database_error", file=sys.stderr)
        return EXIT_DATABASE
    except Exception:
        emit_json({"error": {"code": "database_error"}})
        print("voice-memos: database_error", file=sys.stderr)
        return EXIT_DATABASE
    emit_json(payload)
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
