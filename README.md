# Apple Voice Memos Agent Skill

List, inspect, export, and read embedded Apple transcripts from Voice Memos synced to macOS through iCloud.

## Install

Install the `apple-voice-memos` directory into an agent that supports the [Agent Skills](https://agentskills.io/home) format. You can use a local checkout or extract a release archive into your agent's skills directory.

To install it from GitHub with `npx skills`:

```bash
npx skills add mxl/apple-voice-memos --skill apple-voice-memos
```

The skill is self-contained and does not depend on a particular hosting repository.

### Requirements

- macOS with Voice Memos iCloud sync enabled
- Python 3.10 or newer
- Permission for the agent host to read the Voice Memos group container

Python runtime dependencies are limited to the standard library.

The automated suite validates synthetic schemas and media on macOS and Linux. Compatibility with a specific live macOS Voice Memos schema remains unverified until the consented native checks in `TODO.md` are completed.

## Supported operations

The skill uses one JSON CLI:

```bash
python3 apple-voice-memos/scripts/voice-memos list --limit 20
python3 apple-voice-memos/scripts/voice-memos list --search "meeting" --after 2026-09-01
python3 apple-voice-memos/scripts/voice-memos export --id RECORDING_ID --to /existing/directory
python3 apple-voice-memos/scripts/voice-memos transcript --id RECORDING_ID
```

### List and search

`list` reads `CloudRecordings.db` in SQLite read-only/query-only mode and returns:

- opaque `recording_id`
- title and UTC recording time
- numeric duration
- deletion status
- embedded transcript availability

Source paths are not returned. `%` and `_` remain SQLite `LIKE` wildcards. `--after` is inclusive UTC midnight and `--before` is exclusive UTC midnight. Offsets must be nonnegative. A negative `--limit` requests every matching row and may produce large output.

Transcript detection is bounded and isolated per recording. Missing or malformed media is reported on that row instead of aborting the inventory.

### Export

`export` re-queries an exact recording ID and copies the stored `.m4a` bytes to an explicit existing directory. It confines source access to the Voice Memos recordings root, rejects traversal/symlinks/hard links/special files, copies through open descriptors, detects common source changes, and publishes without replacing an existing file.

Export does not transcode, flatten, edit, hydrate, or upload media. The result may contain embedded transcripts or other container metadata. It is not recovery of a pre-edit original and may differ from Finder/Share output. QTA and compositions are not supported.

### Embedded transcripts

`transcript` accepts a recording ID, not an arbitrary path. It reads Apple's embedded `tsrp` data with bounded atom and payload traversal, checks all relevant tracks, validates JSON/timestamps, and returns timestamped text plus the exact source segments. It does not remove filler words or repetitions.

Not every Voice Memo has an embedded transcript. This skill does not perform speech recognition or choose an ASR provider. When transcription is requested but no embedded transcript exists, it returns a validated audio-path handoff to the parent agent.

Summarization is a separate, explicit model operation. Listing, inspection, export, and raw extraction do not invoke `PROMPT.md` or a subagent.

## Security and privacy

- The source library is never modified.
- Listing/export perform no automatic network or model calls and do not change macOS permissions.
- Titles, database values, transcripts, and generated notes are treated as untrusted data, not instructions or authorization.
- Diagnostics avoid titles, transcripts, database rows, and protected source paths.
- Full Disk Access belongs to the host application and is broader than this skill. Skill metadata is not a sandbox and does not protect against a compromised host or malicious processes running as the same user.
- Summarization can disclose content to the configured model provider and host logs. Check those policies before requesting it.

Apple does not publish the Voice Memos database schema or `tsrp` format. The CLI validates required columns and fails closed when the schema or deletion state is unsupported. Native support claims should be limited to tested macOS versions and media layouts.

## Tests

Tests use generated SQLite databases and synthetic media only:

```bash
python3 -m unittest discover -s tests -v
```

They do not discover or read the user's Voice Memos library.

## Example requests

> List my most recent Voice Memos.

> Find the memo about the kitchen renovation.

> Export my latest memo to `~/Desktop`.

> Show the raw embedded transcript of yesterday's memo.

> Summarize the embedded transcript of my latest meeting memo.

## License

[0BSD](https://opensource.org/license/0bsd)
