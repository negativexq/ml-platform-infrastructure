#!/usr/bin/env bash
# Run every query in every checked-in dashboard against Prometheus and report
# which ones return data.
#
# The point is to keep the dashboards honest: a panel that never returns a
# series is a claim this platform cannot back up, and should be removed rather
# than left looking impressive.
set -euo pipefail

PROM="${PROM:-http://localhost:9090}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

cd "$REPO_ROOT"

python3 - "$PROM" <<'PY'
import json, pathlib, sys, urllib.parse, urllib.request

prom = sys.argv[1]
failures = []

for path in sorted(pathlib.Path("observability/dashboards").glob("*.json")):
    dash = json.loads(path.read_text())
    print(f"\n{dash['title']}  ({path})")
    for panel in dash["panels"]:
        if panel["type"] == "row":
            continue
        for target in panel.get("targets", []):
            expr = target["expr"]
            url = f"{prom}/api/v1/query?" + urllib.parse.urlencode({"query": expr})
            try:
                with urllib.request.urlopen(url, timeout=15) as resp:
                    body = json.load(resp)
            except Exception as err:
                print(f"  ERROR  {panel['title']}: {err}")
                failures.append((panel["title"], expr, str(err)))
                continue
            if body.get("status") != "success":
                print(f"  ERROR  {panel['title']}: {body.get('error')}")
                failures.append((panel["title"], expr, body.get("error", "")))
                continue
            series = body["data"]["result"]
            mark = "OK  " if series else "EMPTY"
            print(f"  {mark}   {panel['title']:<38} {len(series)} series")
            if not series:
                failures.append((panel["title"], expr, "no series"))

print()
if failures:
    print(f"{len(failures)} query/queries returned nothing:")
    for title, expr, why in failures:
        print(f"  - {title}: {why}\n      {expr}")
    sys.exit(1)
print("every dashboard query returns data")
PY
