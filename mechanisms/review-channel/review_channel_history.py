"""Review-specific consumption of the shared CL-52 reader; no second locator parser."""
import os
from pathlib import Path
import sys
import review_channel_base as base
import review_channel_contract as C
import review_channel_receipt as RC


def helper():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'repo-layout'))
    try:
        import history_read
    except ImportError as exc:
        raise base.PreflightError('history-unavailable', 'shared history helper unavailable') from exc
    return history_read


def items(root, prefix):
    h = helper()
    try:
        return h.working_history(root, prefix)
    except h.HistoryReadError as exc:
        raise base.PreflightError('history-unavailable', exc.code+': '+exc.message)


def read(root, path):
    h = helper()
    try:
        view = h.head_commit(root)
        if view is None:
            return None
        return h.read_history(root, view, path)
    except h.HistoryReadError as exc:
        if exc.code == 'history-not-found':
            return None
        raise base.PreflightError('history-unavailable', exc.code+': '+exc.message)


def round_files(root, directory):
    """Read a complete historical publication from one source, matching present pieces.

    Return None for a purely current round, preserving legacy schema behaviour.
    """
    historical = items(root, directory+'/')
    if not historical:
        return None
    by_path = {}
    for i in historical:
        if '/' in i['path'][len(directory)+1:]:
            raise base.PreflightError('history-unavailable', 'history-invalid: nested round member')
        if i['path'] in by_path and i['blob_oid'] != by_path[i['path']]['blob_oid']:
            raise base.PreflightError('history-unavailable', 'history-ambiguous: round member '+i['path'])
        by_path[i['path']] = i
    # Use a declared source that actually contains all members, never stitch snapshots.
    h = helper(); view = h.head_commit(root)
    for source in sorted({i['source_commit'] for i in historical}):
        try:
            g = h.GitHistory(root, view); g.ancestor(source)
            paths = [p for p in g.tree(source) if p.startswith(directory+'/')]
            if len(paths) != 4 or any('/' in p[len(directory)+1:] for p in paths):
                continue
            content = {p: g.blob(source, p) for p in paths}
            current_root = Path(root,directory)
            present_paths = {p.relative_to(root).as_posix() for p in current_root.rglob('*') if p.is_file()} if current_root.exists() else set()
            if not present_paths <= set(paths):
                raise base.PreflightError('history-unavailable', 'history-integrity: extra current round member')
            if not set(by_path) <= set(content):
                continue
            if any(content[p]['sha256'] != i['sha256'] for p,i in by_path.items()):
                continue
            for p,i in content.items():
                present = Path(root,p)
                if present.exists():
                    if present.is_symlink() or base.read_bytes(str(present)) != i['content']:
                        raise base.PreflightError('history-unavailable', 'history-integrity: present round member differs '+p)
                elif p not in by_path:
                    raise base.PreflightError('history-unavailable', 'history-invalid: missing undeclared member '+p)
            names = {p.rsplit('/',1)[-1] for p in content}
            if 'bundle_manifest.json' not in names or not any(n.startswith('receipt-r') and n.endswith('.json') for n in names):
                continue
            manifest = base.strict_json_load(content[directory+'/bundle_manifest.json']['content'])
            task = directory+'/'+manifest['task_file']
            receipts = [p for p in paths if p.rsplit('/',1)[-1].startswith('receipt-r')]
            receipt = base.strict_json_load(content[receipts[0]]['content'])
            verdict = receipt['verdict_path']
            round_name = directory.rsplit('/',1)[-1]
            expected_round = round_name.split('-')[-1]
            expected_stage = round_name.split('-')[0] if directory.startswith('tasks/') else receipt.get('stage')
            if receipt.get('receipt_schema') not in C.RECEIPT_READ_SCHEMAS:
                raise base.PreflightError('inherit-unanchored', 'historical receipt schema incompatible')
            if receipt.get('round') != expected_round or receipt.get('stage') != expected_stage:
                raise base.PreflightError('inherit-unanchored', 'historical receipt round/stage mismatch')
            if directory.startswith('reviews/') and receipt.get('subject') != directory.split('/')[1]:
                raise base.PreflightError('inherit-unanchored', 'historical receipt subject mismatch')
            if any(manifest.get(k) != receipt.get(k) for k in ('subject','stage','round')):
                raise base.PreflightError('inherit-unanchored', 'historical manifest route mismatch')
            if RC.receipt_shape_problems(receipt) or receipt.get("evidence_storage") is not None:
                raise base.PreflightError('inherit-unanchored', 'historical Profile shape mismatch')
            if {task, verdict, directory+'/bundle_manifest.json', receipts[0]} != set(paths):
                continue
            if base.sha256_bytes(base.canonical_json(manifest['inputs'])) != manifest['manifest_sha256'] or manifest['manifest_sha256'] != receipt['input_manifest_sha256']:
                raise base.PreflightError('history-unavailable', 'history-integrity: round manifest')
            if content[verdict]['sha256'] != receipt['verdict_sha256']:
                raise base.PreflightError('history-unavailable', 'history-integrity: round verdict')
            task_items = [i for i in manifest['inputs'] if i.get('role')=='review-task']
            if len(task_items)!=1 or task_items[0]['sha256']!=content[task]['sha256'] or task_items[0]['bytes']!=content[task]['bytes']:
                raise base.PreflightError('history-unavailable', 'history-integrity: round taskbook')
            return content
        except h.HistoryReadError as exc:
            raise base.PreflightError('history-unavailable', exc.code+': '+exc.message)
        except (KeyError, ValueError, TypeError, UnicodeError) as exc:
            raise base.PreflightError('inherit-unanchored', 'historical round schema incompatible: '+type(exc).__name__)
    raise base.PreflightError('history-unavailable', 'history-integrity: no single complete round source')
