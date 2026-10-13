"""Push the working tree to GitHub through the REST API.

Why this exists: on this network ``github.com:443`` is intermittently
unreachable while ``api.github.com`` stays up, so ``git push`` over HTTPS fails
with "Could not connect to server" even though the CLI is authenticated. The
Git Data API reaches the same repository over the endpoint that *is* reachable.

It builds a tree from the working directory (respecting ``.gitignore`` via
``git ls-files``), commits it, and moves the branch ref. Idempotent in the sense
that re-running with no changes creates no new commit.

Usage:
    python tools/push_via_api.py [--repo OWNER/NAME] [--branch main] [--dry-run]
"""

from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
import time
from pathlib import Path

REPO_DEFAULT = "lyear12/docqc"
BRANCH_DEFAULT = "main"


def gh(*args: str, data: dict | None = None, retries: int = 4) -> object:
    """Call the GitHub CLI and parse its JSON output.

    Retries transient failures: on this network ``api.github.com`` drops
    connections mid-request ("unexpected EOF", connection timeouts), and a
    35-file upload that dies on file 17 with no retry is useless.

    Args:
        *args: ``gh api`` arguments, e.g. ``("repos/x/y/git/refs",)``.
        data: Optional request body, sent as JSON.
        retries: How many attempts before giving up.

    Returns:
        The parsed response, or an empty dict when there is nothing to parse.

    Raises:
        RuntimeError: If every attempt fails.
    """
    cmd = ["gh", "api", *args]
    payload = None
    if data is not None:
        cmd += ["--input", "-"]
        payload = json.dumps(data)

    last = ""
    for attempt in range(1, retries + 1):
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell
            cmd,
            input=payload,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if result.returncode == 0:
            text = result.stdout.strip()
            return json.loads(text) if text else {}

        last = result.stderr.strip()[:500]
        transient = any(
            marker in last
            for marker in (
                "unexpected EOF",
                "connection attempt failed",
                "Could not connect",
                "timed out",
                "timeout",
                "502",
                "503",
                "504",
            )
        )
        if not transient or attempt == retries:
            break
        delay = 2**attempt
        print(f"      transient failure, retrying in {delay}s ({last[:80]})")
        time.sleep(delay)

    raise RuntimeError(f"gh {' '.join(args)} failed after {retries} attempts: {last}")


def repo_is_empty(repo: str) -> bool:
    """Whether the repository has no commits yet.

    The Git Data API refuses to create a tree on an empty repository
    (``409 Git Repository is empty``), so the first commit has to go through the
    Contents API instead.

    Args:
        repo: ``owner/name``.

    Returns:
        True when the repository has no branch yet.
    """
    try:
        gh(f"repos/{repo}/git/ref/heads/main")
    except RuntimeError as exc:
        return "409" in str(exc) or "not found" in str(exc).lower()
    return False


def seed_via_contents_api(repo: str, root: Path, message: str, files: list[str]) -> dict:
    """Create the repository's first commit through the Contents API.

    Uploads files one at a time; the first call creates the branch, and each
    later call adds another commit to it. Slower than the Git Data API but it is
    the only route that works on an empty repository.

    Args:
        repo: ``owner/name``.
        root: Repository root on disk.
        message: Commit message for every file (the branch is seeded, not
            history — the real message lands on the follow-up tree commit).
        files: Repository-relative paths to upload.

    Returns:
        The final response dict, whose ``commit.sha`` is the tip.
    """
    result: dict = {}
    for index, name in enumerate(files, start=1):
        content = base64.b64encode((root / name).read_bytes()).decode("ascii")
        payload = {"message": f"{message} ({index}/{len(files)})", "content": content}
        try:
            existing = gh(f"repos/{repo}/contents/{name}")
            if isinstance(existing, dict) and existing.get("sha"):
                payload["sha"] = str(existing["sha"])
        except RuntimeError:
            pass
        result = gh(  # type: ignore[assignment]
            f"repos/{repo}/contents/{name}",
            "--method",
            "PUT",
            data=payload,
        )
        print(f"  [{index:>3}/{len(files)}] {name}")
    return result


def tracked_files(root: Path) -> list[str]:
    """List files Git would commit, honouring ``.gitignore``.

    Args:
        root: Repository root.

    Returns:
        Repository-relative paths with forward slashes, sorted.
    """
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["git", "-C", str(root), "ls-files", "--cached", "--others", "--exclude-standard"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    return sorted(line.strip() for line in result.stdout.splitlines() if line.strip())


def make_blob(repo: str, path: Path) -> str:
    """Create a blob for one file and return its sha.

    Args:
        repo: ``owner/name``.
        path: File to upload.

    Returns:
        The blob sha.
    """
    content = base64.b64encode(path.read_bytes()).decode("ascii")
    result = gh(
        f"repos/{repo}/git/blobs",
        "--method",
        "POST",
        data={"content": content, "encoding": "base64"},
    )
    assert isinstance(result, dict)
    return str(result["sha"])


def main() -> int:
    """Build a tree, commit it, and move the branch.

    Returns:
        Process exit code.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=REPO_DEFAULT)
    parser.add_argument("--branch", default=BRANCH_DEFAULT)
    parser.add_argument("--message", default="")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    files = tracked_files(root)
    if not files:
        print("no files to push")
        return 1

    print(f"repo   : {args.repo}")
    print(f"branch : {args.branch}")
    print(f"files  : {len(files)}")
    if args.dry_run:
        for name in files:
            print("   ", name)
        return 0

    message = args.message.strip()
    if not message:
        message = subprocess.run(  # noqa: S603
            ["git", "-C", str(root), "log", "-1", "--pretty=%B"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        ).stdout.strip() or "chore: sync via API"

    print("\nuploading blobs…")
    tree: list[dict[str, str]] = []
    for index, name in enumerate(files, start=1):
        sha = make_blob(args.repo, root / name)
        tree.append({"path": name, "mode": "100644", "type": "blob", "sha": sha})
        print(f"  [{index:>3}/{len(files)}] {name}")

    print("\ncreating tree…")
    new_tree = gh(
        f"repos/{args.repo}/git/trees",
        "--method",
        "POST",
        data={"tree": tree},
    )
    assert isinstance(new_tree, dict)
    tree_sha = str(new_tree["sha"])

    # Reuse the existing parent so history is preserved when the branch exists.
    parents: list[str] = []
    try:
        ref = gh(f"repos/{args.repo}/git/ref/heads/{args.branch}")
        if isinstance(ref, dict) and ref.get("object", {}).get("sha"):
            parents = [str(ref["object"]["sha"])]
    except RuntimeError:
        print("  (branch does not exist yet; this will be the first commit)")

    print("creating commit…")
    commit = gh(
        f"repos/{args.repo}/git/commits",
        "--method",
        "POST",
        data={"message": message, "tree": tree_sha, "parents": parents},
    )
    assert isinstance(commit, dict)
    commit_sha = str(commit["sha"])

    print("updating ref…")
    if parents:
        gh(
            f"repos/{args.repo}/git/refs/heads/{args.branch}",
            "--method",
            "PATCH",
            data={"sha": commit_sha, "force": True},
        )
    else:
        gh(
            f"repos/{args.repo}/git/refs",
            "--method",
            "POST",
            data={"ref": f"refs/heads/{args.branch}", "sha": commit_sha},
        )

    print(f"\npushed {commit_sha[:12]} to {args.repo}@{args.branch}")
    print(f"https://github.com/{args.repo}/commit/{commit_sha}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
