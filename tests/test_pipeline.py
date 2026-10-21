import copy
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / 'tools'))
from continuity import collect, load, run, save, seal, stamp
from prompt_intake import clean_text, intake
from knowledge_gate import validate
from demo import build_demo

THREAD = '11111111-1111-4111-8111-111111111111'

class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=BASE)
        self.root = Path(self.tmp.name)
        assert self.root.resolve().is_relative_to(BASE.resolve())
        self.addCleanup(self.tmp.cleanup)

    def run_demo(self):
        result = build_demo(self.root / 'workspace')
        self.workspace = self.root / 'workspace'
        return result

    def test_complete_demo_has_real_version_transitions(self):
        result = self.run_demo()
        self.assertEqual([s['report']['pending_count'] for s in result['stages']], [2, 0, 1])
        self.assertTrue(result['oldReviewRejected'])
        self.assertNotIn(str(BASE.resolve()), json.dumps(result))

    def test_assistant_and_automatic_prompt_not_user_facts(self):
        self.run_demo()
        with closing(sqlite3.connect(self.workspace / 'memory/prompt-intake/state.sqlite3')) as db:
            rows = db.execute("SELECT text FROM messages WHERE status='cached'").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertIn('第一阶段', rows[0][0])

    def test_repeated_import_is_idempotent(self):
        self.run_demo()
        stats = intake([BASE / 'examples/sessions'], self.workspace / 'memory/prompt-intake')
        self.assertEqual(stats['inserted'], 0)

    def test_partial_jsonl_waits_and_incremental_append_completes(self):
        source = self.root / 'input'
        source.mkdir()
        path = source / 'sample.jsonl'
        meta = {'type': 'session_meta', 'payload': {'id': THREAD}}
        event = {'type': 'response_item', 'timestamp': '2026-01-02T00:00:00Z', 'payload': {
            'role': 'user', 'content': [{'type': 'input_text', 'text': '虚构追加记录'}]}}
        text = json.dumps(event)
        path.write_text(json.dumps(meta)+'\n'+text[:20], encoding='utf-8')
        first = intake([source], self.root / 'output')
        self.assertEqual(first['partial'], 1)
        self.assertEqual(first['inserted'], 0)
        with path.open('a', encoding='utf-8') as stream:
            stream.write(text[20:]+'\n')
        second = intake([source], self.root / 'output')
        self.assertEqual(second['inserted'], 1)

    def test_source_output_overlap_rejected(self):
        source = self.root / 'input'
        source.mkdir()
        with self.assertRaises(ValueError):
            intake([source], source / 'output')

    def test_known_prompt_wrapper_is_skipped(self):
        self.assertEqual(clean_text('<heartbeat>automatic</heartbeat>')[1], 'skipped_injection')

    def test_credential_like_value_is_redacted(self):
        token_like = 'sk-' + 'x' * 20
        text, state = clean_text('A fictional credential: '+token_like)
        self.assertEqual(state, 'redacted')
        self.assertNotIn(token_like, text)

    def test_changed_source_invalidates_reading(self):
        self.run_demo()
        report, code = run(self.workspace, 'check')
        self.assertEqual(code, 2)
        self.assertEqual(report['pending_count'], 1)
        self.assertEqual(next(r for r in report['sources'] if r['status']=='requires_review')['id'], 'file:reading-plan')

    def test_seal_rejects_missing_and_outside_outputs(self):
        self.run_demo()
        config = load(self.workspace / 'memory/continuity/config.json')
        sources = collect(self.workspace, config)
        source = sources['file:reading-plan']
        plan = {'reviews': [{'id': source['id'], 'expected_sha256': source['sha256'],
                            'disposition': 'state_updated', 'note': '虚构核读', 'outputs': ['missing.md']}]}
        with self.assertRaises(ValueError):
            seal(self.workspace, sources, plan, {})
        outside = self.workspace.parent / 'outside.md'
        outside.write_text('outside', encoding='utf-8')
        plan['reviews'][0]['outputs'] = ['../outside.md']
        with self.assertRaises(ValueError):
            seal(self.workspace, sources, plan, {})

    def test_seal_checks_reviewed_source_fingerprint(self):
        self.run_demo()
        sources = collect(self.workspace, load(self.workspace / 'memory/continuity/config.json'))
        plan = load(self.workspace / 'demo-review-plan.json')
        with self.assertRaisesRegex(ValueError, '版本'):
            seal(self.workspace, sources, plan, {})

    def test_tag_and_link_drift_detected(self):
        self.run_demo()
        bad = self.workspace / 'notes/bad.md'
        bad.write_text('tags: `unregistered`\n\n[缺失](missing.md)', encoding='utf-8')
        errors = validate(self.workspace)
        self.assertTrue(any('unknown tag' in e for e in errors))
        self.assertTrue(any('unresolved local link' in e for e in errors))

    def test_graph_endpoint_and_source_checked(self):
        self.run_demo()
        save(self.workspace / 'knowledge-graph.json', {'nodes': [{'id': 'a'}],
             'edges': [{'from': 'a', 'to': 'missing', 'kind': 'fact'}]})
        errors = validate(self.workspace)
        self.assertEqual(len(errors), 2)

if __name__ == '__main__':
    unittest.main()
