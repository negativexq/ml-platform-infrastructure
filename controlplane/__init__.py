"""Control plane: the platform's own domain, API and lifecycle state.

PostgreSQL owns lifecycle state. MLflow, Argo and KServe are reached only
through the provider ports in `controlplane.application.providers` and are
never a source of truth.
"""
