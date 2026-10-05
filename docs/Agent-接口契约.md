# Agent 接口契约

> 面向"**让 agent 直接驱动流水线**"这件事：你（人）跟 agent 说"拿我这份拍摄稿出片"，
> agent 自己把 parse → shots → assets → voice → build 跑完。
>
> 这份文档写清 agent 需要知道的**全部约定**。`tests/test_agent_contract.py` 守着它。

---

## 一、整体形状：agent 与门禁的握手

流水线有 **G0–G5 六道人审门禁**。这跟"全自动"不矛盾 —— 门禁不是障碍，
而是 agent **向人确认的固定时机**：

```
agent: lvs doctor --json                → 环境行不行（不行就先修）
agent: lvs parse <稿> --task X          → 建立任务
agent: lvs run <稿> --task X            → 跑到第一道门，退出码 3
       （此时 gate --json 告诉它"下一道门是 G0，产物已就绪"）
agent: 把产物指给你看 → 你说"行"
agent: lvs gate --task X --approve G0 --note "用户确认"
agent: lvs run <稿> --task X            → 从原地继续，跑到下一道门
       ...重复直到成片
```

**关键**：`lvs run` 的语义是"**跑到下一道门为止**"，不是一次跑完。
重跑同一条命令即可续跑（已完成阶段自动跳过）。所以 agent 的循环很短：

```
while 退出码 == 3:                       # 3 = 等人审
    看 board --json 得到 next_stage
    把门禁要看的产物给用户
    用户同意 → gate --approve
    再 run 一次
```

---

## 二、退出码：agent 的**唯一**跨进程信号

```
0  成功
1  跑完了，但有失败件（逐镜隔离：某几镜没取到素材/没合出音）—— 可继续
2  输入/配置/前置不对 —— 改完再来（**别自动重试**）
3  被人审门禁拦住 —— **等人做决定，不是错误**
```

`2` 与 `3` 必须分开，否则 agent 分不清"该等用户"还是"该报警"。
（有 `--json` 时，同一个数值也在 `exit_code` 字段里。）

---

## 三、`--json` 契约

### 3.1 哪些命令有

| 命令 | `--json` | 输出形态 |
|---|---|---|
| `parse` `shots` `assets` `voice` `build` `run` | ✅ | 散文在前，**信封是最后一段** |
| `gate` | ✅ | 干净 JSON |
| `qc` | ✅ | 干净 JSON（生图巡检，G3 判据） |
| `qc --final` | ✅ | 干净 JSON（**成片自检报告**：ffprobe / 抽帧 / 音频 / 字幕；★ 只报警不改判，**不进 G5 门禁**） |
| `config check` | ✅ | 干净 JSON（配置体检 findings + 段识别；退出码 0 通过 / 1 有 error / 2 找不到文件） |
| `styles` | ✅ | 干净 JSON |
| `board` | ✅ | **干净 JSON**（进度 + `next_stage`） |
| `doctor` | ✅ | **干净 JSON**（体检项数组） |
| `status` | ✅ | **干净 JSON**（一行一任务：门禁开闭 / 产物 / `next`） |
| `map` | ❌ | 无 `--json`，**故意的** —— 输出就是给人/给 agent 读的行号锚定文本 |
| `publish` | ❌ | 暂无 `--json`；产物是**文件**（封面 PNG + `bilibili.md` / `douyin.md` / `meta.json`） |
| `cast` `library` `image` `clean` `check` `migrate` | ❌ | 暂无（见 §九） |

#### 3.1.1 ★ 省 token 的**只读**入口（2026-10-05 补，续跑先看这一节）

