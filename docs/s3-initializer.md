# Go S3 storage initializer

`docker/storage-initializer/Dockerfile` builds `services/storage-initializer-go` from
the repository root. The final scratch image contains a static binary and public
CA certificates, runs as UID/GID 1000, and accepts the existing KServe arguments:

```text
/storage-initializer s3://bucket/key-or-prefix /mnt/models
```

The bootstrap `ClusterStorageContainer` URI matcher and digest-pinning requirements
are unchanged. Other storage providers still use their separate upstream initializer.
The AWS SDK default credential chain supports environment credentials, session tokens
and web identity; no credential is embedded in the image.

## Download contract

Exact objects take precedence; prefix downloads preserve relative paths. Directory
markers are skipped, but empty files are preserved. Pagination is supported. Missing
objects, invalid names, path traversal, duplicate paths, and file/directory collisions
fail initialization. Listed ETags are sent as `If-Match` so replacements between listing
and download fail. Received lengths must match the listing. SDK-supported response
checksums are validated when supplied; objects without checksums only receive length
and ETag consistency checks. ETags are not treated as MD5 hashes.

The destination must be an empty writable real directory. Downloads stream to a hidden
staging directory on the same volume, with bounded workers. On failure or cancellation,
staging is removed. Once all downloads validate, complete top-level files/directories
are renamed into place. A rename failure rolls back entries already published. A
mounted volume cannot itself be atomically renamed: this is atomic per top-level entry,
with init-container completion gating access by the serving container. It is not a
transactional publish for independent readers of a shared volume. A SIGKILL can leave
staging behind; the next run fails closed on a nonempty volume. Use a fresh volume or
have the operator clear it before retrying.

## Environment

| Variable | Default / behavior |
| --- | --- |
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN` | AWS default credential chain |
| `AWS_REGION`, `AWS_DEFAULT_REGION` | SDK resolution; `us-east-1` if unset |
| `AWS_ENDPOINT_URL` | Custom HTTP/HTTPS S3/MinIO endpoint |
| `AWS_ENDPOINT_URL_S3` | Service-specific endpoint takes precedence |
| `awsAnonymousCredential` | `false` |
| `S3_USER_VIRTUAL_BUCKET` | `false` (path-style); `true` for virtual hosted buckets |
| `S3_USE_ACCELERATE` | `false` |
| `S3_VERIFY_SSL` | `true`; explicit `0`/`false` preserves KServe override |
| `AWS_CA_BUNDLE` | Custom CA PEM; otherwise public CA certificates |
| `CA_BUNDLE_CONFIGMAP_NAME`, `CA_BUNDLE_VOLUME_MOUNT_POINT` | Fallback `cabundle.crt` in mounted custom CA directory |
| `S3_MAX_FILE_CONCURRENCY` | `4`, allowed range 1–64 |
| `S3_MAX_OBJECTS` | `10000` |
| `S3_MAX_DOWNLOAD_BYTES` | `21474836480` (20 GiB across all objects) |
| `S3_CONNECT_TIMEOUT` | `15` seconds |
| `S3_READ_TIMEOUT` | `30` seconds of socket read inactivity |
| `S3_MAX_ATTEMPTS` | `3` total SDK attempts per request |
| `S3_DOWNLOAD_TIMEOUT` | `1800` seconds overall |

KServe still converts project Secret annotations into AWS endpoint/region environment
variables. Credential Secrets and revision-specific serving accounts are unchanged.
Unlike the Python library, this implementation does not retry a partially read object
body: it fails the init container. Request failures before streaming use SDK retries.

## Validation

Run `make storage-initializer-go-test`. Tests exercise real SDK HTTP requests with
SigV4/session-token credentials, path-style endpoints, pagination, ETag preconditions
and valid/invalid CRC32 responses. Other tests cover exact objects, nested/empty files,
limits, bounded concurrency, traversal/collisions, short/oversized bodies, cancellation
and cleanup. Tests also cover custom CA bundles and anonymous TLS requests. The manually triggered
CI workflow builds and scans the image and emits an SPDX SBOM.

Local ARM64 image measurement, SBOM, scan and disposable MinIO acceptance are in
[the recorded evidence](evidence/live-2026-10-07/storage-initializer-go/README.md).
Image size is recorded separately from runtime acceptance. On 2026-10-07 the local
acceptance cluster deployed the digest-pinned Go image and exercised fresh private
MinIO model loads, CPU inference and canary rollout. The lifecycle report and actual
KServe init-container image/exit statuses are archived outside the repository under
`ml-platform-infra-artifacts/2026-10-07/image-slimming/local-release/`.
