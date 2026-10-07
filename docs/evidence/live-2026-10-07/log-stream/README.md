# Go log streaming and readable Runs logs

Verified in the local acceptance cluster, including the user-reported `a061e088` pipeline run and its `train` step. Kubernetes HTTP bytes are decoded as UTF-8. The Runs detail log panel presents timestamp, severity, source and short messages; three Git metadata warnings are grouped with full diagnostics retained under Details. Argo INFO exits containing `error="<nil>"` no longer count as errors. Raw output remains available through copy/download.

The image release gate, 22 UI unit tests and eight live streaming checks passed. Browser checks exercised both job and pipeline Go SSE streams and navigation cancellation. Desktop/mobile screenshots and raw scan reports are in the external archive. The training Dockerfile now sets `GIT_PYTHON_REFRESH=quiet`; the native ARM64 image was rebuilt and passed pip check, CLI and MLflow/sklearn import checks without a Git executable or missing-Git warning. Existing job definitions were not substituted. Cloudpickle serialization remains unchanged.

Cleanup removed temporary Docker images/build cache, inactive node images and obsolete registry manifests. Current workload images and deployment/hook references were retained. One MC reference in the existing Helm hooks was already absent before cleanup; no MC image was deleted by this run.
