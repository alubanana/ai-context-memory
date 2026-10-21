"""增量上下文核读门禁。只生成差异；状态判断由核读者写入并签收。"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import uuid

from prompt_intake import clean_text

ROOT = Path(__file__).resolve().parents[1]


def digest(value):
    return hashlib.sha256(value).hexdigest()


def load(path, default=None):
    return json.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else default


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    tmp.replace(path)


def stamp():
    return datetime.now(timezone.utc).isoformat()


def collect(root, config):
    """只读来源。消息集合散列不含刷新时间，重复运行不产生伪变化。"""
    dbpath = root / 'memory/prompt-intake/state.sqlite3'
    if not dbpath.exists():
        raise ValueError('消息索引不存在；先运行 prompt_intake.py')
    sources = {}
    db = sqlite3.connect(dbpath.resolve().as_uri() + '?mode=ro', uri=True)
    try:
        for thread, label in config['threads'].items():
            messages = []
            for key, ts, text in db.execute(
                "SELECT key,timestamp,text FROM messages WHERE thread=? AND timestamp>=? "
                "AND status IN ('cached','redacted') ORDER BY timestamp,key",
                (thread, config['since'])):
                text, status = clean_text(text)
                if not text or status == 'skipped_injection':
                    continue
                messages.append({'key': key, 'time': ts, 'text': text})
            payload = json.dumps(messages, ensure_ascii=False, sort_keys=True).encode('utf-8')
            ident = 'thread:' + thread
            sources[ident] = dict(id=ident, owner=label['owner'], title=label['title'],
                kind='messages', sha256=digest(payload), count=len(messages),
                latest=messages[-1]['time'] if messages else None,
                path=f'memory/prompt-intake/{thread}.md', messages=messages)
        # 未登记的新对话必须进入分流；不因没有人工路由而静默遗漏。
        for (thread,) in db.execute("SELECT DISTINCT thread FROM messages WHERE timestamp>=?", (config['since'],)):
            if thread not in config['threads'] and thread not in config.get('generated_threads',{}):
                count = 0
                for (text,) in db.execute("SELECT text FROM messages WHERE thread=? AND timestamp>=? AND status IN ('cached','redacted')", (thread,config['since'])):
                    cleaned, _ = clean_text(text)
                    count += bool(cleaned)
                if count:
                    ident='unrouted:'+thread
                    sources[ident] = dict(id=ident, owner='inbox', title='待分流对话',
                        kind='unrouted', sha256=None, count=count,
                        path=f'memory/prompt-intake/{thread}.md')
    finally:
        db.close()
    for item in config.get('files', []):
        path = Path(item['path'])
        if not path.is_absolute():
            path = root / path
        ident = 'file:' + item['id']
        sources[ident] = dict(id=ident, owner=item['owner'], title=item['title'], kind='file',
            sha256=digest(path.read_bytes()) if path.is_file() else None,
            path=str(path), missing=not path.is_file())
    return sources


def assess(root, sources, reviews, topic=None):
    rows = []
    for ident, source in sources.items():
        if topic and source['owner'] not in (topic, 'inbox'):
            continue
        record = reviews.get(ident, {})
        reason = None
        if source['kind'] == 'unrouted':
            reason = 'unrouted'
        elif source.get('missing'):
            reason = 'missing_source'
        elif not record:
            reason = 'not_reviewed'
        elif record.get('sha256') != source['sha256']:
            reason = 'changed_requires_reading'
        else:
            for output, expected in record.get('outputs', {}).items():
                path = root / output
                if not path.is_file():
                    reason='missing_state_output'
                    break
                # 输出更改可来自合法后续回写；不把它伪装成来源已重读。
                if not expected:
                    reason='unsigned_output'
        row={k:v for k,v in source.items() if k not in ('messages','changes')}
        row['status']='requires_review' if reason else 'reviewed'
        row['reason']=reason
        row['last_reviewed_at']=record.get('reviewed_at')
        row['disposition']=record.get('disposition')
        row['note']=record.get('note')
        rows.append(row)
    return rows


def seal(root, sources, plan, previous):
    """只签收人工指定版本。来源在核读后变化、输出缺失均拒绝。"""
    records = dict(previous)
    for entry in plan['reviews']:
        ident=entry['id']
        if ident not in sources or sources[ident]['kind']=='unrouted':
            raise ValueError('来源未登记，先分流：'+ident)
        if not entry.get('expected_sha256') or entry['expected_sha256'] != sources[ident]['sha256']:
            raise ValueError('来源版本已改变或未提供核读指纹：'+ident)
        if entry.get('disposition') not in ('state_updated','no_state_change','historical_context'):
            raise ValueError('无效核读结论：'+ident)
        if not entry.get('note') or not entry.get('outputs'):
            raise ValueError('必须记录核读结果及可恢复的状态文件：'+ident)
        outputs={}
        for name in entry['outputs']:
            path=(root/name).resolve()
            if not path.is_relative_to(root.resolve()) or not path.is_file():
                raise ValueError('状态输出不存在或不在共享工作区：'+name)
            outputs[name]=digest(path.read_bytes())
        records[ident]=dict(sha256=sources[ident]['sha256'], reviewed_at=stamp(),
            disposition=entry['disposition'], note=entry['note'], outputs=outputs)
    return records


def run(root, command, topic=None, plan=None, source_id=None):
    base=root/'memory/continuity'
    config=load(base/'config.json')
    if not config:
        raise ValueError('缺少 continuity/config.json')
    sources=collect(root,config)
    previous=load(base/'reviews.json',{})
    if command=='show':
        if source_id not in sources:
            raise ValueError('未知来源')
        return sources[source_id],0
    if command=='seal':
        review_plan=load(plan)
        previous=seal(root,sources,review_plan,previous)
        save(base/'reviews.json',previous)
    rows=assess(root,sources,previous,topic)
    pending=[r for r in rows if r['status']=='requires_review']
    refresh=load(root/'memory/source-refresh.json',{})
    refresh_issue=None
    try:
        age=(datetime.now(timezone.utc)-datetime.fromisoformat(refresh['finished_at'])).total_seconds()/3600
        if refresh.get('status')!='ok' or age>24:
            refresh_issue='refresh_failed_or_older_than_24h'
    except (KeyError,ValueError,TypeError):
        refresh_issue='refresh_not_verified'
    report=dict(checked_at=stamp(),status='requires_review' if pending or refresh_issue else 'current',
        since=config['since'], topic=topic, source_refresh_issue=refresh_issue,
        pending_count=len(pending), by_owner=dict(Counter(r['owner'] for r in pending)), sources=rows,
        scope='配置中的消息与资料；新对话进入待分流。来源已索引不等于核读，核读不等于稳定人格结论。')
    if command in ('scan','seal'):
        save(base/'queue.json',report)
    return report,2 if command=='check' and report['status']!='current' else 0


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['scan','check','show','seal'])
    parser.add_argument('--root',type=Path,default=ROOT/'runtime')
    parser.add_argument('--topic')
    parser.add_argument('--plan',type=Path)
    parser.add_argument('--id',dest='source_id')
    parser.add_argument('--brief',action='store_true')
    args=parser.parse_args()
    if args.command=='seal' and not args.plan:
        parser.error('seal 需要 --plan')
    try:
        report,code=run(args.root,args.command,args.topic,args.plan,args.source_id)
        if args.brief and 'sources' in report:
            report={k:v for k,v in report.items() if k!='sources'}
        print(json.dumps(report,ensure_ascii=False,indent=2))
        return code
    except (ValueError,OSError,sqlite3.Error) as exc:
        print('continuity failed: '+str(exc),file=sys.stderr)
        return 1


if __name__=='__main__':
    if hasattr(sys.stdout,'reconfigure'):sys.stdout.reconfigure(encoding='utf-8')
    raise SystemExit(main())
