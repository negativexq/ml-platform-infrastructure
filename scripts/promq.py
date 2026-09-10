#!/usr/bin/env python3
"""Run a Prometheus instant query and print the result compactly.

    kubectl -n observability port-forward svc/monitoring-prometheus 9090:9090 &
    ./scripts/promq.py 'model_ready{job="ml-platform-inference"}'
"""

from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request

PROM = os.environ.get("PROM", "http://localhost:9090")

# Preferred label to identify a series by, most specific first.
ID_LABELS = ("pod", "reason", "path", "deployment", "model_uri", "instance")


def label_for(metric: dict[str, str]) -> str:
    for key in ID_LABELS:
        if key in metric:
            return metric[key]
    return metric.get("__name__", "-")


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    query = sys.argv[1]
    url = f"{PROM}/api/v1/query?" + urllib.parse.urlencode({"query": query})
    with urllib.request.urlopen(url, timeout=20) as resp:
        body = json.load(resp)

    if body.get("status") != "success":
        print(f"  query error: {body.get('error')}")
        return 1

    result = body["data"]["result"]
    if not result:
        print("  no data")
        return 0

    for series in sorted(result, key=lambda s: label_for(s["metric"])):
        name = label_for(series["metric"])
        value = float(series["value"][1])
        print(f"  {name:<44} {value:.4g}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
