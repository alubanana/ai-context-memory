"""Validate controlled tags, local Markdown links, and graph references."""
import argparse
import json
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit

def validate(root):
    root = Path(root).resolve()
    registry = json.loads((root / 'memory/classification/tags.json').read_text(encoding='utf-8'))
    canonical = set(registry['canonical'])
    errors = []
    for path in sorted((root / 'notes').rglob('*.md')):
        text = path.read_text(encoding='utf-8')
        for value in re.findall(r'^tags:\s*(.*)$', text, re.M):
            for tag in re.findall(r'`([^`]+)`', value):
                if tag not in canonical:
                    errors.append(f'{path.relative_to(root)}: unknown tag {tag}')
        for target in re.findall(r'\[[^\]]*\]\(([^)]+)\)', text):
            if urlsplit(target).scheme or target.startswith('#'):
                continue
            target = unquote(target.split('#')[0])
            resolved = (path.parent / target).resolve()
            if not resolved.is_relative_to(root) or not resolved.is_file():
                errors.append(f'{path.relative_to(root)}: unresolved local link {target}')
    graph_path = root / 'knowledge-graph.json'
    if graph_path.exists():
        graph = json.loads(graph_path.read_text(encoding='utf-8'))
        node_ids = [node['id'] for node in graph['nodes']]
        if len(node_ids) != len(set(node_ids)):
            errors.append('duplicate graph node id')
        for edge in graph['edges']:
            if edge['from'] not in node_ids or edge['to'] not in node_ids:
                errors.append('graph edge has an unknown endpoint')
            if edge.get('kind') == 'fact' and not edge.get('source'):
                errors.append('graph fact edge lacks a source')
    return errors

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', type=Path)
    args = parser.parse_args()
    issues = validate(args.root)
    print(json.dumps({'status': 'failed' if issues else 'ok', 'issues': issues}, ensure_ascii=False))
    raise SystemExit(bool(issues))
