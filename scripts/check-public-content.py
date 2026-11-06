#!/usr/bin/env python3
"""Check public files and, optionally, every stored Git object for private data.

Findings omit matched values. This checks recognizable patterns, not every
possible personal fact. Run before publishing, in addition to human review.
Normal author, committer and tagger attribution is intentionally public;
private paths and credential patterns are still checked in those fields.
"""

import argparse
import os
from pathlib import Path
import re
import socket
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
PATTERNS = {
    "absolute home directory": re.compile(rb"/(?:Users|home)/[^/\s<>`'\"\\]+"),
    "Windows home directory": re.compile(rb"[A-Za-z]:\\Users\\[^\\\s<>`'\"]+"),
    "private key": re.compile(rb"-----BEGIN (?:OPENSSH |RSA |EC |DSA )?PRIVATE KEY-----"),
    "possible access token": re.compile(rb"(?:gh[pousr]_[A-Za-z0-9]{25,}|github_pat_[A-Za-z0-9_]{30,}|sk-[A-Za-z0-9_-]{32,})"),
}
EMAIL = re.compile(rb"[A-Za-z0-9_.+%-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")


def git(*args: str) -> bytes:
    return subprocess.check_output(["git", "-C", str(ROOT), *args])


def scan(raw: bytes, identities: set[bytes]) -> set[str]:
    findings = {label for label, pattern in PATTERNS.items() if pattern.search(raw)}
    for match in EMAIL.finditer(raw):
        domain = match.group(1).lower()
        if not (domain.endswith((b".test", b".invalid")) or domain in
                {b"example.com", b"example.org", b"example.net"}):
            findings.add("email address")
    for identity in identities:
        if re.search(rb"(?<![A-Za-z0-9_])" + re.escape(identity) + rb"(?![A-Za-z0-9_])", raw, re.I):
            findings.add("local user or host identifier")
    return findings


def scan_git_object(raw: bytes, kind: str, identities: set[bytes]) -> set[str]:
    """Allow normal Git attribution while retaining path/secret checks everywhere."""
    if kind == "blob":
        return scan(raw, identities)
    headers, _, message = raw.partition(b"\n\n")
    findings = scan(message, identities)
    for line in headers.splitlines():
        if line.startswith((b"author ", b"committer ", b"tagger ")):
            # Names and emails in these fields are intentional public credit.
            findings.update(label for label, pattern in PATTERNS.items() if pattern.search(line))
        else:
            findings.update(scan(line, identities))
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", action="store_true", help="also scan every stored Git blob, commit and tag, including unreachable objects")
    args = parser.parse_args()
    names = {Path.home().name, os.environ.get("USER", ""), os.environ.get("LOGNAME", ""),
             socket.gethostname(), socket.gethostname().split(".")[0]}
    names.update(parent.name for parent in ROOT.parents if parent.parent.name in {"Users", "home"})
    identities = {name.encode() for name in names if len(name) >= 3 and name not in {"root", "localhost"}}
    problems = 0
    files = git("ls-files", "--cached", "--others", "--exclude-standard", "-z").split(b"\0")
    count = 0
    for name in filter(None, files):
        path = ROOT / os.fsdecode(name)
        if not path.is_file() or path.is_symlink():
            continue
        count += 1
        findings = scan(path.read_bytes(), identities)
        if findings:
            problems += 1
            # Even filenames may contain an identifier: report only an index.
            print(f"File #{count}: {', '.join(sorted(findings))}")
    objects = 0
    if args.history:
        for row in git("cat-file", "--batch-all-objects", "--batch-check=%(objectname) %(objecttype)").splitlines():
            oid, kind = row.decode().split()
            if kind not in {"blob", "commit", "tag"}:
                continue
            objects += 1
            raw = git("cat-file", kind, oid)
            findings = scan_git_object(raw, kind, identities)
            if findings:
                problems += 1
                print(f"Object #{objects} ({kind}): {', '.join(sorted(findings))}")
    print(f"Scanned {count} public files and {objects} Git objects; {problems} findings.")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
