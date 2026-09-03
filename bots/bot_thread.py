#!/usr/bin/env python3
"""bot_thread.py — Hermes Bot Team Chat v1 编排器 (外部, 零 core 改动)

设计: ~/.hermes/plans/bot-team-chat-design.md Phase 1 v1
用法:
  bot_thread.py create <name> --members p1,p2          建线程
  bot_thread.py say <thread> "人类消息"                 人类发消息 (role=human)
  bot_thread.py ask <thread> "@legal-advisor 问题"      @mention 触发 bot 回合
  bot_thread.py show <thread> [-n 20]                   看 transcript
  bot_thread.py list                                    列线程

锁语义 (Cumora HELD 本地版): threads.version CAS, 冲突重试一次后报 HELD。
Phase 0 实证要点: profile 不继承 ~/.hermes/.env, 子进程必须显式注入。
"""
import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

DB_DIR = Path.home() / ".hermes" / "bot-threads"
DB_PATH = DB_DIR / "threads.db"
ENV_FILE = Path.home() / ".hermes" / ".env"
PROFILES_DIR = Path.home() / ".hermes" / "profiles"

WINDOW_N = 20          # transcript 滚动窗口条数
WINDOW_CHARS = 8000    # 窗口总字符上限
MSG_TRUNC = 1500       # 单条消息截断
TURN_TIMEOUT = 600     # bot 回合子进程超时 (legal-advisor M5 重试可烧 300s+)
HELD = "HELD: 线程在你作答期间被更新, 已重读重试一次仍冲突, 请重新发起"


def db() -> sqlite3.Connection:
    DB_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS threads(
        id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL,
        members TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 0,
        created_at REAL NOT NULL)"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS messages(
        id INTEGER PRIMARY KEY, thread_id INTEGER NOT NULL REFERENCES threads(id),
        seq INTEGER NOT NULL, author TEXT NOT NULL, role TEXT NOT NULL,
        content TEXT NOT NULL, created_at REAL NOT NULL,
        model TEXT, input_tokens INTEGER, output_tokens INTEGER,
        duration_ms INTEGER, warm INTEGER)"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS sessions(
        thread_id INTEGER NOT NULL, profile TEXT NOT NULL,
        session_id TEXT NOT NULL, updated_at REAL NOT NULL,
        PRIMARY KEY(thread_id, profile))"""
    )
    # P3 自主权: 每线程自动接续阈值 + 自动行动战绩
    conn.execute(
        """CREATE TABLE IF NOT EXISTS thread_config(
        thread_id INTEGER PRIMARY KEY, max_auto_chain INTEGER NOT NULL DEFAULT 0)"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS auto_actions(
        id INTEGER PRIMARY KEY, thread_id INTEGER NOT NULL, ts REAL NOT NULL,
        actor TEXT NOT NULL, target TEXT NOT NULL, trigger_seq INTEGER NOT NULL,
        depth INTEGER NOT NULL, status TEXT NOT NULL)"""
    )
    # R 复刻模块: 看板/文档/日程/项目/公司/投票/发布 (Cumora 0.14.2 功能区)
    for ddl in (
        "CREATE TABLE IF NOT EXISTS boards(id INTEGER PRIMARY KEY, name TEXT NOT NULL, pos INTEGER DEFAULT 0)",
        "CREATE TABLE IF NOT EXISTS board_cards(id INTEGER PRIMARY KEY, board_id INTEGER NOT NULL, title TEXT NOT NULL, note TEXT DEFAULT '', pos INTEGER DEFAULT 0)",
        "CREATE TABLE IF NOT EXISTS docs(id INTEGER PRIMARY KEY, title TEXT NOT NULL, body TEXT DEFAULT '', updated_at REAL NOT NULL)",
        "CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, title TEXT NOT NULL, ts REAL NOT NULL, note TEXT DEFAULT '')",
        "CREATE TABLE IF NOT EXISTS projects(id INTEGER PRIMARY KEY, name TEXT NOT NULL, status TEXT DEFAULT '进行中', note TEXT DEFAULT '')",
        "CREATE TABLE IF NOT EXISTS companies(id INTEGER PRIMARY KEY, name TEXT NOT NULL, note TEXT DEFAULT '')",
        "CREATE TABLE IF NOT EXISTS polls(id INTEGER PRIMARY KEY, question TEXT NOT NULL, options TEXT NOT NULL DEFAULT '[]', votes TEXT NOT NULL DEFAULT '{}', closed INTEGER DEFAULT 0)",
        "CREATE TABLE IF NOT EXISTS releases(id INTEGER PRIMARY KEY, version TEXT NOT NULL, commit_sha TEXT DEFAULT '', notes TEXT DEFAULT '', rollback TEXT DEFAULT '', baseline TEXT DEFAULT '', status TEXT DEFAULT '灰度', created_at REAL NOT NULL)",
    ):
        conn.execute(ddl)
    # P1 迁移: 老库 messages 表补元数据列 (重复列报错则忽略)
    for col in ("model TEXT", "input_tokens INTEGER", "output_tokens INTEGER",
                "duration_ms INTEGER", "warm INTEGER"):
        try:
            conn.execute(f"ALTER TABLE messages ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass
    return conn


def load_env() -> dict:
    """合并 ~/.hermes/.env 进子进程 env (不覆盖已存在的键)。"""
    env = dict(os.environ)
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    return env


def check_profile(name: str) -> None:
    if name != "default" and not (PROFILES_DIR / name).is_dir():
        raise SystemExit(f"profile 不存在: {name} (查 {PROFILES_DIR})")


def append(conn, thread_id: int, author: str, role: str, content: str,
           max_retry: int = 1, meta=None) -> int:
    """CAS 追加消息。冲突重试 max_retry 次后 raise HELD。"""
    meta = meta or {}
    for attempt in range(max_retry + 1):
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT version FROM threads WHERE id=?", (thread_id,)
            ).fetchone()
            if row is None:
                conn.rollback()
                raise SystemExit(f"线程不存在: id={thread_id}")
            version = row[0]
            seq = conn.execute(
                "SELECT COALESCE(MAX(seq),0)+1 FROM messages WHERE thread_id=?",
                (thread_id,),
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO messages(thread_id,seq,author,role,content,created_at,"
                "model,input_tokens,output_tokens,duration_ms,warm)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (thread_id, seq, author, role, content, time.time(),
                 meta.get("model"), meta.get("input_tokens"),
                 meta.get("output_tokens"), meta.get("duration_ms"),
                 meta.get("warm")),
            )
            cur = conn.execute(
                "UPDATE threads SET version=version+1 WHERE id=? AND version=?",
                (thread_id, version),
            )
            if cur.rowcount == 1:
                conn.commit()
                return seq
            conn.rollback()
        except sqlite3.Error:
            conn.rollback()
            raise
    raise SystemExit(HELD)


def get_thread(conn, name: str):
    row = conn.execute(
        "SELECT id,name,members,version FROM threads WHERE name=?", (name,)
    ).fetchone()
    if row is None:
        raise SystemExit(f"线程不存在: {name}")
    return row


def transcript_window(conn, thread_id: int) -> str:
    rows = conn.execute(
        "SELECT author,role,content FROM messages WHERE thread_id=?"
        " ORDER BY seq DESC LIMIT ?", (thread_id, WINDOW_N),
    ).fetchall()
    rows.reverse()
    parts, total = [], 0
    for author, role, content in rows:
        piece = f"[{author}|{role}] {content[:MSG_TRUNC]}"
        if total + len(piece) > WINDOW_CHARS:
            break
        parts.append(piece)
        total += len(piece)
    return "\n".join(parts)


BOT_PROMPT = """【团队线程 "{thread}" · 共享 transcript】
{transcript}

