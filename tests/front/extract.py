#!/usr/bin/env python3
"""Extrait le résultat JSON de <pre id="__result"> d'un dump-dom chromium."""
import html
import json
import re
import sys


def main() -> int:
    path, label = sys.argv[1], sys.argv[2]
    raw = open(path, encoding="utf-8").read()
    match = re.search(r'<pre id="__result">(.*?)</pre>', raw, re.S)
    if not match:
        print(f"{label}: AUCUN RÉSULTAT (<pre id=__result> absent)")
        return 2
    data = json.loads(html.unescape(match.group(1)))
    print(f"{label}: {data['pass']}/{data['total']}")
    for failure in data.get("failures", []):
        extra = f" — {failure['extra']}" if failure.get("extra") else ""
        print(f"  FAIL: {failure['name']}{extra}")
    if data.get("jsErrors"):
        print("  JS ERRORS: " + json.dumps(data["jsErrors"], ensure_ascii=False))
    return 0 if data["pass"] == data["total"] and not data.get("jsErrors") else 1


if __name__ == "__main__":
    raise SystemExit(main())
