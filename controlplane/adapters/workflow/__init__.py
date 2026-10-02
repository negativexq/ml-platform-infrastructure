"""Workflow adapters. Implements WorkflowProvider; the in-memory fake lives in adapters/fakes.py."""

from controlplane.adapters.workflow.argo import ArgoWorkflowProvider

__all__ = ["ArgoWorkflowProvider"]
