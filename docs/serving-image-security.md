# Serving image dependency remediation

The original ARM64 serving and initializer scans recorded five fixable HIGH findings.
The remediation updates the dependency chain; it does not exclude CVEs or bypass
`pip check`. Live reports and scans are kept in
[acceptance evidence](evidence/live-2026-10-06/serving-arm64/README.md).

The serving image uses MLflow 3.16.1 with cryptography 50.0.2, FastAPI 0.142.2 and
Starlette 1.7.0. Model serialization dependencies (scikit-learn, NumPy and cloudpickle)
remain aligned with training. The MLServer runtime plugin remains 1.7.1.

MLServer 1.7.1 declares FastAPI `<0.116.0`, which cannot resolve the fixed Starlette
versions. The explicit platform fork `mlserver==1.7.1+mlp.1` changes dependency metadata
to FastAPI `>=0.142.2,<1` and Starlette `>=1.3.1,<2`. It does not change inference source.
[docker/serving/patch_mlserver.py](../docker/serving/patch_mlserver.py) downloads the
exact upstream wheel, verifies SHA256
`d2ac7502915bb5311343a878aa1fb614f9b0f2436ee550a6137c1872a4dca193`, checks the expected
metadata, refuses signed/unexpected inputs, and regenerates the versioned dist-info and
RECORD hashes deterministically. The fork version appears explicitly in the lock and
SBOM. Its compatibility with the newer HTTP stack requires actual inference acceptance;
changing a dependency declaration alone does not prove compatibility. Keep this patch
under review on each upstream upgrade and remove it when a supported release permits
the fixed dependency chain.

The initializer uses the upstream standalone `kserve-storage==0.21.0` package and its
`Storage.download` entrypoint. It does not require the full KServe Python serving SDK,
its old protobuf cap or a native psutil/compiler build. The controller remains KServe
0.15.0; this is an independent storage-library update. The classic S3 URI, AWS credential,
endpoint, TLS and revision-specific account contract is unchanged and must be verified
against the real private artifact path. Other storage providers have no new live proof.

The supported `scripts/lock.sh serving` command generates the verified compatibility
wheel in its temporary directory before resolution; `scripts/lock.sh storage-initializer`
resolves the standalone SDK. Both paths reproduce the recorded package pins.

For an explicit ARM64 regeneration with uv 0.12.23 and Python 3.12:

```bash
python docker/serving/patch_mlserver.py --out /tmp/mlp-security-wheels
uv pip compile constraints/serving.in --constraint constraints/serving.txt \
  --find-links /tmp/mlp-security-wheels --python-version 3.12 \
  --python-platform aarch64-unknown-linux-gnu --no-annotate --no-header \
  --output-file constraints/serving.txt
uv pip compile constraints/storage-initializer.in --constraint constraints/storage-initializer.txt \
  --python-version 3.12 --python-platform aarch64-unknown-linux-gnu \
  --no-annotate --no-header --output-file constraints/storage-initializer.txt
```

The existing lock used as a constraint reproduces the recorded versions. For deliberate
upgrades, remove that constraint, resolve again and repeat build/runtime/scan gates.
AMD64 resolution with these constraints matched the ARM64 locks; an AMD64 image runtime
is a separate gate. Both Dockerfiles install the resolved locks and run `pip check`.

For each new artifact, retain its immutable digest, source hashes, SPDX SBOM and Trivy
vulnerability/secret scan; then test model loading and both MLflow `/invocations` and
native V2 inference, plus cluster S3 initialization and gateway/canary behavior. Keep the
final clean release artifact rerun separate from working-tree acceptance image results.

The [recorded new-image lifecycle](evidence/live-2026-10-06/serving-arm64/security-remediation/cpu-lifecycle.json)
passed all seven phases in 641 seconds. This rerun includes the healthy canary gate;
adversarial rollback is covered by the earlier separate live evidence.
