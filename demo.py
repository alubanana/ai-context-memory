"""Run a fictional example through the actual import and review pipeline."""
import json
from pathlib import Path
import shutil
import sys

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE / 'tools'))
from prompt_intake import intake
from continuity import collect, load, run, save, seal, stamp
from knowledge_gate import validate

def build_demo(runtime=None):
    root = Path(runtime) if runtime else BASE / 'runtime'
    # Copy only four explicitly enumerated synthetic inputs; never a user workspace.
    names = ['memory/continuity/config.json', 'memory/classification/tags.json',
             'notes/research-plan.md', 'notes/acceptance.md']
    for name in names:
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(BASE / 'examples/workspace' / name, target)
    (root / 'memory/continuity/reviews.json').unlink(missing_ok=True)
    save(root / 'memory/source-refresh.json', {'status': 'ok', 'finished_at': stamp()})
    intake_stats = intake([BASE / 'examples/sessions'], root / 'memory/prompt-intake')
    first, _ = run(root, 'scan')
    output = root / 'state/current.md'
    output.parent.mkdir(exist_ok=True)
    output.write_text('# 科普阅读器当前状态\n\n示范核读：第一阶段验证来源追溯，摘要风格比较在第二阶段进行。\n', encoding='utf-8')
    sources = collect(root, load(root / 'memory/continuity/config.json'))
    plan = {'reviews': [{'id': k, 'expected_sha256': v['sha256'], 'disposition': 'state_updated',
                        'note': '已核对讨论与计划，第一阶段范围一致。',
                        'outputs': ['state/current.md']} for k, v in sources.items()]}
    save(root / 'demo-review-plan.json', plan)
    reviewed, _ = run(root, 'seal', plan=root / 'demo-review-plan.json')
    note = root / 'notes/research-plan.md'
    note.write_text(note.read_text(encoding='utf-8')+'\n虚构更新：新增原文段落高亮的验收条件。\n', encoding='utf-8')
    changed, _ = run(root, 'scan')
    rejected = False
    try:
        seal(root, collect(root, load(root / 'memory/continuity/config.json')), plan, load(root / 'memory/continuity/reviews.json'))
    except ValueError:
        rejected = True
    issues = validate(root)
    if issues or not rejected:
        raise ValueError({'gate': issues, 'old_review_rejected': rejected})
    reports = {'title': '个人AI助手记忆与知识管理', 'synthetic': True,
               'intake': intake_stats, 'oldReviewRejected': rejected,
               'stages': [{'name': '首次扫描', 'report': first},
                          {'name': '核读签收', 'report': reviewed},
                          {'name': '来源更新', 'report': changed}]}
    # Public demo records use logical relative paths; SQLite runtime stays ignored.
    def relative(value):
        if isinstance(value, dict):
            return {k: relative(v) for k, v in value.items()}
        if isinstance(value, list):
            return [relative(v) for v in value]
        if isinstance(value, str):
            for prefix in (str(root.resolve()), str(BASE.resolve())):
                value = value.replace(prefix + '\\', '').replace(prefix + '/', '')
            return value.replace('\\', '/')
        return value
    return relative(reports)

if __name__ == '__main__':
    result = build_demo()
    payload = json.dumps(result, ensure_ascii=False).replace('<', '\\u003c')
    template = (BASE / 'web/demo.html').read_text(encoding='utf-8')
    (BASE / 'docs').mkdir(exist_ok=True)
    (BASE / 'docs/index.html').write_text(template.replace('__DEMO_DATA__', payload), encoding='utf-8')
    print(json.dumps({'stages': [s['report']['pending_count'] for s in result['stages']],
                      'old_review_rejected': result['oldReviewRejected'], 'output': 'docs/index.html'}))