【你的任务】你是本线程里的 {profile}。请以上线程为背景给出你的回复(线程中的下一条消息)。按你的人格设定作答, 不要解释你在做什么。"""


def _find_key(obj, key):
    """在嵌套 dict/list 里递归找 key (usage-file 结构不保证, 宽容解析)。"""
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            r = _find_key(v, key)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _find_key(v, key)
            if r is not None:
                return r
    return None


def bot_turn(profile: str, thread_name: str, window: str,
             resume_id=None):
    """跑一个 bot 回合。返回 (reply, meta)。
    meta: model/input_tokens/output_tokens/duration_ms/warm/session_id。
    P1  observability: --usage-file 落 model+tokens。
    P2  暖启动: 传 resume_id 则 --resume, 省每回合冷启动重载。"""
    prompt = BOT_PROMPT.format(thread=thread_name, transcript=window, profile=profile)
    usage_path = f"/tmp/bot-thread-usage-{os.getpid()}-{int(time.time() * 1000)}.json"
    cmd = ["hermes", "-p", profile, "-z", prompt, "--usage-file", usage_path]
    if resume_id:
        cmd += ["--resume", resume_id]
    t0 = time.time()
    r = subprocess.run(
        cmd, capture_output=True, text=True,
        timeout=TURN_TIMEOUT, env=load_env(),
    )
    duration_ms = int((time.time() - t0) * 1000)
    if r.returncode != 0:
        raise SystemExit(f"bot 回合失败: {profile}\nstderr: {r.stderr[-500:]}")
    meta = {
        "duration_ms": duration_ms,
        "warm": 1 if resume_id else 0,
    }
    try:
        usage = json.loads(Path(usage_path).read_text())
        for k in ("model", "session_id", "input_tokens", "output_tokens"):
            v = _find_key(usage, k)
            if v is not None:
                meta[k] = v
    except Exception:
        pass
    finally:
        Path(usage_path).unlink(missing_ok=True)
    return r.stdout.strip(), meta


def get_session(conn, thread_id: int, profile: str):
    row = conn.execute(
        "SELECT session_id FROM sessions WHERE thread_id=? AND profile=?",
        (thread_id, profile),
    ).fetchone()
    return row[0] if row else None


def put_session(conn, thread_id: int, profile: str, session_id: str):
    conn.execute(
        "INSERT INTO sessions(thread_id,profile,session_id,updated_at)"
        " VALUES(?,?,?,?)"
        " ON CONFLICT(thread_id,profile) DO UPDATE SET"
        " session_id=excluded.session_id, updated_at=excluded.updated_at",
        (thread_id, profile, session_id, time.time()),
    )
    conn.commit()


# ── P3 自主权: 自动接续阈值 + 战绩 ──

def get_auto_chain(conn, thread_id: int) -> int:
    row = conn.execute(
        "SELECT max_auto_chain FROM thread_config WHERE thread_id=?", (thread_id,)
    ).fetchone()
    return row[0] if row else 0


def set_auto_chain(conn, thread_id: int, n: int):
    conn.execute(
        "INSERT INTO thread_config(thread_id,max_auto_chain) VALUES(?,?)"
        " ON CONFLICT(thread_id) DO UPDATE SET max_auto_chain=excluded.max_auto_chain",
        (thread_id, n),
    )
    conn.commit()


def log_auto_action(conn, thread_id: int, actor: str, target: str,
                    trigger_seq: int, depth: int, status: str):
    conn.execute(
        "INSERT INTO auto_actions(thread_id,ts,actor,target,trigger_seq,depth,status)"
        " VALUES(?,?,?,?,?,?,?)",
        (thread_id, time.time(), actor, target, trigger_seq, depth, status),
    )
    conn.commit()


def auto_history(conn, thread_id: int, limit: int = 10):
    return conn.execute(
        "SELECT ts,actor,target,trigger_seq,depth,status FROM auto_actions"
        " WHERE thread_id=? ORDER BY id DESC LIMIT ?", (thread_id, limit),
    ).fetchall()


def run_turn(conn, tid: int, thread_name: str, profile: str, depth: int,
             max_depth: int, visited: set) -> None:
    """跑一个 bot 回合并落库; 若回复 @ 其他成员且深度允许, 自动接续 (Cumora agent 互唤等价物)。"""
    window = transcript_window(conn, tid)
    resume_id = get_session(conn, tid, profile)
    warm_tag = f" (resume {resume_id[:8]}…)" if resume_id else " (cold)"
    tag = "🔁 自动接续" if depth > 0 else "🤖"
    print(f"{tag} [{thread_name}] {profile} 作答中{warm_tag}...", flush=True)
    reply, meta = bot_turn(profile, thread_name, window, resume_id)
    seq = append(conn, tid, profile, "bot", reply, meta=meta)
    if meta.get("session_id"):
        put_session(conn, tid, profile, meta["session_id"])
    cost = ""
    if meta.get("output_tokens") is not None:
        cost = (f" [{meta.get('model','?')} | "
                f"in={meta.get('input_tokens')} out={meta.get('output_tokens')} | "
                f"{meta['duration_ms']}ms | {'暖' if meta['warm'] else '冷'}]")
    print(f"✅ [{thread_name}#{seq}]{cost}\n{reply}\n")

    if depth >= max_depth:
        return
    members = get_thread(conn, thread_name)[2].split(",")
    for target in parse_mentions(reply, members):
        if target == profile or target in visited:
            continue
        visited.add(target)
        log_auto_action(conn, tid, profile, target, seq, depth + 1, "completed")
        run_turn(conn, tid, thread_name, target, depth + 1, max_depth, visited)



def parse_mentions(text: str, members: list) -> list:
    found = re.findall(r"@([A-Za-z0-9_-]+)", text)
    unknown = [m for m in found if m not in members]
    if unknown:
        raise SystemExit(f"@mention 不是线程成员: {unknown} (成员: {members})")
    dedup, seen = [], set()
    for m in found:
        if m not in seen:
            dedup.append(m)
            seen.add(m)
    return dedup


def cmd_create(args):
    members = [m.strip() for m in args.members.split(",") if m.strip()]
    if not members:
        raise SystemExit("--members 不能为空")
    for m in members:
        check_profile(m)
    conn = db()
    try:
        conn.execute(
            "INSERT INTO threads(name,members,created_at) VALUES(?,?,?)",
            (args.name, ",".join(members), time.time()),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        raise SystemExit(f"线程已存在: {args.name}")
    print(f"✅ 线程 '{args.name}' 已建, 成员: {members}")


def cmd_say(args):
    conn = db()
    tid, _, _, _ = get_thread(conn, args.name)
    seq = append(conn, tid, args.as_, "human", args.text)
    print(f"✅ [{args.name}#{seq}] {args.as_} (human): {args.text[:80]}")


def cmd_ask(args):
    conn = db()
    tid, _, members_s, _ = get_thread(conn, args.name)
    members = members_s.split(",")
    mentions = parse_mentions(args.text, members)
    if not mentions:
        raise SystemExit("没有 @mention, v1 不猜测路由。请显式 @成员")
    append(conn, tid, "user", "human", args.text)
    max_depth = min(get_auto_chain(conn, tid), 3)  # 硬上限 3, 防失控
    visited = set(mentions)
    for profile in mentions:
        run_turn(conn, tid, args.name, profile, 0, max_depth, visited)


def cmd_chain(args):
    conn = db()
    tid, name, _, _ = get_thread(conn, args.name)
    if args.n is None:
        print(f"[{name}] max_auto_chain = {get_auto_chain(conn, tid)} (默认 0 = 关掉自动接续)")
        return
    if args.n < 0 or args.n > 3:
        raise SystemExit("n 范围 0-3")
    set_auto_chain(conn, tid, args.n)
    print(f"✅ [{name}] max_auto_chain = {args.n}")


def cmd_autonomy(args):
    conn = db()
    tid, name, _, _ = get_thread(conn, args.name)
    print(f"=== [{name}] 自主权 ===")
    print(f"max_auto_chain = {get_auto_chain(conn, tid)} (改: bot_thread.py chain {name} <0-3>)")
    rows = auto_history(conn, tid)
    print(f"\n自动行动战绩 (最近 {len(rows)} 条):")
    if not rows:
        print("  (无)")
    for ts, actor, target, seq, depth, status in rows:
        print(f"  {time.strftime('%m-%d %H:%M', time.localtime(ts))} "
              f"{actor} → @{target} (trigger #{seq}, depth {depth}, {status})")


# ── Phase 2 桌面 UI: 本地 Web 面板 (零依赖, preview 打开) ──
# 设计语言学自 Cumora 0.14.2 (asar CSS tokens 提取 2026-09-03):
# 命名空间 cloud/sky/paper/ink/coral/gold/whisper; 人=coral 暖, bot=sky 冷,
# 状态绿点=avail, 头像=彩色圆+首字母(+AI 肖像位), Manrope 字体, 8px圆角/999px药丸,
# 阴影带藏青 tint, 聊天区顶部天空 radial wash。深色模式自动跟 Hermes app 主题。

INDEX_HTML = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8"><title>Bot Team Chat</title>
<style>
/* Cumora tokens (light) */
:root {
  --paper: #FAFCFE; --cloud: #FFFFFF; --cloud-2: #F4F8FB;
  --skype: #00A8F0; --skype-deep: #0078C8; --skype-ink: #003B6F; --sky-glow: #4FC2F4;
  --coral: #FF7A6B; --coral-deep: #C84E3F; --coral-soft: #FFF0ED;
  --gold: #F4B740; --gold-deep: #BA8418; --whisper: #C678DD;
  --avail: #6EC56A;
  --ink-900: #1C1917; --ink-700: #44403C; --ink-500: #78716C;
  --ink-300: #D6D3D1; --ink-200: #E7E5E4; --ink-100: #F5F5F4;
  --shadow-soft: 0 12px 40px -8px rgba(10,30,60,.25);
  --shadow-ring: 0 0 0 1px rgba(10,30,60,.06);
  --grad-brand: linear-gradient(135deg, #0078C8, #003B6F);
  --grad-chrome: linear-gradient(180deg, #FBFDFF 0%, #F1F7FB 100%);
  --grad-rail: linear-gradient(180deg, #F8FBFD, #EDF4F9);
  --chat-wash: radial-gradient(ellipse 80% 40% at 0% 0%, rgba(194,230,251,.3), transparent);
}
* { box-sizing: border-box; }
body { font-family: Manrope, -apple-system, "PingFang SC", system-ui, sans-serif;
       margin: 0; display: flex; height: 100vh; color: var(--ink-900);
       background: var(--grad-chrome); font-size: 14px; }
/* ── 最左图标栏 (Cumora icon rail) ── */
#rail { width: 56px; background: var(--grad-rail); border-right: 1px solid var(--ink-200);
        display: flex; flex-direction: column; align-items: center;
        padding: 12px 0; gap: 6px; }
#rail .logo { width: 36px; height: 36px; border-radius: 10px; background: var(--grad-brand);
              color: #fff; display: flex; align-items: center; justify-content: center;
              font-size: 18px; margin-bottom: 10px; }
.navbtn { width: 40px; height: 40px; border-radius: 10px; border: none; background: transparent;
          color: var(--ink-500); cursor: pointer; display: flex; align-items: center;
          justify-content: center; }
.navbtn:hover { background: rgba(0,168,240,.1); color: var(--skype-deep); }
.navbtn.active { background: var(--skype); color: #fff;
                 box-shadow: 0 6px 14px -5px rgba(0,120,200,.5); }
.navbtn svg { width: 20px; height: 20px; }
#rail .spacer { flex: 1; }
/* ── 通用视图容器 ── */
.view { display: none; flex: 1; min-width: 0; }
.view.on { display: flex; }
.panel { flex: 1; display: flex; flex-direction: column; min-width: 0; }
.ph { padding: 14px 20px 10px; border-bottom: 1px solid var(--ink-200);
      background: var(--grad-rail); }
.ph h2 { margin: 0; font-family: Fraunces, Georgia, serif; font-size: 17px;
         color: var(--skype-ink); font-weight: 600; }
.ph .sub { font-size: 12px; color: var(--ink-500); margin-top: 2px; }
.pbody { flex: 1; overflow-y: auto; padding: 16px 20px; }
/* ── 聊天视图 ── */
#v-chat { }
#side { width: 220px; background: var(--grad-rail); border-right: 1px solid var(--ink-200);
        overflow-y: auto; padding: 12px 8px; display: flex; flex-direction: column; gap: 2px; }
.thread { display: flex; align-items: center; gap: 9px; padding: 8px;
          border-radius: 10px; cursor: pointer; }
.thread:hover { background: rgba(0,168,240,.08); }
.thread.active { background: var(--skype); box-shadow: 0 6px 16px -6px rgba(0,120,200,.5); }
.thread.active .tname, .thread.active .tsub { color: #fff; }
.thread .tname { font-size: 13.5px; font-weight: 600; overflow: hidden;
                 text-overflow: ellipsis; white-space: nowrap; }
.thread .tsub { font-size: 11px; color: var(--ink-500); }
#roster { display: flex; gap: 14px; padding: 10px 20px; border-bottom: 1px solid var(--ink-200);
          background: var(--cloud); align-items: center; overflow-x: auto; }
.member { display: flex; flex-direction: column; align-items: center; gap: 3px;
          font-size: 10.5px; color: var(--ink-500); min-width: 46px; }
#msgs { flex: 1; overflow-y: auto; padding: 18px 22px; }
.msg { display: flex; gap: 10px; margin-bottom: 16px; max-width: 78%; }
.msg .col { min-width: 0; }
.msg .head { font-size: 12px; font-weight: 700; margin-bottom: 3px; }
.msg.bot .head { color: var(--skype-deep); }
.msg.human .head { color: var(--coral-deep); text-align: right; }
.bubble { padding: 10px 14px; border-radius: 14px; font-size: 14px; line-height: 1.55;
          white-space: pre-wrap; word-break: break-word; box-shadow: var(--shadow-ring); }
.msg.bot .bubble { background: var(--cloud); border-top-left-radius: 4px; }
.msg.human { margin-left: auto; flex-direction: row-reverse; }
.msg.human .bubble { background: var(--coral); color: #fff; border-top-right-radius: 4px;
                     box-shadow: 0 8px 20px -8px rgba(200,78,63,.45); }
.meta { font-size: 11px; color: var(--ink-500); margin-top: 4px;
        font-family: ui-monospace, "SF Mono", monospace; }
.warm-tag { color: var(--gold-deep); font-weight: 700; }
#inputbar { margin: 0 22px 10px; display: flex; gap: 8px; align-items: center;
            background: var(--cloud); border-radius: 999px; padding: 6px 6px 6px 18px;
            box-shadow: var(--shadow-soft), var(--shadow-ring); }
#inp { flex: 1; border: none; outline: none; background: transparent;
       font-family: inherit; font-size: 14px; color: inherit; }
#inp::placeholder { color: var(--ink-500); }
#send { width: 36px; height: 36px; min-width: 36px; border-radius: 999px; border: none;
        background: var(--grad-brand); color: #fff; cursor: pointer;
        display: flex; align-items: center; justify-content: center;
        box-shadow: 0 6px 14px -4px rgba(0,80,140,.5); }
#send:disabled { opacity: .5; }
#hint { font-size: 11px; color: var(--ink-500); text-align: center; padding-bottom: 10px; }
/* ── 头像 ── */
.ava { width: 38px; height: 38px; min-width: 38px; border-radius: 999px;
       display: flex; align-items: center; justify-content: center;
       overflow: hidden; position: relative; box-shadow: var(--shadow-ring); }
.ava.xs { width: 30px; height: 30px; min-width: 30px; }
.ava .dot { position: absolute; right: -1px; bottom: -1px; width: 9px; height: 9px;
            border-radius: 999px; background: var(--avail); border: 2px solid var(--cloud); }
/* ── 功能模块通用 ── */
.grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.card { background: var(--cloud); border-radius: 12px; padding: 14px 16px;
        box-shadow: var(--shadow-ring); }
.card h4 { margin: 0 0 8px; font-size: 14px; color: var(--skype-ink); }
input.f, textarea.f, select.f { width: 100%; padding: 8px 10px; border-radius: 8px;
        border: 1px solid var(--ink-200); background: var(--paper);
        font-family: inherit; font-size: 13px; color: inherit; margin-bottom: 6px; }
textarea.f { min-height: 90px; resize: vertical; }
.btn { padding: 7px 14px; border-radius: 999px; border: none; background: var(--skype);
       color: #fff; cursor: pointer; font-size: 13px; }
.btn.ghost { background: transparent; color: var(--skype-deep);
             box-shadow: inset 0 0 0 1.5px var(--skype); }
.btn.sm { padding: 3px 10px; font-size: 12px; }
.row { display: flex; gap: 8px; align-items: center; margin-bottom: 8px; }
table.t { width: 100%; border-collapse: collapse; font-size: 13px; }
table.t td, table.t th { text-align: left; padding: 7px 10px;
                         border-bottom: 1px solid var(--ink-100); }
table.t th { color: var(--ink-500); font-weight: 600; font-size: 12px; }
.badge { display: inline-block; padding: 2px 10px; border-radius: 999px;
         font-size: 11px; font-weight: 700; }
.badge.gray { background: var(--ink-100); color: var(--ink-700); }
.badge.prod { background: var(--coral-soft); color: var(--coral-deep); }
.badge.ok { background: #E8F7E9; color: #3f9142; }
.badge.doing { background: #E3F3FD; color: var(--skype-deep); }
/* 看板 */
#boardWrap { display: flex; gap: 12px; align-items: flex-start; overflow-x: auto; }
.bcol { min-width: 220px; width: 220px; background: rgba(255,255,255,.55);
        border-radius: 12px; padding: 10px; box-shadow: var(--shadow-ring); }
.bcol h4 { margin: 2px 4px 10px; font-size: 13px; color: var(--ink-700); }
.bcard { background: var(--cloud); border-radius: 10px; padding: 9px 11px;
         margin-bottom: 8px; font-size: 13px; box-shadow: var(--shadow-ring); }
.bcard small { color: var(--ink-500); }
/* 投票 */
.poll { margin-bottom: 14px; }
.poll .opt { display: flex; align-items: center; gap: 8px; margin: 5px 0; font-size: 13px; }
.poll .bar { flex: 1; height: 8px; border-radius: 999px; background: var(--ink-100);
             overflow: hidden; }
.poll .bar i { display: block; height: 100%; background: var(--skype); }
/* 统计 */
.stat { display: inline-block; margin-right: 14px; }
.stat b { font-size: 22px; color: var(--skype-deep); font-family: Fraunces, serif; }
.stat span { font-size: 12px; color: var(--ink-500); margin-left: 4px; }
@media (prefers-color-scheme: dark) {
  :root { --paper: #21252B; --cloud: #282C34; --cloud-2: #2F333B;
          --ink-900: #E6E1DC; --ink-700: #C9C4BE; --ink-500: #9CA0A8;
          --ink-300: #4A4F57; --ink-200: #3A3F47; --ink-100: #33373F;
          --coral: #E06C75; --coral-deep: #FF9A8E; --coral-soft: #4a3335;
          --skype: #61AFEF; --skype-deep: #4FC2F4; --skype-ink: #97C7E8;
          --grad-chrome: linear-gradient(180deg,#21252B,#1D2126);
          --grad-rail: linear-gradient(180deg,#252932,#20242B);
          --chat-wash: radial-gradient(ellipse 80% 40% at 0% 0%, rgba(97,175,239,.08), transparent);
          --shadow-soft: 0 12px 40px -8px rgba(0,0,0,.5);
          --shadow-ring: 0 0 0 1px rgba(255,255,255,.06); }
}
</style></head>
<body>
<div id="rail">
  <div class="logo">✦</div>
  <button class="navbtn active" data-v="chat" title="聊天"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg></button>
  <button class="navbtn" data-v="board" title="看板"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2"/><line x1="9" y1="3" x2="9" y2="21"/></svg></button>
  <button class="navbtn" data-v="docs" title="文档"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg></button>
  <button class="navbtn" data-v="events" title="日程"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="18" rx="2"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/></svg></button>
  <button class="navbtn" data-v="projects" title="项目"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg></button>
  <button class="navbtn" data-v="companies" title="公司"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="2" width="16" height="20" rx="1"/><line x1="9" y1="6" x2="15" y2="6"/><line x1="9" y1="10" x2="15" y2="10"/><line x1="9" y1="14" x2="15" y2="14"/></svg></button>
  <button class="navbtn" data-v="polls" title="投票"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="6" y1="20" x2="6" y2="14"/><line x1="12" y1="20" x2="12" y2="8"/><line x1="18" y1="20" x2="18" y2="4"/></svg></button>
  <button class="navbtn" data-v="releases" title="发布"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4.5 16.5c-1.5 1.26-2 5-2 5s3.74-.5 5-2c.71-.84.7-2.13-.09-2.91a2.18 2.18 0 0 0-2.91-.09z"/><path d="M12 15l-3-3a22 22 0 0 1 2-3.95A12.88 12.88 0 0 1 22 2c0 2.72-.78 7.5-6 11a22.35 22.35 0 0 1-4 2z"/></svg></button>
  <div class="spacer"></div>
  <button class="navbtn" data-v="settings" title="设置"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg></button>
</div>

<!-- 聊天 -->
<div class="view on" id="v-chat">
  <div id="side"><div id="threads" style="flex:1">加载中…</div></div>
  <div class="panel">
    <div id="roster"></div>
    <div id="msgs"></div>
    <div id="inputbar">
      <input id="inp" placeholder="@default 你好，或直接发消息…">
      <button id="send" onclick="sendMsg()" title="发送"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg></button>
    </div>
    <div id="hint">@成员 提及 bot · 纯文本 = 人类消息 · 金色 暖 = 续接会话</div>
  </div>
</div>

<!-- 看板 -->
<div class="view" id="v-board"><div class="panel">
  <div class="ph"><h2>看板</h2><div class="sub">Boards · 拖拽后续加, 先能建卡</div></div>
  <div class="pbody"><div id="boardWrap">加载中…</div></div>
</div></div>

<!-- 文档 -->
<div class="view" id="v-docs"><div class="panel">
  <div class="ph"><h2>文档</h2><div class="sub">Documents</div></div>
  <div class="pbody"><div class="grid2">
    <div class="card"><h4>新建文档</h4>
      <input class="f" id="docTitle" placeholder="标题">
      <textarea class="f" id="docBody" placeholder="正文…"></textarea>
      <button class="btn" onclick="newDoc()">保存</button></div>
    <div class="card"><h4>文档列表</h4><div id="docList">加载中…</div></div>
  </div></div>
</div></div>

<!-- 日程 -->
<div class="view" id="v-events"><div class="panel">
  <div class="ph"><h2>日程</h2><div class="sub">Calendar</div></div>
  <div class="pbody"><div class="grid2">
    <div class="card"><h4>新建事件</h4>
      <input class="f" id="evTitle" placeholder="事件标题">
      <input class="f" id="evTs" type="datetime-local">
      <input class="f" id="evNote" placeholder="备注">
      <button class="btn" onclick="newEvent()">添加</button></div>
    <div class="card"><h4>事件列表</h4><div id="evList">加载中…</div></div>
  </div></div>
</div></div>

<!-- 项目 -->
<div class="view" id="v-projects"><div class="panel">
  <div class="ph"><h2>项目</h2><div class="sub">Projects</div></div>
  <div class="pbody"><div class="grid2">
    <div class="card"><h4>新建项目</h4>
      <input class="f" id="pjName" placeholder="项目名">
      <input class="f" id="pjNote" placeholder="备注">
      <button class="btn" onclick="newProject()">添加</button></div>
    <div class="card"><h4>项目列表</h4><div id="pjList">加载中…</div></div>
  </div></div>
</div></div>

<!-- 公司 -->
<div class="view" id="v-companies"><div class="panel">
  <div class="ph"><h2>公司</h2><div class="sub">Companies</div></div>
  <div class="pbody"><div class="grid2">
    <div class="card"><h4>新建公司</h4>
      <input class="f" id="coName" placeholder="公司名">
      <input class="f" id="coNote" placeholder="备注">
      <button class="btn" onclick="newCompany()">添加</button></div>
    <div class="card"><h4>公司列表</h4><div id="coList">加载中…</div></div>
  </div></div>
</div></div>

<!-- 投票 -->
<div class="view" id="v-polls"><div class="panel">
  <div class="ph"><h2>投票</h2><div class="sub">Polls</div></div>
  <div class="pbody"><div class="grid2">
    <div class="card"><h4>发起投票</h4>
      <input class="f" id="plQ" placeholder="问题">
      <input class="f" id="plOpts" placeholder="选项, 逗号分隔">
      <button class="btn" onclick="newPoll()">发起</button></div>
    <div class="card"><h4>进行中的投票</h4><div id="plList">加载中…</div></div>
  </div></div>
</div></div>

<!-- 发布 -->
<div class="view" id="v-releases"><div class="panel">
  <div class="ph"><h2>发布</h2><div class="sub">Shipping · 三件套: release notes / rollback plan / smoke 证据</div></div>
  <div class="pbody"><div class="grid2">
    <div class="card"><h4>新建发布</h4>
      <input class="f" id="rlVer" placeholder="版本号 (如 v1.2.0)">
      <input class="f" id="rlSha" placeholder="Commit SHA">
      <textarea class="f" id="rlNotes" placeholder="Release notes: 用户可见变化 + 已知缺口"></textarea>
      <textarea class="f" id="rlRb" placeholder="Rollback plan: 触发条件 + 具体命令"></textarea>
      <textarea class="f" id="rlBase" placeholder="Baseline 基线指标 (生产必填)"></textarea>
      <button class="btn" onclick="newRelease()">提交灰度</button></div>
    <div class="card"><h4>发布列表</h4><div id="rlList">加载中…</div></div>
  </div></div>
</div></div>

<!-- 设置 -->
<div class="view" id="v-settings"><div class="panel">
  <div class="ph"><h2>设置</h2><div class="sub">Trust & Autonomy · Quota · Release notes</div></div>
  <div class="pbody">
    <div class="card" style="margin-bottom:14px"><h4>用量</h4><div id="quotaStats">加载中…</div></div>
    <div class="card" style="margin-bottom:14px"><h4>自主行动战绩 (Pulled-group track records)</h4><div id="autoList">加载中…</div></div>
    <div class="card"><h4>Release notes · 2026-09-03</h4>
      <div style="font-size:13px;line-height:1.7">
      <b>Shell v2</b>: 复刻 Cumora 功能区 — 看板/文档/日程/项目/公司/投票/发布<br>
      <b>P1</b>: 回合元数据观测 · <b>P2</b>: 暖启动 resume (34x) · <b>P3</b>: autonomy 自动接续+战绩<br>
      <b>UI</b>: Cumora 设计语言 (cloud/sky/coral tokens, SVG 机器人头像)</div></div>
  </div>
</div></div>

<script>
/* ── 头像: SVG 机器人脸 ── */
const BOT_COLORS = [['#00A8F0','#0078C8'],['#4FC2F4','#0a82b8'],['#6EC56A','#3f9142'],
                    ['#C678DD','#8e44ad'],['#F4B740','#c8871a'],['#FF7A6B','#d85a4b']];
const hash = s => { let h = 0; for (const c of s) h = (h * 31 + c.charCodeAt(0)) | 0;
                     return Math.abs(h); };
function botFace(name) {
  const h = hash(name);
  const [c1, c2] = BOT_COLORS[h % BOT_COLORS.length];
  const eye = h % 3, mouth = (h >> 3) % 3;
  const EYES = [
    `<circle cx="16" cy="19" r="2.6" fill="#fff"/><circle cx="24" cy="19" r="2.6" fill="#fff"/><circle cx="16" cy="19" r="1.2" fill="#1C1917"/><circle cx="24" cy="19" r="1.2" fill="#1C1917"/>`,
    `<rect x="13" y="16.5" width="5" height="5" rx="1.2" fill="#fff"/><rect x="22" y="16.5" width="5" height="5" rx="1.2" fill="#fff"/>`,
    `<path d="M13 19 q3 -3.5 6 0 M21 19 q3 -3.5 6 0" stroke="#fff" stroke-width="2" fill="none" stroke-linecap="round"/>`
  ][eye];
  const MOUTH = [
    `<path d="M15 24.5 q5 4 10 0" stroke="#fff" stroke-width="2" fill="none" stroke-linecap="round"/>`,
    `<line x1="16" y1="25" x2="24" y2="25" stroke="#fff" stroke-width="2" stroke-linecap="round"/>`,
    `<circle cx="20" cy="25" r="2" stroke="#fff" stroke-width="1.6" fill="none"/>`
  ][mouth];
  const ANT = (h >> 5) % 2 ? `<line x1="20" y1="8" x2="20" y2="12" stroke="${c2}" stroke-width="1.6"/><circle cx="20" cy="7" r="1.8" fill="${c2}"/>` : '';
  return `<svg viewBox="0 0 40 40" width="100%" height="100%"><defs><linearGradient id="g${h%7}" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="${c1}"/><stop offset="1" stop-color="${c2}"/></linearGradient></defs><circle cx="20" cy="21" r="13" fill="url(#g${h%7})"/>${ANT}${EYES}${MOUTH}</svg>`;
}
const humanFace = () => `<svg viewBox="0 0 40 40" width="100%" height="100%"><circle cx="20" cy="14" r="6" fill="#fff"/><path d="M8 34 q12 -13 24 0" fill="#fff"/></svg>`;
const ava = (name, dot, xs) =>
  `<div class="ava ${xs?'xs':''}" style="background:${name === 'user' ? '#FF7A6B' : 'linear-gradient(135deg,' + BOT_COLORS[hash(name) % BOT_COLORS.length][0] + ',#003B6F)'}">` +
  `${name === 'user' ? humanFace() : botFace(name)}${dot ? '<span class="dot"></span>' : ''}</div>`;

/* ── 通用 ── */
let cur = null, busy = false;
async function j(u, opt) { const r = await fetch(u, opt); return r.json(); }
const post = (u, body) => j(u, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
function esc(s) { return (s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
function ts(s) { return s ? new Date(s * 1000).toLocaleString('zh-CN', {month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit'}) : ''; }

/* ── 视图切换 ── */
document.querySelectorAll('.navbtn').forEach(b => b.onclick = () => {
  document.querySelectorAll('.navbtn').forEach(x => x.classList.remove('active'));
  document.querySelectorAll('.view').forEach(x => x.classList.remove('on'));
  b.classList.add('active');
  document.getElementById('v-' + b.dataset.v).classList.add('on');
  loadView(b.dataset.v);
});
function loadView(v) {
  ({chat: loadThreads, board: loadBoard, docs: loadDocs, events: loadEvents,
    projects: loadProjects, companies: loadCompanies, polls: loadPolls,
    releases: loadReleases, settings: loadSettings}[v] || (() => {}))();
}

/* ── 聊天 ── */
async function loadThreads() {
  const d = await j('/api/threads');
  document.getElementById('threads').innerHTML = d.threads.map(t =>
    `<div class="thread ${cur===t.name?'active':''}" onclick="openT('${t.name}')">` +
    ava(t.members.split(',')[0]) +
    `<div style="min-width:0"><div class="tname">${esc(t.name)}</div>` +
    `<div class="tsub">${esc(t.members)} · ${t.msgs}条</div></div></div>`).join('')
    || '<div style="color:var(--ink-500);font-size:12px;padding:8px">(无线程)</div>';
}
async function openT(name) {
  cur = name; await loadThreads();
  const d = await j('/api/threads');
  const t = d.threads.find(x => x.name === name);
  document.getElementById('roster').innerHTML = (t ? t.members.split(',') : [])
    .map(m => `<div class="member">${ava(m, true, true)}<span>${esc(m)}</span></div>`).join('')
    + `<div class="member">${ava('user', false, true)}<span>user</span></div>`;
  await loadMsgs();
}
async function loadMsgs() {
  if (!cur) return;
  const d = await j('/api/messages?name=' + encodeURIComponent(cur));
  const box = document.getElementById('msgs');
  box.innerHTML = d.messages.map(m => {
    const isBot = m.role === 'bot';
    const metaBits = isBot ? [m.model, m.duration_ms ? m.duration_ms+'ms' : null] : [];
    const meta = isBot ? `<div class="meta">${metaBits.filter(Boolean).join(' · ')}${m.warm ? ' · <span class="warm-tag">暖</span>' : ''}</div>` : '';
    return `<div class="msg ${m.role}">${isBot ? ava(m.author, true) : ava('user')}<div class="col"><div class="head">${esc(m.author)}</div><div class="bubble">${esc(m.content)}</div>${meta}</div></div>`;
  }).join('');
  box.scrollTop = box.scrollHeight;
}
async function sendMsg() {
  const inp = document.getElementById('inp');
  const text = inp.value.trim();
  if (!text || !cur || busy) return;
  busy = true; inp.value = '';
  document.getElementById('send').disabled = true;
  try { await post(text.includes('@') ? '/api/ask' : '/api/say', {name: cur, text}); }
  catch (e) { alert('失败: ' + e); }
  busy = false; document.getElementById('send').disabled = false;
  await loadMsgs(); await loadThreads();
}
document.getElementById('inp').addEventListener('keydown', e => { if (e.key === 'Enter') sendMsg(); });

/* ── 看板 ── */
async function loadBoard() {
  let d = await j('/api/boards');
  if (!d.rows.length) {
    for (const [i, n] of ['待办', '进行中', '已完成'].entries()) await post('/api/boards', {name: n, pos: i});
    d = await j('/api/boards');
  }
  const cards = (await j('/api/board_cards')).rows;
  document.getElementById('boardWrap').innerHTML = d.rows.map(col => {
    const cs = cards.filter(c => c.board_id === col.id);
    return `<div class="bcol"><h4>${esc(col.name)} · ${cs.length}</h4>` +
      cs.map(c => `<div class="bcard">${esc(c.title)}${c.note ? `<br><small>${esc(c.note)}</small>` : ''}</div>`).join('') +
      `<div class="row" style="margin-top:6px"><input class="f" id="bc_${col.id}" placeholder="加卡片…" style="margin:0" onkeydown="if(event.key==='Enter')newCard(${col.id})">` +
      `<button class="btn sm" onclick="newCard(${col.id})">+</button></div></div>`;
  }).join('');
}
async function newCard(colId) {
  const inp = document.getElementById('bc_' + colId);
  if (!inp.value.trim()) return;
  await post('/api/board_cards', {board_id: colId, title: inp.value.trim()});
  loadBoard();
}

/* ── 文档 ── */
async function loadDocs() {
  const d = await j('/api/docs');
  document.getElementById('docList').innerHTML = d.rows.map(r =>
    `<div class="row"><b style="flex:1">${esc(r.title)}</b><small style="color:var(--ink-500)">${ts(r.updated_at)}</small></div>`).join('') || '(空)';
}
async function newDoc() {
  const t = document.getElementById('docTitle').value.trim();
  if (!t) return;
  await post('/api/docs', {title: t, body: document.getElementById('docBody').value});
  document.getElementById('docTitle').value = ''; document.getElementById('docBody').value = '';
  loadDocs();
}

/* ── 日程 ── */
async function loadEvents() {
  const d = await j('/api/events');
  document.getElementById('evList').innerHTML = d.rows.map(r =>
    `<div class="row"><b style="flex:1">${esc(r.title)}</b><small>${esc(r.ts)}</small></div>`).join('') || '(空)';
}
async function newEvent() {
  const t = document.getElementById('evTitle').value.trim();
  if (!t) return;
  await post('/api/events', {title: t, ts: document.getElementById('evTs').value, note: document.getElementById('evNote').value});
  document.getElementById('evTitle').value = ''; loadEvents();
}

/* ── 项目 ── */
const PJ_STATUS = ['进行中', '暂停', '已完成'];
async function loadProjects() {
  const d = await j('/api/projects');
  document.getElementById('pjList').innerHTML =
    `<table class="t"><tr><th>项目</th><th>状态</th><th>备注</th></tr>` +
    d.rows.map(r => `<tr><td><b>${esc(r.name)}</b></td><td><span class="badge ${r.status==='已完成'?'ok':r.status==='进行中'?'doing':'gray'}">${esc(r.status)}</span></td><td>${esc(r.note)}</td></tr>`).join('') + '</table>';
}
async function newProject() {
  const t = document.getElementById('pjName').value.trim();
  if (!t) return;
  await post('/api/projects', {name: t, note: document.getElementById('pjNote').value});
  document.getElementById('pjName').value = ''; loadProjects();
}

/* ── 公司 ── */
async function loadCompanies() {
  const d = await j('/api/companies');
  document.getElementById('coList').innerHTML =
    `<table class="t"><tr><th>公司</th><th>备注</th></tr>` +
    d.rows.map(r => `<tr><td><b>${esc(r.name)}</b></td><td>${esc(r.note)}</td></tr>`).join('') + '</table>';
}
async function newCompany() {
  const t = document.getElementById('coName').value.trim();
  if (!t) return;
  await post('/api/companies', {name: t, note: document.getElementById('coNote').value});
  document.getElementById('coName').value = ''; loadCompanies();
}

/* ── 投票 ── */
async function loadPolls() {
  const d = await j('/api/polls');
  document.getElementById('plList').innerHTML = d.rows.map(r => {
    const opts = JSON.parse(r.options || '[]'), votes = JSON.parse(r.votes || '{}');
    const total = Object.values(votes).reduce((a, b) => a + b, 0);
    return `<div class="poll card" style="box-shadow:none;border:1px solid var(--ink-100)"><b>${esc(r.question)}</b>` +
      opts.map(o => { const v = votes[o] || 0, pct = total ? Math.round(v * 100 / total) : 0;
        return `<div class="opt"><button class="btn sm ghost" onclick="vote(${r.id}, '${esc(o)}')">投</button><span style="min-width:70px">${esc(o)}</span><div class="bar"><i style="width:${pct}%"></i></div><small>${v}票 ${pct}%</small></div>`; }).join('') + '</div>';
  }).join('') || '(暂无投票)';
}
async function vote(id, opt) { await post(`/api/polls/${id}/vote`, {opt}); loadPolls(); }
async function newPoll() {
  const q = document.getElementById('plQ').value.trim();
  const opts = document.getElementById('plOpts').value.split(/[,，]/).map(s => s.trim()).filter(Boolean);
  if (!q || !opts.length) return;
  await post('/api/polls', {question: q, options: JSON.stringify(opts), votes: '{}'});
  document.getElementById('plQ').value = ''; document.getElementById('plOpts').value = '';
  loadPolls();
}

/* ── 发布 ── */
async function loadReleases() {
  const d = await j('/api/releases');
  document.getElementById('rlList').innerHTML = d.rows.map(r =>
    `<div class="card" style="margin-bottom:10px"><div class="row"><b style="flex:1">${esc(r.version)}</b>` +
    `<span class="badge ${r.status==='生产'?'prod':'gray'}">${esc(r.status)}</span>` +
    (r.status === '灰度' ? `<button class="btn sm" onclick="promote(${r.id})">晋升生产</button>` : '') + `</div>` +
    (r.commit_sha ? `<small style="color:var(--ink-500)">${esc(r.commit_sha)}</small>` : '') +
    (r.notes ? `<div style="font-size:12.5px;margin-top:6px"><b>Release notes</b><br>${esc(r.notes)}</div>` : '') +
    (r.rollback ? `<div style="font-size:12.5px;margin-top:6px"><b>Rollback</b><br>${esc(r.rollback)}</div>` : '') +
    (r.baseline ? `<div style="font-size:12.5px;margin-top:6px"><b>Baseline</b><br>${esc(r.baseline)}</div>` : '') + '</div>'
  ).join('') || '(暂无发布)';
}
async function promote(id) { await post('/api/releases', {id, status: '生产'}); loadReleases(); }
async function newRelease() {
  const v = document.getElementById('rlVer').value.trim();
  if (!v) return;
  await post('/api/releases', {version: v, commit_sha: document.getElementById('rlSha').value,
    notes: document.getElementById('rlNotes').value, rollback: document.getElementById('rlRb').value,
    baseline: document.getElementById('rlBase').value, status: '灰度'});
  ['rlVer','rlSha','rlNotes','rlRb','rlBase'].forEach(i => document.getElementById(i).value = '');
  loadReleases();
}

/* ── 设置 ── */
async function loadSettings() {
  const d = await j('/api/stats');
  document.getElementById('quotaStats').innerHTML =
    `<span class="stat"><b>${d.turns}</b><span>bot 回合</span></span>` +
    `<span class="stat"><b>${(d.input_tokens/1000).toFixed(1)}k</b><span>input tokens</span></span>` +
    `<span class="stat"><b>${(d.output_tokens/1000).toFixed(1)}k</b><span>output tokens</span></span>` +
    `<span class="stat"><b>${d.avg_ms}ms</b><span>平均耗时</span></span>`;
  document.getElementById('autoList').innerHTML = d.auto_actions.map(a =>
    `<div class="row"><span class="badge gray">${esc(a.thread)}</span>` +
    `<span style="flex:1">${esc(a.actor)} → @${esc(a.target)} (trigger #${a.trigger_seq}, depth ${a.depth})</span>` +
    `<span class="badge ok">${esc(a.status)}</span></div>`).join('') || '(无自动行动记录)';
}

setInterval(() => { if (cur && !busy) loadMsgs(); }, 5000);
loadThreads();
</script></body></html>"""