| 想干什么 | 用哪个 | 代价实测 |
|---|---|---|
| 续跑前"到哪了" | `lvs status --brief` | 6 行 / 4 任务；单任务 5.9 s |
| 看分镜全表 | `lvs shots --index` | 516 KB → 363 行 / **18.6 KB** |
| 看某一镜 | `lvs shots --peek <id>` | **316 字符 / 5 行**（`--full` 才打提示词全文） |
| 读源码/文档 | `lvs map show 文件 起 止` / `lvs map grep 正则` | 按区间取，别整文件读 |
| 门禁开闭 | `lvs gate --json` | 见 §四 |

> ★ 这些"只读"标志**必须真的只读**：`shots` 在 CLI 的 `_ENVELOPE_COMMANDS` 里，
> 早先 `--peek/--index` 会顺手 `note_stage_end`（只读命令写阶段状态）——
> 已抽 `cli._shots_readonly` 绕过。**加只读标志时照这个做**。

> ⚠️ **`board` / `doctor` 的 `--json` 在 2026-10-03 之前根本不存在** ——
> `doctor.run(as_json=…)` 早就实现了，但 CLI 从来没传过。
> 如果你在旧版本上跑，agent 就没法机器可读地问"到哪了 / 环境行不行"。

### 3.2 两种形态，判据不同

**A. 阶段命令（parse/shots/assets/voice/build/run）—— "取最后那段 JSON"**

它们的散文照旧打在前面（"素材 12/64"、进度条），**信封在最后**。
agent 的做法：从输出**末尾往前**找第一个能解析成 dict 的 `{`。

> ⚠️ 别直接 `json.loads(整个 stdout)` —— 会失败。也别"找第一个 `{`"：
> 提示词里真的有花括号槽位名（`{SAIGYO}`），会误判。

**B. 查询命令（gate/board/doctor/qc/styles）—— "整个 stdout 就是一个 JSON"**

直接 `json.loads(stdout)`。

**信封里锁定的 5 个决策字段**（`lvs/result.py` 声明，测试守着）：

```json
{
  "ok": false,
  "status": "blocked",
  "exit_code": 3,
  "counts": {"total": 0, "done": 0, "skipped": 0, "failed": 0},
  "next_hint": "…给 agent 的下一步提示…"
}
```

其余字段可自由扩展，这 5 个不许改名/删。

---

## 四、`gate --json`：agent 决定下一步的依据

最有用的一条。它直接告诉你**下一道门是什么、要不要动**：

```json
{
  "task": "UGE01",
  "next": {
    "id": "G0",
    "key": "script",
    "state": "pending",
    "ready": true,
    "criterion_ok": true,
    "stage_hint": "把拍摄稿放进来（并跑 `lvs parse` 建立任务）",
    "action_hint": "产物已就绪（1 个文件）—— 审完再放行：lvs gate --task UGE01 --approve G0 --note \"…\""
  },
  "all_open": false
}
```

- `next.id` —— 卡在哪道门
- `state` —— `pending`（还没审）/ `stale`（产物变了，批准失效）/ `rejected`（被打回）
- `ready` —— 产物齐了没有
- `criterion_ok` —— **自动判据**过没过（门禁 = 人批准 **且** 自动判据）
- `action_hint` —— 可直接抄的命令

---

## 五、`board --json`：进度与下一步

```json
{
  "task": "UGE01",
  "stages": [{"stage": "parse", "label": "解析", "status": "done", "done": 0, "total": 0, "detail": "正文 20 段"}],
  "stages_done": ["parse"],
  "stages_todo": ["shots", "assets", "voice", "build"],
  "next_stage": "shots",
  "shots": {"total": 0, "ready": 0, "failed": 0, "by_source": {}}
}
```

`next_stage` 就是"下一步该跑哪个阶段"，不用解析中文看板。

---

## 六、`doctor --json`：开跑前的体检

返回**数组**，每项 `{name, status, detail, hint}`，`status ∈ {通过, 警告, 缺失}`。

```json
[{"name": "Python", "status": "通过", "detail": "3.13.12", "hint": ""},
 {"name": "定妆批准", "status": "警告", "detail": "5 个角色有候选图但一个都没批准", "hint": "…"}]
```

