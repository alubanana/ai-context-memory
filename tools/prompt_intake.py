"""Incremental user-message intake for exported JSONL sessions. Standard library only."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import time
import os
from pathlib import Path

UUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")
WRAPPERS = (
    "environment_context", "app-context", "in_app_context", "in-app-context",
    "codex_delegation", "recommended_plugins", "permissions", "permissions_instructions",
    "skills_instructions", "collaboration_mode", "system_reminder", "system-reminder",
    "INSTRUCTIONS", "heartbeat", "codex_internal_context", "subagent_notification",
    "external_codex_apps_open_page", "in-app-browser-context", "turn_aborted",
)
CLEAN_VERSION = "2026-10-16-continuity-v4"
SENSITIVE = re.compile(
    r"(?im)(?:\b(?:password|passwd|pwd|api[_ -]?key|access[_ -]?token|token|secret|cvv|cvc)\b"
    r"|密码|口令|验证码|身份证(?:号)?|银行卡(?:号)?)\s*[:=：]\s*[^\r\n]+"
    r"|\b(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9]{15,}|Bearer\s+[A-Za-z0-9._~+/-]{12,})"
    r"|\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"
    r"|(?<!\d)\d{17}[\dXx](?!\w)"
    r"|(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)"
)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def clean_text(value: str) -> tuple[str, str]:
    """Remove recognized injected blocks, without paraphrasing user prose."""
    text = value
    if text.lstrip().startswith('The following is the Codex agent history') or '>>> APPROVAL REQUEST END' in text:
        return '', 'skipped_injection'
    if re.match(r'\s*\[\d+\] (?:tool [^\n]+ (?:call|result):|assistant:)',text):
        return '', 'skipped_injection'
    for tag in WRAPPERS:
        text = re.sub(r"<" + re.escape(tag) + r"\b[^>]*>.*?</" + re.escape(tag) + r"\s*>",
                      "", text, flags=re.S | re.I)
    # AGENTS header precedes an INSTRUCTIONS block in native session logs.
    text = re.sub(r"(?m)^# AGENTS\.md instructions(?: for [^\r\n]*)?\s*\n", "", text)
    # A truncated/unknown injection is safer to skip than to attribute to the user.
    if re.search(r"<(?:" + "|".join(map(re.escape, WRAPPERS)) + r")\b", text, re.I):
        return "", "skipped_injection"
    if re.match(r"\s*(?:You are (?:ChatGPT|Codex)|# (?:System instructions|AGENTS\.md)|"
                r"<system\b|\[codex_delegation\])", text, re.I):
        return "", "skipped_injection"
    text = text.strip("\r\n")
    if not text.strip():
        return "", "skipped_injection"
    text, count = SENSITIVE.subn("[REDACTED]", text)
    return text, "redacted" if count else "cached"


def user_text(event: dict) -> tuple[str, str | None] | None:
    if event.get("type") != "response_item":
        return None
    item = event.get("payload")
    if not isinstance(item, dict) or item.get("role") != "user":
        return None
    content = item.get("content", [])
    if isinstance(content, str):
        return content, item.get("id") or event.get("id")
    parts = []
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get("type") in ("input_text", "text"):
                if isinstance(part.get("text"), str):
                    parts.append(part["text"])
    return ("\n".join(parts), item.get("id") or event.get("id")) if parts else None


def open_db(output: Path) -> sqlite3.Connection:
    output.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(output / "state.sqlite3")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS files (
            path TEXT PRIMARY KEY, bytes INTEGER, offset INTEGER, line INTEGER,
            prefix_sha TEXT, mtime_ns INTEGER, thread TEXT);
        CREATE TABLE IF NOT EXISTS messages (
            key TEXT PRIMARY KEY, thread TEXT, timestamp TEXT, sha TEXT,
            status TEXT, text TEXT);
        CREATE TABLE IF NOT EXISTS sources (
            key TEXT, path TEXT, line INTEGER, offset INTEGER, sha TEXT,
            PRIMARY KEY(key,path,line,offset,sha));
        CREATE TABLE IF NOT EXISTS intake_meta (key TEXT PRIMARY KEY, value TEXT);
    """)
    return db


