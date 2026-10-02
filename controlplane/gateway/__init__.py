"""The inference gateway: the one public way in to a project's endpoints."""

from controlplane.gateway.app import create_gateway

__all__ = ["create_gateway"]