退出码：有 `缺失` → `1`，否则 `0`。

agent 应该**先跑它**：很多"跑一半才炸"的事这里就能提前发现
（缺 Pexels key / 定妆没批准 / 人脸模型不可用 / ComfyUI 不通）。

---

## 七、一条完整例子（真实任务形状）

```powershell
$P = ".venv\Scripts\python.exe"
$C = "config.雨月物语.toml"          # ★ 一本书一份 config，见下

# 0) 体检（agent 先看这个）
& $P -m lvs doctor --json
& $P -m lvs doctor --config $C

# 1) 建任务 + 跑
& $P -m lvs parse "<拍摄稿.md>" --task UGE01 --config $C
& $P -m lvs run   "<拍摄稿.md>" --task UGE01 --config $C
#   → 退出码 3，卡在 G0

# 2) 问门禁
& $P -m lvs gate --task UGE01 --config $C --json
#   → next.id = "G0"，action_hint 里有现成命令

# 3) 用户确认后放行，再跑（每道门重复）
& $P -m lvs gate --task UGE01 --config $C --approve G0 --note "稿子看过了"
& $P -m lvs run  "<拍摄稿.md>" --task UGE01 --config $C

# 4) 任何时候问进度
& $P -m lvs board --task UGE01 --config $C --json
```

**成片**：`.work/<任务名>/final.mp4`

---

## 八、agent 最该知道的六件事

**0. 续跑三件套（2026-10-05 新增；全是"省 token"的入口，先看 §3.1.1）**

- `lvs status --brief` —— **续跑第一步跑它**，别一上来读 `shots.json`。
- `lvs shots --index` / `--peek <id>` —— 想"看分镜"时用（516 KB → 18.6 KB）。
- `lvs map show/grep` —— 按行号区间读源码/文档，别再生成 `*_numbered.txt` 那类全量副本。

1. **`lvs run` = 跑到下一道门**，不是跑完。重跑即续跑。
2. **一本书一份 config**。`config.toml` 的 `[paths].lib` 指向哪本书是**固定**的，
   跑别的书必须 `--config config.<书名>.toml`（没有就 `lvs init` 生成）。
   不给就会**串库**：拿另一本书的定妆库去配这一本的槽位。
3. **打回/跳过必须写 `--note`**。跳过是显式越权（`--skip`），理由要留痕。
4. **`--skip-gates` 等于放弃人审** —— 只在"急着看个大概"时用，不能当默认。
5. **GPU 互斥**：`assets`（生图）与 `voice`（本地 TTS）不能同时开
   （8GB 显存装不下）。`guard` 会拦并说明关哪个。
6. **密钥可以只活在环境变量里**：`LVS_OPENAI_API_KEY` / `LVS_PEXELS_API_KEY`
   （环境变量**优先于** `config.toml`，写进文件会留明文副本）。
   `lvs doctor` 会说明"来自环境变量"。设置页的密钥输入框**故意永远是空的** ——
   回显的掩码一旦被提交回去，后端会直接**拒绝写入**（那是不可逆的覆盖）。

---

## 九、目前**不能**自动化的地方（诚实列出）

| 卡点 | 现状 | 绕过 |
|---|---|---|
| **门禁要人点头** | 设计如此 | agent 把产物给用户看 → 用户说行 → agent approve |
| **定妆批准** | `lvs cast --approve` 要人看候选图 | 同上：把 `_审阅表.png` 给用户看 |
| **`cast` 没有 `--json`** | 只能读散文 | 暂无（候选：加 `--json`） |
| **`library` / `image` / `clean` 没有 `--json`** | 同上 | 暂无 |
| **人脸巡检不可用** | `[qc].face_model]` 那个权重是坏的 | 见 `docs/一致性现状核对.md` 第四节 |

前两条不是缺陷，是**设计的护栏**：几百镜的代价太高，不给人点头就往下跑是错的。