def cmd_serve(args):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import urllib.parse

    # R 复刻模块白名单: api 路径 → (表, 允许列)
    MODULES = {
        "boards": ("boards", ("name", "pos")),
        "board_cards": ("board_cards", ("board_id", "title", "note", "pos")),
        "docs": ("docs", ("title", "body", "updated_at")),
        "events": ("events", ("title", "ts", "note")),
        "projects": ("projects", ("name", "status", "note")),
        "companies": ("companies", ("name", "note")),
        "polls": ("polls", ("question", "options", "votes", "closed")),
        "releases": ("releases", ("version", "commit_sha", "notes",
                                  "rollback", "baseline", "status", "created_at")),
    }

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _json(self, obj, code=200):
            body = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)

        def _rows(self, conn, sql, params=()):
            conn.row_factory = sqlite3.Row
            return [dict(r) for r in conn.execute(sql, params).fetchall()]

        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            if u.path == "/":
                body = INDEX_HTML.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(body)
                return
            conn = db()
            if u.path == "/api/threads":
                self._json({"threads": self._rows(conn,
                    "SELECT t.name,t.members,"
                    " (SELECT COUNT(*) FROM messages m WHERE m.thread_id=t.id) AS msgs"
                    " FROM threads t ORDER BY t.id")})
                return
            if u.path == "/api/messages":
                q = urllib.parse.parse_qs(u.query)
                name = q.get("name", [""])[0]
                try:
                    tid, _, _, _ = get_thread(conn, name)
                except SystemExit:
                    self._json({"messages": []}, 404)
                    return
                self._json({"messages": self._rows(conn,
                    "SELECT seq,author,role,content,model,duration_ms,warm"
                    " FROM messages WHERE thread_id=? ORDER BY seq", (tid,))})
                return
            if u.path == "/api/stats":
                r = conn.execute(
                    "SELECT COUNT(*) n, COALESCE(SUM(input_tokens),0) it,"
                    " COALESCE(SUM(output_tokens),0) ot,"
                    " COALESCE(AVG(duration_ms),0) avg_ms"
                    " FROM messages WHERE role='bot'").fetchone()
                auto = self._rows(conn,
                    "SELECT a.*, t.name AS thread FROM auto_actions a"
                    " JOIN threads t ON t.id=a.thread_id"
                    " ORDER BY a.id DESC LIMIT 20")
                self._json({"turns": r[0], "input_tokens": r[1],
                            "output_tokens": r[2], "avg_ms": int(r[3]),
                            "auto_actions": auto})
                return
            parts = u.path.strip("/").split("/")
            if len(parts) >= 2 and parts[0] == "api" and parts[1] in MODULES:
                table, _ = MODULES[parts[1]]
                self._json({"rows": self._rows(conn,
                    f"SELECT * FROM {table} ORDER BY id")})
                return
            self._json({"error": "not found"}, 404)

        def do_POST(self):
            u = urllib.parse.urlparse(self.path)
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                self._json({"error": "bad json"}, 400)
                return
            parts = u.path.strip("/").split("/")
            conn = db()

            # 模块 CRUD (带 id 则 UPDATE, 否则 INSERT)
            if len(parts) >= 2 and parts[0] == "api" and parts[1] in MODULES:
                table, cols = MODULES[parts[1]]
                data = {k: v for k, v in body.items() if k in cols}
                if not data:
                    self._json({"error": "no valid fields"}, 400)
                    return
                if body.get("id"):
                    sets = ", ".join(f"{k}=?" for k in data)
                    conn.execute(f"UPDATE {table} SET {sets} WHERE id=?",
                                 (*data.values(), body["id"]))
                else:
                    if table == "docs":
                        data.setdefault("updated_at", time.time())
                    if table == "releases":
                        data.setdefault("created_at", time.time())
                    keys = ", ".join(data)
                    conn.execute(f"INSERT INTO {table}({keys}) VALUES({','.join('?' * len(data))})",
                                 tuple(data.values()))
                conn.commit()
                self._json({"ok": True})
                return

            # 投票
            if len(parts) == 4 and parts[:2] == ["api", "polls"] and parts[3] == "vote":
                row = conn.execute("SELECT votes FROM polls WHERE id=?",
                                   (parts[2],)).fetchone()
                if not row:
                    self._json({"error": "poll not found"}, 404)
                    return
                votes = json.loads(row[0] or "{}")
                opt = body.get("opt", "")
                votes[opt] = votes.get(opt, 0) + 1
                conn.execute("UPDATE polls SET votes=? WHERE id=?",
                             (json.dumps(votes, ensure_ascii=False), parts[2]))
                conn.commit()
                self._json({"ok": True})
                return

            name, text = body.get("name", ""), body.get("text", "")
            if not name or not text:
                self._json({"error": "name+text required"}, 400)
                return
            try:
                tid, tname, members_s, _ = get_thread(conn, name)
            except SystemExit as e:
                self._json({"error": str(e)}, 404)
                return
            members = members_s.split(",")
            if u.path == "/api/say":
                append(conn, tid, "user", "human", text)
                self._json({"ok": True})
                return
            if u.path == "/api/ask":
                mentions = parse_mentions(text, members)
                if not mentions:
                    self._json({"error": "没有 @mention"}, 400)
                    return
                append(conn, tid, "user", "human", text)
                max_depth = min(get_auto_chain(conn, tid), 3)
                visited = set(mentions)
                for profile in mentions:
                    run_turn(conn, tid, tname, profile, 0, max_depth, visited)
                self._json({"ok": True})
                return
            self._json({"error": "not found"}, 404)

    port = args.port
    print(f"🌐 Bot Team Chat 面板: http://localhost:{port}")
    print(f"   数据: {DB_PATH} (预览窗格打开即可, Ctrl+C 停)")
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()


