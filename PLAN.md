# Implementation Status

The synthetic implementation and CI scope is complete. Native macOS compatibility and release acceptance remain unverified; see `TODO.md` for the outstanding checks.

## Supported Operations

- `list`: return recording metadata and embedded-transcript availability as JSON.
- `export`: copy stored `.m4a` bytes for an exact recording ID to an explicit existing directory.
- `transcript`: extract Apple’s embedded transcript and preserve its source segments and timestamps.
- Summarization: perform only when explicitly requested, using `PROMPT.md`.

## Security And Data Boundaries

- Open the Voice Memos database in SQLite read-only/query-only mode. Fail closed when required schema, identity, or deletion state is uncertain.
- Select media by opaque recording ID. Keep source paths out of ordinary list output.
- Confine media access to the Voice Memos recordings root. Reject traversal, links, and special files; copy through open descriptors and do not overwrite an existing destination.
- Bound media parsing and output. Treat titles, database values, transcripts, and generated notes as untrusted data.
- Listing, export, and raw extraction do not invoke a model or make network calls. Do not change macOS permissions or claim skill metadata is a sandbox.
- Export copies stored media without transcoding. Embedded metadata may remain in the copied container.

## Verification

Run the synthetic unit and CLI tests with:

```bash
python3 -m unittest discover -s tests -v
```

Tests use generated databases and media in temporary directories. They do not access a personal Voice Memos library. Automated results do not establish compatibility with a live Apple schema or media layout.

## Remaining Acceptance Checks

Before claiming support for a specific macOS environment, complete the consented native checks listed in `TODO.md`, including schema and ID mappings, access-denial behavior, cloud-only media, and stored-media export semantics. Verify the packaged skill in advertised agent hosts and validate an exact-tag release before publishing.

## Deferred Scope

Do not add recording, editing, deletion, automatic iCloud hydration, bulk export, ASR, uploads, indexing, a daemon, GUI, or a native Full Disk Access broker without a concrete user requirement. Evaluate QTA and composition support only after native fixtures and semantics are available.
