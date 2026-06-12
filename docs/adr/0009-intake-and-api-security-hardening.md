# Intake and API security hardening

The intake scanner and the Phase 1 HTTP API enforce four standing security
constraints. (1) The scanner treats `raw/inbox/` as untrusted: it rejects symbolic
links and any path whose real location resolves outside the inbox, so a link placed
under `raw/inbox/` can never cause hashing or manifesting of files elsewhere on the
filesystem. Such paths are skipped and reported as `skipped_symlink` warnings rather
than ingested. (2) Manifest merges are gated on checksum identity: because manifests
are the authoritative source listing (ADR-0008), an existing manifest whose stored
`sha256` disagrees with the freshly scanned content is treated as an error and left
untouched, never silently merged. (3) The API never returns absolute filesystem
paths — the `Source` response model omits `raw_path` and exposes only
repository-relative paths — and all read endpoints are schema-enforced via response
models so manifest/job drift surfaces at the boundary. (4) The service uses the
reserved API port 18000 (ADR-0005) and is published only to host loopback;
container deployments bind `0.0.0.0` internally but publish to `127.0.0.1` only, so
the API is never exposed on LAN or public interfaces (Build Spec: local-only, no
public exposure).

Consequences: intake favors safety over completeness — a legitimate symlinked inbox
file will be skipped, not followed, which is the intended trade for the immutable-raw
and untrusted-data guarantees. Schema-enforced responses mean new manifest fields
must be added to the response models to appear over the API; this is deliberate, so
that on-disk schema changes are a conscious API decision rather than an accidental
leak. These are durable contracts that downstream phases (extraction, query, review)
may rely on.
