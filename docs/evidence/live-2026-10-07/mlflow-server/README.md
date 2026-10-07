# MLflow tracking/registry server profile

Native ARM64 build and all seven Docker fixture checks passed with PostgreSQL 16 and private MinIO: pip check, database migration, server readiness, upstream UI, experiment/run logging and search, logged-model lookup/registration/aliases/control-plane discovery, and S3-proxied artifact upload/list/download. The profile uses mlflow-skinny with explicit server dependencies, the original upstream UI and cryptography 50.0.2. NumPy/pandas remain because MLflow server imports require them.

Trivy reported zero fixable HIGH/CRITICAL findings. The image contains 82 Python distributions and its SPDX SBOM contains 215 packages. The measured uncompressed Trivy size is 540,818,944 bytes. No cluster rollout was performed. Raw reports remain in the external archive; SHA256SUMS covers the files prefixed mlflow-pre-push.
