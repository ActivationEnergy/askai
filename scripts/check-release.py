#!/usr/bin/env python3
"""Read-only verification of an isolated release candidate; never changes Git refs."""

import argparse
from pathlib import Path
import re
import subprocess
import sys
import tomllib


class ReleaseError(Exception):
    """A candidate or its publication baseline could not be verified."""


def git(repo, *args):
    try:
        return subprocess.check_output(['git', '-C', str(repo), *args], stderr=subprocess.PIPE,
                                       text=True, timeout=30).strip()
    except (OSError, subprocess.SubprocessError) as exc:
        raise ReleaseError('Git verification failed; remote state is unknown if this was an online check') from exc


def field(text, pattern, description):
    match = re.search(pattern, text, re.M)
    if not match:
        raise ReleaseError('Missing or invalid ' + description)
    return match.group(1)


def verify(repo, *, tag, parent, expected_version=None, formula=None, allow_untagged=False, online=False):
    repo = Path(repo).resolve()
    if Path(git(repo, 'rev-parse', '--show-toplevel')).resolve() != repo:
        raise ReleaseError('Candidate path must be the Git working tree root')
    if git(repo, 'status', '--porcelain=v1', '--untracked-files=all'):
        raise ReleaseError('Candidate must be clean, including staged and untracked public files')
    head = git(repo, 'rev-parse', 'HEAD^{commit}')
    baseline = git(repo, 'rev-parse', parent + '^{commit}')
    parents = git(repo, 'rev-list', '--parents', '-n', '1', head).split()[1:]
    if parents != [baseline]:
        raise ReleaseError('Candidate must be one release commit directly after the verified baseline')
    project = tomllib.loads((repo / 'pyproject.toml').read_text())
    version = project['project']['version']
    module_version = field((repo / 'src/askai_cli/__init__.py').read_text(),
                           r'^__version__\s*=\s*[\'"]([^\'"]+)[\'"]', 'module version')
    packages = tomllib.loads((repo / 'uv.lock').read_text())['package']
    lock_versions = [item.get('version') for item in packages if item.get('name') == 'askai-cli']
    if module_version != version or lock_versions != [version] or (expected_version is not None and version != expected_version):
        raise ReleaseError('Project, module, lock and expected versions must agree')
    if tag != 'v' + version:
        raise ReleaseError('Tag must equal v plus the candidate version')
    source_formula = (repo / 'packaging/homebrew/askai.rb').read_text()
    check_formula(source_formula, version, tag)
    refs = git(repo, 'for-each-ref', '--format=%(refname)', 'refs/tags').splitlines()
    exists = 'refs/tags/' + tag in refs
    if exists:
        if git(repo, 'rev-parse', 'refs/tags/' + tag + '^{commit}') != head:
            raise ReleaseError('Existing local tag points to another commit; do not rewrite it')
    elif not allow_untagged:
        raise ReleaseError('Local release tag is missing; use --allow-untagged only for preparation')
    if formula is not None:
        check_formula(Path(formula).read_text(), version, tag, head)
    remote_status = 'not checked; publication not authorized by this offline result'
    if online:
        rows = git(repo, 'ls-remote', '--heads', '--tags', 'origin', 'refs/heads/main',
                   'refs/tags/' + tag, 'refs/tags/' + tag + '^{}')
        remote = dict((name, oid) for oid, name in (line.split() for line in rows.splitlines()))
        main = remote.get('refs/heads/main')
        remote_tag = remote.get('refs/tags/' + tag + '^{}', remote.get('refs/tags/' + tag))
        if remote_tag is not None and remote_tag != head:
            raise ReleaseError('Remote tag points elsewhere; do not rewrite it')
        if main not in (baseline, head):
            raise ReleaseError('Remote main differs from the reviewed baseline and candidate')
        if remote_tag == head and main == head:
            remote_status = 'already published'
        elif main == baseline and remote_tag is None:
            remote_status = 'baseline confirmed; exact refs may be published after all other release checks'
        else:
            raise ReleaseError('Remote publication is partial or changed; review before pushing')
    return {'version': version, 'commit': head, 'tree': git(repo, 'rev-parse', head + '^{tree}'),
            'tag': tag, 'tag_present': exists, 'remote': remote_status}


def check_formula(text, version, tag, revision=None):
    if field(text, r'^\s*version\s+"([^"]+)"', 'Formula version') != version:
        raise ReleaseError('Formula version mismatch')
    if field(text, r'\btag:\s*"([^"]+)"', 'Formula tag') != tag:
        raise ReleaseError('Formula tag mismatch')
    if revision is not None and field(text, r'\brevision:\s*"([^"]+)"', 'Formula revision') != revision:
        raise ReleaseError('Tap Formula revision does not match the candidate commit')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, epilog='Build from a git archive of the verified commit, not a working tree containing ignored files. Offline success does not confirm remote state.')
    parser.add_argument('repo', type=Path, help='isolated release repository, never the private develop checkout')
    parser.add_argument('--tag', required=True)
    parser.add_argument('--parent', required=True, help='previous reviewed release commit; must be the sole direct parent')
    parser.add_argument('--version', dest='expected_version')
    parser.add_argument('--formula', type=Path, help='independent tap Formula; requires an exact revision')
    parser.add_argument('--allow-untagged', action='store_true', help='prepare only; do not publish until tagged and reverified')
    parser.add_argument('--online', action='store_true', help='also verify origin main and this tag; network failures block verification')
    args = parser.parse_args(argv)
    try:
        result = verify(**vars(args))
    except (ReleaseError, OSError, ValueError, KeyError, TypeError) as exc:
        print('Release verification failed: ' + (str(exc) if isinstance(exc, ReleaseError) else 'invalid or unreadable release metadata'), file=sys.stderr)
        return 1
    for key, value in result.items():
        print(f'{key}: {value}')
    if args.allow_untagged:
        print('Preparation only: a matching local tag and final verification are required before publication.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
