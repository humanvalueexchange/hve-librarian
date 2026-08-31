# Proton Telegram Indexed Intake Victory

**Date:** August 31, 2026
**Owner:** Luna, HVE CTO / head architect
**Status:** Implemented, live-tested, and ready for continued use

## Executive result

The complete Telegram-to-library workflow now works end to end:

1. Hans sends a Proton public-share URL through the Telegram channel.
2. HVE-Librarian receives the message and queues one durable Proton job.
3. The local visible Chromium worker performs the required two-click Proton
   download sequence.
4. The downloaded original is validated by filename, MIME type, signature,
   size, and SHA-256.
5. The HVE knowledge pipeline extracts text, chunks the document, and indexes
   it.
6. The worker sends a separate Telegram completion notice only after the
   manifest reaches `indexed`.
7. Notification state is persisted so delivery can be retried without
   duplicating a notice already recorded as sent.

This closes the asynchronous observability gap: a worker completion that
occurs after the Telegram agent turn now produces a durable, user-facing
completion notice.

## Live acceptance evidence

Fresh Telegram acceptance job:

- **Job ID:** `proton-e7d3450c7b4a5c92`
- **Job status:** `completed`
- **Notification status:** `sent`
- **File:** `Reality-Transurfing-Steps-I-V-Vabim-Zeland.pdf`
- **Size:** `3,385,002` bytes
- **Pages:** `688`
- **Chunks:** `682`
- **SHA-256:** `fe2b42992a7714df3c058809cce5d9f5134f9b14616565c90ef6963e738bdfe9`
- **Manifest:** `/hve-library/state/manifests/fe2b42992a7714df.json`
- **Indexed original:** `/hve-library/raw/pdfs/Reality-Transurfing-Steps-I-V-Vabim-Zeland.pdf`

The indexed original checksum matches the persisted job and manifest records.
The completion notice was sent through the configured HVE-Librarian Telegram
home channel after indexing completed.

## Runtime implementation

The runtime implementation is committed in the separate
`humanvalueexchange/hanshermesagent` repository, which owns the Hermes
collector and worker code:

- Proton jobs carry a Telegram notification target.
- The worker scans completed jobs for indexed manifests.
- Notices include status, filename, type, size, SHA-256, page count, chunk
  count, library path, and manifest path.
- Notification attempts, retry timing, errors, and sent state are persisted.
- The worker uses the profile-scoped Hermes sender rather than direct Telegram
  credentials.
- Notices are emitted only after indexing, not merely after download.

## Runtime state at acceptance

- `hermes-gateway-hve-librarian.service`: active
- Telegram platform: connected
- `hermes-proton-worker.service`: active
- Visible Chromium CDP endpoint: reachable on `127.0.0.1:9222`
- Gateway session store: healthy

The gateway and Proton worker were reloaded after the implementation changes.

## Validation

Focused regression tests passed:

`python -m unittest tests.test_proton_file_collector tests.test_link_collector -q`

The live acceptance pass verified the browser download, indexed manifest,
persisted checksum, and Telegram completion notification. Historical failed
jobs remain preserved as evidence and are not current behavior.

## Authoritative success condition

Do not report Proton intake success from a queued or downloaded state alone.
The acceptance condition is:

`status=completed` + indexed manifest + persisted original + checksum +
`notification_status=sent`.

Direct CLI smoke tests prove worker capability but do not prove Telegram
end-to-end delivery. A Telegram-originated job is required for complete
workflow acceptance.

## Scope boundary

The PDF Telegram path is proven. MP3/MP4 production intake and broader
production cutover require separate supervised acceptance tests and explicit
approval.