def digest_prefix(stream, length: int) -> str:
    digest = hashlib.sha256()
    remaining = length
    while remaining:
        data = stream.read(min(1024 * 1024, remaining))
        if not data:
            break
        remaining -= len(data)
        digest.update(data)
    return digest.hexdigest()


def render(db: sqlite3.Connection, output: Path, thread: str) -> None:
    lines = [f"# Thread {thread}", "", "authorship: user_message_authorship_unverified", ""]
    for key, timestamp, digest, status, text in db.execute(
        "SELECT key,timestamp,sha,status,text FROM messages WHERE thread=? ORDER BY timestamp,key", (thread,)
    ):
        lines.extend([f"## {timestamp or 'unknown_time'}", "", f"status: {status}",
                      f"sha256: {digest}", "authorship: user_message_authorship_unverified"])
        for path, line, offset, source_sha in db.execute(
            "SELECT path,line,offset,sha FROM sources WHERE key=? ORDER BY path,line", (key,)
        ):
            lines.append("source: " + json.dumps({"path": path, "line": line, "byte_offset": offset,
                                                 "sha256": source_sha}, ensure_ascii=False))
        if text:
            # Fences keep pasted Markdown/HTML as inert original text.
            fence = "`" * max(3, max((len(m[0]) + 1 for m in re.finditer(r"`+", text)), default=3))
            lines.extend(["", fence + "text", text, fence])
        lines.append("")
    target = output / f"{thread}.md"
    temporary = output / f"{thread}.md.tmp"
    temporary.write_text("\n".join(lines), encoding="utf-8")
    temporary.replace(target)


def intake(roots: list[Path], output: Path) -> dict[str, int]:
    """串行化写入与渲染，允许多个中控同时请求最新消息。"""
    output=output.resolve()
    if any(output==p.resolve() or output.is_relative_to(p.resolve()) or p.resolve().is_relative_to(output) for p in roots):
        raise ValueError('Output and source roots must not overlap')
    output.mkdir(parents=True,exist_ok=True)
    lock=output/'intake.lock'
    deadline=time.monotonic()+20
    while True:
        try:
            fd=os.open(lock,os.O_CREAT|os.O_EXCL|os.O_WRONLY)
            break
        except FileExistsError:
            if time.monotonic()>=deadline:
                raise ValueError('消息索引正在写入或锁待核查；本次不宣称已同步：'+str(lock))
            time.sleep(0.1)
    try:
        os.write(fd,str(os.getpid()).encode('ascii'))
        os.close(fd)
        return _intake(roots,output)
    finally:
        lock.unlink(missing_ok=True)


