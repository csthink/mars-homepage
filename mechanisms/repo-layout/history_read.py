#!/usr/bin/env python3
"""Fixed Git history reads. Contract: repo-layout design §7 (CL-52).

No network, working-tree writes, persistent caches, or interpretation of authority.
The syntax-only entry point does not invoke Git and can run outside a repository.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

LOCATIONS = 'records/history-locations.jsonl'
KEYS = {'version', 'source_commit', 'selection', 'paths_sha256', 'objects_sha256', 'authority_ref'}
HEX = re.compile(r'^[0-9a-f]{64}$')


class HistoryReadError(RuntimeError):
    def __init__(self, code, message, path=None, source_commit=None):
        super().__init__(message)
        self.code, self.message, self.path, self.source_commit = code, message, path, source_commit

    def as_dict(self):
        return dict(version=1, code=self.code, message=self.message,
                    path=self.path, source_commit=self.source_commit)


def fail(code, message, path=None, source_commit=None):
    raise HistoryReadError(code, message, path, source_commit)


def path_ok(path, directory=False, empty=False):
    if empty and path == '':
        return True
    if not isinstance(path, str) or not path or '\\' in path or any(ord(c) < 32 or ord(c) == 127 for c in path):
        return False
    if directory != path.endswith('/'):
        return False
    parts = (path[:-1] if directory else path).split('/')
    if any(p in ('', '.', '..', '.git') for p in parts):
        return False
    if parts[0] == 'review-attempts' or (len(parts) > 2 and parts[0] == 'tasks' and parts[2] == 'attempts'):
        return False
    return True


def _pairs(pairs):
    obj = {}
    for k, v in pairs:
        if k in obj:
            fail('history-invalid', 'duplicate JSON key: ' + k, LOCATIONS)
        obj[k] = v
    return obj


def validate_locations_syntax(content):
    try:
        text = content.decode('utf-8') if isinstance(content, bytes) else content
        if not isinstance(text, str) or '\r' in text or (text and not text.endswith('\n')):
            raise ValueError('expected UTF-8 lines ending in LF')
        rows, seen = [], set()
        for line in text.split('\n')[:-1]:
            row = json.loads(line, object_pairs_hook=_pairs)
            if not isinstance(row, dict) or set(row) != KEYS or type(row['version']) is not int or row['version'] != 1:
                raise ValueError('unknown keys, version or row shape')
            if not isinstance(row['source_commit'], str) or not re.fullmatch(r'(?:[0-9a-f]{40}|[0-9a-f]{64})', row['source_commit']):
                raise ValueError('source_commit must be a full object ID')
            if any(not isinstance(row[k], str) or not HEX.fullmatch(row[k]) for k in ('paths_sha256', 'objects_sha256')):
                raise ValueError('invalid selection digest')
            sel = row['selection']
            if not isinstance(sel, dict) or set(sel) != {'roots', 'paths', 'exclude'}:
                raise ValueError('invalid selection keys')
            for k in sel:
                if not isinstance(sel[k], list) or any(not path_ok(p, k == 'roots') for p in sel[k]) or len(set(sel[k])) != len(sel[k]):
                    raise ValueError('invalid or repeated selection path')
            if not sel['roots'] and not sel['paths']:
                raise ValueError('empty selection')
            ref = row['authority_ref']
            if not isinstance(ref, str) or ref.count('#') != 1:
                raise ValueError('invalid authority_ref')
            p, loc = ref.split('#')
            if not path_ok(p) or not loc or any(ord(c) < 32 or ord(c) == 127 for c in loc):
                raise ValueError('invalid authority path or locator')
            canonical = json.dumps(row, sort_keys=True, separators=(',', ':'))
            if canonical in seen:
                raise ValueError('duplicate location row')
            seen.add(canonical); rows.append(row)
        return rows
    except (ValueError, TypeError, UnicodeError) as exc:
        fail('history-invalid', str(exc), LOCATIONS)


class GitHistory:
    def __init__(self, repo_root, view_commit):
        self.root = str(Path(repo_root).resolve())
        self.env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
        self.env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
                        GIT_NO_REPLACE_OBJECTS='1', GIT_NO_LAZY_FETCH='1', GIT_TERMINAL_PROMPT='0')
        common = self.git('rev-parse', '--git-common-dir').decode().strip()
        if (Path(self.root) / common / 'info/grafts').exists():
            fail('history-invalid', 'Git grafts are unsupported')
        if self.git('for-each-ref', '--format=%(refname)', 'refs/replace/'):
            fail('history-invalid', 'Git replace objects are unsupported')
        fmt = self.git('rev-parse', '--show-object-format').decode().strip()
        self.oid_len = {'sha1': 40, 'sha256': 64}.get(fmt)
        self.view = view_commit
        self.commit(view_commit)
        self._trees = {}
        self._blobs = {}
        self._ancestors = {view_commit}

    def git(self, *args, missing_ok=False):
        p = subprocess.run(['git', '-c', 'protocol.allow=never', '-c', 'core.fsmonitor=false',
                            '-c', 'core.quotePath=false', '-C', self.root, *args],
                           env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if p.returncode and not missing_ok:
            fail('history-unavailable', 'Git operation failed: ' + args[0], source_commit=getattr(self, 'view', None))
        return None if p.returncode else p.stdout

    def commit(self, oid):
        if not isinstance(oid, str) or not self.oid_len or not re.fullmatch('[0-9a-f]{%d}' % self.oid_len, oid):
            fail('history-invalid', 'expected complete commit ID', source_commit=oid)
        if self.git('cat-file', '-t', oid, missing_ok=True) != b'commit\n':
            fail('history-unavailable', 'commit object missing or wrong type', source_commit=oid)

    def ancestor(self, source):
        if source in self._ancestors:
            return
        self.commit(source)
        if self.git('merge-base', '--is-ancestor', source, self.view, missing_ok=True) is None:
            fail('history-unavailable', 'source ancestry cannot be proved', source_commit=source)
        self._ancestors.add(source)

    def tree(self, commit):
        if commit not in self._trees:
            tree = {}
            for entry in self.git('ls-tree', '-rz', '--full-tree', commit).split(b'\0'):
                if not entry:
                    continue
                head, name = entry.split(b'\t', 1)
                try:
                    name = name.decode('utf-8')
                except UnicodeError:
                    fail('history-invalid', 'tree path is not UTF-8', source_commit=commit)
                mode, typ, oid = head.decode().split(' ')
                tree[name] = (mode, typ, oid)
            self._trees[commit] = tree
        return self._trees[commit]

    def blob(self, commit, path):
        if not path_ok(path):
            fail('history-invalid', 'unsafe file path', path, commit)
        if (commit, path) in self._blobs:
            return dict(self._blobs[commit, path])
        entry = self.tree(commit).get(path)
        if entry is None:
            fail('history-not-found', 'path absent from fixed tree', path, commit)
        mode, typ, oid = entry
        if mode not in ('100644', '100755') or typ != 'blob':
            fail('history-invalid', 'only regular blobs are readable', path, commit)
        b = self.git('cat-file', 'blob', oid)
        item = dict(source_commit=commit, path=path, blob_oid=oid, bytes=len(b),
                    sha256=hashlib.sha256(b).hexdigest(), content=b)
        self._blobs[commit, path] = item
        return dict(item)

    def index(self, commit):
        if LOCATIONS not in self.tree(commit):
            return b''
        return self.blob(commit, LOCATIONS)['content']

    def locations(self):
        if self.git('rev-parse', '--is-shallow-repository').strip() == b'true':
            fail('history-unavailable', 'shallow history cannot prove location continuity', LOCATIONS)
        commits = self.git('rev-list', '--full-history', '--reverse', '--topo-order', self.view, '--', LOCATIONS).decode().splitlines()
        introductions = {}
        for commit in commits:
            content = self.index(commit)
            rows = validate_locations_syntax(content)
            parents = self.git('rev-list', '--parents', '-n', '1', commit).decode().split()[1:]
            for parent in parents:
                prev = self.index(parent)
                if not content.startswith(prev):
                    fail('history-invalid', 'location history is not append-only', LOCATIONS, commit)
            for row, line in zip(rows, content.splitlines(keepends=True)):
                introductions.setdefault(line, commit)
        current = self.index(self.view)
        rows = validate_locations_syntax(current)
        result = []
        for row, line in zip(rows, current.splitlines(keepends=True)):
            intro = introductions.get(line)
            if not intro:
                fail('history-unavailable', 'location introduction cannot be proved', LOCATIONS)
            result.append((row, intro))
        return result

    def expand(self, row, intro):
        source = row['source_commit']; self.ancestor(source)
        tree = self.tree(source); sel = row['selection']; paths = set(sel['paths'])
        if self.git('merge-base', '--is-ancestor', source, intro, missing_ok=True) is None:
            fail('history-unavailable', 'source must precede declaration introduction', LOCATIONS, source)
        if not paths <= set(tree):
            fail('history-invalid', 'selection contains absent paths', LOCATIONS, source)
        for root in sel['roots']:
            matches = {p for p in tree if p.startswith(root)}
            if not matches:
                fail('history-invalid', 'selection root matches no files', root, source)
            paths.update(matches)
        if not set(sel['exclude']) <= paths or not paths - set(sel['exclude']):
            fail('history-invalid', 'invalid exclusion or empty selection', LOCATIONS, source)
        paths.difference_update(sel['exclude'])
        ordered = sorted(paths, key=lambda p: p.encode('utf-8'))
        items = [self.blob(source, p) for p in ordered]
        pd = hashlib.sha256(''.join(p+'\n' for p in ordered).encode()).hexdigest()
        od = hashlib.sha256(''.join(i['path']+'\t'+i['blob_oid']+'\n' for i in items).encode()).hexdigest()
        if (pd, od) != (row['paths_sha256'], row['objects_sha256']):
            fail('history-integrity', 'selection digest mismatch', LOCATIONS, source)
        p, locator = row['authority_ref'].split('#')
        try:
            text = self.blob(intro, p)['content'].decode('utf-8')
        except UnicodeError:
            fail('history-invalid', 'authority is not UTF-8', p, intro)
        headings = [re.sub(r'^#{1,6}\s+', '', line).strip() for line in text.splitlines() if re.match(r'^#{1,6}\s+', line)]
        matches = headings.count(locator)
        if not matches:
            matches = len(re.findall(r'(?<![\w:-])'+re.escape(locator)+r'(?![\w:-])', text))
        if matches != 1:
            fail('history-invalid', 'authority locator is not unique', p, intro)
        return items

    def items(self, prefix=''):
        if not path_ok(prefix, directory=True, empty=True):
            fail('history-invalid', 'invalid directory prefix', prefix)
        out, groups, claims = {}, 0, {}
        for row, intro in self.locations():
            sel = row['selection']
            if prefix and not any(p.startswith(prefix) or prefix.startswith(p) for p in sel['roots']+sel['paths']):
                continue
            items = self.expand(row, intro); groups += 1
            for i in items:
                # A repeated authorization cannot claim a new version of the same path.
                claim = (row['authority_ref'], i['path'])
                if claim in claims and claims[claim] != i['blob_oid']:
                    fail('history-integrity', 'conflicting immutable group', i['path'], i['source_commit'])
                claims[claim] = i['blob_oid']
                if not i['path'].startswith(prefix):
                    continue
                key = (i['path'], i['blob_oid'])
                if key not in out or i['source_commit'] < out[key]['source_commit']:
                    out[key] = i
        return sorted(out.values(), key=lambda i: (i['path'].encode(), i['source_commit'].encode())), groups


def list_history(repo_root, view_commit, prefix=''):
    return [{k: v for k, v in i.items() if k != 'content'} for i in GitHistory(repo_root, view_commit).items(prefix)[0]]


def read_history(repo_root, view_commit, path, source_commit=None, expected_sha256=None):
    if not path_ok(path):
        fail('history-invalid', 'unsafe file path', path, source_commit)
    if expected_sha256 is not None and (not isinstance(expected_sha256, str) or not HEX.fullmatch(expected_sha256)):
        fail('history-invalid', 'invalid expected SHA-256', path, source_commit)
    g = GitHistory(repo_root, view_commit)
    if source_commit is not None:
        g.ancestor(source_commit); item = g.blob(source_commit, path)
    else:
        prefix = path.rsplit('/', 1)[0]+'/' if '/' in path else ''
        items = [i for i in g.items(prefix)[0] if i['path'] == path]
        if not items:
            fail('history-not-found', 'no declared historical source', path)
        if len(items) != 1:
            fail('history-ambiguous', 'multiple historical versions require source_commit', path)
        item = items[0]
    if expected_sha256 is not None and item['sha256'] != expected_sha256:
        fail('history-integrity', 'content SHA-256 mismatch', path, item['source_commit'])
    return item


def head_commit(repo_root):
    """Consumer convenience: resolve the working repository view once per operation."""
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    env.update(GIT_NO_REPLACE_OBJECTS='1', GIT_NO_LAZY_FETCH='1', GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull)
    p = subprocess.run(['git', '-c', 'protocol.allow=never', '-C', str(repo_root), 'rev-parse', '--verify', 'HEAD'],
                       env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return p.stdout.decode().strip() if p.returncode == 0 else None


def working_history(repo_root, prefix=''):
    """No unborn-repository history; never ignore a present index without a commit."""
    view = head_commit(repo_root)
    if view is None:
        if (Path(repo_root)/LOCATIONS).exists():
            fail('history-unavailable', 'location declarations have no committed view', LOCATIONS)
        return []
    return list_history(repo_root, view, prefix)


def next_owner_decision(repo_root, subject, filename_prefix):
    """Return the next volume index and original predecessor path (layout §6.6)."""
    prefix = 'records/governance/'+subject+'/'
    if not path_ok(prefix, directory=True) or '/' in filename_prefix:
        fail('history-invalid', 'invalid Owner Decision subject or filename prefix')
    paths = {p.as_posix() for p in Path(repo_root, prefix).glob(filename_prefix+'*.md')}
    paths = {Path(p).relative_to(repo_root).as_posix() for p in paths}
    paths.update(i['path'] for i in working_history(repo_root, prefix))
    rx = re.compile(re.escape(prefix+filename_prefix)+r'(\d+)\.md$')
    entries = [(int(rx.fullmatch(p)[1]), p) for p in paths if rx.fullmatch(p)]
    latest = max(entries, default=(0, None))
    return latest[0]+1, latest[1]


class Parser(argparse.ArgumentParser):
    def error(self, message):
        fail('usage-error', message)


def main(argv=None):
    try:
        parser = Parser(description=__doc__)
        commands = parser.add_subparsers(dest='command', required=True, parser_class=Parser)
        for name in ('list', 'read', 'check'):
            p = commands.add_parser(name)
            p.add_argument('--repo-root', required=True); p.add_argument('--view-commit', required=True)
            if name == 'read':
                p.add_argument('--path', required=True); p.add_argument('--source-commit'); p.add_argument('--expected-sha256')
            else:
                p.add_argument('--prefix', default='')
        a = parser.parse_args(argv)
        if not os.path.isabs(a.repo_root):
            fail('usage-error', '--repo-root must be absolute')
        if a.command == 'read':
            item = read_history(a.repo_root, a.view_commit, a.path, a.source_commit, a.expected_sha256)
            content = item.pop('content')
            sys.stdout.buffer.write(content); print(json.dumps(item), file=sys.stderr)
        elif a.command == 'list':
            print(json.dumps(dict(version=1, items=list_history(a.repo_root, a.view_commit, a.prefix))))
        else:
            items, groups = GitHistory(a.repo_root, a.view_commit).items(a.prefix)
            print(json.dumps(dict(version=1, state='PASS', groups=groups, files=len(items))))
        return 0
    except HistoryReadError as exc:
        print(json.dumps(exc.as_dict()), file=sys.stderr)
        return 2 if exc.code in ('usage-error', 'internal-error') else 1
    except Exception as exc:
        print(json.dumps(HistoryReadError('internal-error', type(exc).__name__).as_dict()), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