def cmd_show(args):
    conn = db()
    tid, name, members, version = get_thread(conn, args.name)
    rows = conn.execute(
        "SELECT seq,author,role,content,model,duration_ms,warm FROM messages"
        " WHERE thread_id=? ORDER BY seq DESC LIMIT ?", (tid, args.n),
    ).fetchall()
    print(f"=== {name} (成员: {members}, version={version}) ===")
    for seq, author, role, content, model, dur, warm in reversed(rows):
        tag = ""
        if role == "bot":
            bits = [b for b in [model, f"{dur}ms" if dur else None,
                                "暖" if warm else None] if b]
            tag = f"  ⟨{' | '.join(bits)}⟩" if bits else ""
        print(f"\n--- #{seq} [{author}|{role}]{tag} ---")
        print(content[:2000])


def cmd_list(_args):
    conn = db()
    rows = conn.execute(
        "SELECT t.name,t.members,t.version,"
        " (SELECT COUNT(*) FROM messages m WHERE m.thread_id=t.id)"
        " FROM threads t ORDER BY t.id"
    ).fetchall()
    for name, members, version, n in rows:
        print(f"{name}  成员=[{members}]  消息={n}  version={version}")


def main():
    p = argparse.ArgumentParser(description="Hermes Bot Team Chat v1 编排器")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("create")
    c.add_argument("name")
    c.add_argument("--members", required=True, help="逗号分隔 profile 列表")
    c.set_defaults(fn=cmd_create)

    s = sub.add_parser("say")
    s.add_argument("name")
    s.add_argument("text")
    s.add_argument("--as", dest="as_", default="user")
    s.set_defaults(fn=cmd_say)

    a = sub.add_parser("ask")
    a.add_argument("name")
    a.add_argument("text")
    a.set_defaults(fn=cmd_ask)

    sh = sub.add_parser("show")
    sh.add_argument("name")
    sh.add_argument("-n", type=int, default=20)
    sh.set_defaults(fn=cmd_show)

    l = sub.add_parser("list")
    l.set_defaults(fn=cmd_list)

    ch = sub.add_parser("chain", help="自动接续阈值 (P3 autonomy)")
    ch.add_argument("name")
    ch.add_argument("n", type=int, nargs="?", default=None,
                    help="0-3, 省略则查询")
    ch.set_defaults(fn=cmd_chain)

    au = sub.add_parser("autonomy", help="自主权配置 + 自动行动战绩")
    au.add_argument("name")
    au.set_defaults(fn=cmd_autonomy)

    sv = sub.add_parser("serve", help="本地 Web 面板 (Phase 2 桌面 UI v1)")
    sv.add_argument("--port", type=int, default=8931)
    sv.set_defaults(fn=cmd_serve)

    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