def _intake(roots: list[Path], output: Path) -> dict[str, int]:
    roots = [p.resolve() for p in roots]
    output = output.resolve()
    if any(output == p or output.is_relative_to(p) or p.is_relative_to(output) for p in roots):
        raise ValueError("Output and source roots must not overlap")
    missing = [str(p) for p in roots if not p.is_dir()]
    if missing:
        raise ValueError("Source roots do not exist: " + ", ".join(missing))
    stats = dict(files=0, unchanged=0, reset=0, inserted=0, duplicates=0, malformed=0, partial=0, recleaned=0)
    db = open_db(output)
    try:
        # 修复已缓存的自动提示；原始会话和原文指纹保持不变。
        version = db.execute("SELECT value FROM intake_meta WHERE key='clean_version'").fetchone()
        if not version or version[0] != CLEAN_VERSION:
            with db:
                for key, old_text, old_status in db.execute("SELECT key,text,status FROM messages").fetchall():
                    if not old_text:
                        continue
                    cleaned, status = clean_text(old_text)
                    if old_status == "redacted" and status == "cached":
                        status = "redacted"
                    if cleaned != old_text or status != old_status:
                        db.execute("UPDATE messages SET text=?,status=? WHERE key=?", (cleaned,status,key))
                        stats["recleaned"] += 1
                db.execute("INSERT OR REPLACE INTO intake_meta VALUES ('clean_version',?)", (CLEAN_VERSION,))
        for path in sorted({p.resolve() for root in roots for p in root.rglob("*.jsonl")}):
            stat = path.stat()
            old = db.execute("SELECT bytes,offset,line,prefix_sha,mtime_ns,thread FROM files WHERE path=?",
                             (str(path),)).fetchone()
            if old and old[0] == stat.st_size and old[4] == stat.st_mtime_ns:
                stats["unchanged"] += 1
                continue
            stats["files"] += 1
            offset, line, thread = 0, 0, None
            match = UUID.search(path.stem)
            if match:
                thread = match[0].lower()
            with path.open("rb") as stream, db:
                if old:
                    if stat.st_size >= old[1] and digest_prefix(stream, old[1]) == old[3]:
                        offset, line, thread = old[1], old[2], old[5]
                    else:
                        stats["reset"] += 1
                stream.seek(offset)
                while True:
                    start = stream.tell()
                    raw = stream.readline()
                    if not raw:
                        break
                    if not raw.endswith(b"\n"):
                        stats["partial"] += 1
                        break
                    line += 1
                    offset = stream.tell()
                    try:
                        event = json.loads(raw)
                    except (ValueError, UnicodeDecodeError):
                        stats["malformed"] += 1
                        continue
                    if not isinstance(event, dict):
                        stats["malformed"] += 1
                        continue
                    if event.get("type") == "session_meta":
                        payload = event.get("payload", {})
                        candidate = payload.get("id", "") if isinstance(payload, dict) else ""
                        if isinstance(candidate, str) and UUID.fullmatch(candidate):
                            thread = candidate.lower()
                        continue
                    extracted = user_text(event)
                    if extracted is None or thread is None:
                        continue
                    original, message_id = extracted
                    digest = sha(original.encode("utf-8"))
                    timestamp = event.get("timestamp", "")
                    timestamp = timestamp if isinstance(timestamp, str) else ""
                    # Content hash distinguishes revisions of a reused message ID.
                    identity = [thread, str(message_id) if message_id else timestamp, digest]
                    key = sha(json.dumps(identity, ensure_ascii=False).encode("utf-8"))
                    text, status = clean_text(original)
                    cursor = db.execute("INSERT OR IGNORE INTO messages VALUES (?,?,?,?,?,?)",
                                        (key, thread, timestamp, digest, status, text))
                    stats["inserted" if cursor.rowcount else "duplicates"] += 1
                    db.execute("INSERT OR IGNORE INTO sources VALUES (?,?,?,?,?)",
                               (key, str(path), line, start, sha(raw)))
                stream.seek(0)
                prefix_sha = digest_prefix(stream, offset)
                db.execute("INSERT OR REPLACE INTO files VALUES (?,?,?,?,?,?,?)",
                           (str(path), stat.st_size, offset, line, prefix_sha, stat.st_mtime_ns, thread))
        # Rebuild derived output: also repairs an interrupted previous render.
        for (thread,) in db.execute("SELECT DISTINCT thread FROM messages"):
            render(db, output, thread)
    finally:
        db.close()
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--roots", nargs="+", type=Path,
                        required=True)
    parser.add_argument("--output", type=Path, default=Path("runtime/memory/prompt-intake"))
    args = parser.parse_args()
    try:
        print(json.dumps(intake(args.roots, args.output), ensure_ascii=False))
    except (ValueError, OSError, sqlite3.Error) as exc:
        parser.exit(1, f"prompt intake failed: {exc}\n")


if __name__ == "__main__":
    main()
