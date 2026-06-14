# Raw-file mutation after intake is a hard validation failure

Raw sources are immutable (ADR-0002) and `source_id` is the raw content hash
(`src_<sha256[:16]>`). If a raw file's bytes change after intake while its path stays
the same, the manifest still references it under the old `sha256`, and the entire derived
chain — normalized text, wiki pages, and future indexes and citations — now describes
content that no longer exists at that location. This is silent corruption of the
evidence base, so it is detected loudly.

A dedicated `scripts/validate_raw_integrity.py` check (auto-discovered by
`scripts/validate_all.py`) compares each manifest's recorded checksum to the file on
disk and **hard-fails** (non-zero exit) on a confirmed mismatch. To stay affordable at
the target scale (≈600 files up to 50 MB), it does not re-hash everything: it pre-filters
using the size and `modified_at` already recorded in the manifest, and only re-hashes a
file whose size or mtime has drifted. A confirmed `sha256` mismatch is the failure; a
referenced file that is simply missing is reported but is out of this check's scope
(deletion/retention is a separate concern).

This complements, rather than replaces, the point-of-use guards already in place:
extraction (and wiki generation, transitively) refuse to derive from a source whose bytes
no longer match its manifest (`checksum_mismatch`). The validator adds standing,
suite-level detection so drift surfaces during lint, not only when something happens to
touch the source. A blanket re-hash of every file on every operation was rejected: it
does not scale and would slow or block unrelated reads.

Consequences: an accidental or unexpected mutation of a catalogued raw file is caught as
a loud, deterministic failure with a clear remediation (re-scan to mint the correct
content-keyed manifest, then re-derive), upholding the immutability invariant without
paying a full re-hash on every run. The cost is that integrity detection depends on the
recorded size/mtime pre-filter; a mutation that perfectly preserves both size and mtime
would evade the cheap path, an accepted, low-risk trade for scalability.
