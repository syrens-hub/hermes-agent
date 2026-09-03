# Bot Team Chat — Hermes 多 Agent 团队线程编排器

本地多 agent 团队聊天编排器 + Web 面板（设计稿：`~/.hermes/plans/bot-team-chat-design.md`）。

## 组成

| 文件 | 说明 |
|---|---|
| `bot_thread.py` | 编排器 + Web 面板 + REST API（Python stdlib 零依赖）。`serve --port 8931` 起面板 |
| `bot_wizard.py` | 人格向导：6 字段 → profile.yaml + SOUL.md + 验证过的 config.yaml 模板 + 可选 mmx 头像 |
| `test_cas.py` | CAS 锁并发实测脚本 |
| `personas/` | 8 个 bot 人格 SOUL.md（penn/sage/nova/bram/iris/atlas 复刻自 Cumora，legal-advisor/negotiation-coach 自建） |

## 已实现（全部实测验证）

- **线程存储**：SQLite WAL + version CAS 锁（HELD 语义：冲突重试一次后报"上下文已更新"）；5 进程×20 消息 = 100/100 零丢失零重复
- **@mention 回合调度**：显式提及才触发，多 mention 串行，不猜路由
- **回合可观测**：`--usage-file` 解析 model/session_id/tokens 落 messages 表
- **暖启动**：sessions 表 per (thread,profile) 存 session_id 自动 `--resume`，暖回合 in≈600 tokens vs 冷启动≈20710（34 倍）
- **autonomy 自动接续**：`chain <线程> 0-3` 阈值，bot 回复 @ 成员时自动接续 + auto_actions 战绩表 + 防环路 visited 集
- **Shell v2 Web 面板**（复刻 Cumora 交互）：九视图菜单栏、8 功能模块（boards/docs/events/projects/companies/polls/releases + 通用 CRUD）、发布灰度→生产晋升
- **CLI**：create / say / ask / show / list / chain / autonomy / serve

## 关键经验（踩坑沉淀）

1. model 名→backend 映射是隐形开关：调用方传错 model 名就劫持整条 fallback 链
2. profile 不继承主 `~/.hermes/.env`；profile config 里 `${env:X}` 要靠进程 env 显式注入
3. `--usage-file` 是顶层平铺结构，非嵌套 usage 键
4. pkill -f 杀 shell 脚本必留 python heredoc 孤儿，守护进程必须 setsid + PGID 整组清杀
5. 纯中文 profile 名无 ASCII ID 时需自动追问；模板 `${{env:}}` 双括号会致 401

## 快速开始

```bash
# 起面板
python3 bot_thread.py serve --port 8931

# 建线程 + 拉 bot 进群
python3 bot_thread.py create <线程名>
python3 bot_thread.py chain <线程名> 2   # autonomy 阈值

# 造一个新 bot 人格
python3 bot_wizard.py
```

## 边界（未做，YAGNI）

- standing prompt 模板（STANDING.md 每回合注入）
- triage 语义分诊小脑
- 桌面 app 原生集成（现为独立 Web 面板）
- 云端三件套（push/email/whisper 私聊信道）与多人协同光标
- 团队记忆命名空间（fact_store scope=team）

数据在 `~/.hermes/bot-threads/threads.db`（不入库）。profiles 由 bot_wizard.py 生成到 `~/.hermes/profiles/`（含本机密钥，不入库）。
