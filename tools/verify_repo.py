"""Verify the pushed repository through the API.

PowerShell mangles jq expressions (it splits on the quotes and pipes), so the
checks live here where the argument vector is explicit.
"""

from __future__ import annotations

import json
import subprocess

REPO = "lyear12/docqc"


def api(path: str) -> object:
    """GET a GitHub API path and parse the JSON.

    Args:
        path: API path relative to ``repos/<owner>/<name>``.

    Returns:
        Parsed JSON.
    """
    result = subprocess.run(  # noqa: S603
        ["gh", "api", f"repos/{REPO}" + (f"/{path}" if path else "")],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    return json.loads(result.stdout)


meta = api("")
print("=== repository ===")
print(f"  name       : {meta['name']}")
print(f"  visibility : {meta['visibility']}")
print(f"  license    : {(meta.get('license') or {}).get('spdx_id')}")
print(f"  url        : {meta['html_url']}")
print(f"  branch     : {meta['default_branch']}")
print(f"  size       : {meta.get('size')} KB")

commits = api("commits")
print("\n=== latest commit ===")
head = commits[0]
print(f"  sha  : {head['sha'][:12]}")
print(f"  date : {head['commit']['author']['date']}")
print(f"  msg  : {head['commit']['message'].splitlines()[0]}")

tree = api("git/trees/main?recursive=1")
blobs = [node["path"] for node in tree["tree"] if node["type"] == "blob"]
print(f"\n=== files ({len(blobs)}) ===")
for path in sorted(blobs):
    print(f"  {path}")

print(f"\n=== workflows ===")
try:
    workflows = api("actions/workflows")
    for wf in workflows.get("workflows", []):
        print(f"  {wf['name']:12} {wf['state']:12} {wf['path']}")
except subprocess.CalledProcessError as exc:
    print(f"  (could not read workflows: {exc})")

print(f"\nhttps://github.com/{REPO}")
