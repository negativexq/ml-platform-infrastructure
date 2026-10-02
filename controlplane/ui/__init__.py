"""The web UI: static files only, served by the control plane at /ui.

It talks to the Platform API on the same origin and nothing else (enforced by a
Content-Security-Policy and by tests).
"""

from pathlib import Path

STATIC_DIR = Path(__file__).parent / "static"

# connect-src 'self': a page script cannot reach MLflow, Argo, Kubernetes or any other
# host even if someone wrote the code to try.
CONTENT_SECURITY_POLICY = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)
