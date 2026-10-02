"""Print the control plane's OpenAPI document (from the in-memory demo app) as JSON.

Used by `npm run gen:api` in controlplane/ui/web to generate the UI's typed API client, so
the UI cannot drift from the API without the build failing.
"""

import json

from controlplane.demo import build_demo

print(json.dumps(build_demo().app.openapi(), indent=2, sort_keys=True))
