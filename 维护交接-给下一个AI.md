# Anima Studio 维护交接文档（给下一个 AI）

> 本文件是 **Anima Studio**（`E:\anima-studio`）的当前状态交接。桌面上的《Anime-Wen-项目交接-给下一个AI.md》覆盖整个 Anime Wen 项目（含 ComfyUI / Next Trainer），但其中 **Studio 的会话模型、搜索方式已过时**——以本文件为准。
> 全文都是本机实测结论，**别再重踩坑**。最后更新：2026-09-15。
>
> ⚠️ **本文件有两份副本，内容必须一致**：
> `E:\anima-studio\维护交接-给下一个AI.md`（随代码走，会传上 GitHub）
> 和桌面 `文档\Anima-Studio-维护交接-给下一个AI.md`（方便随时翻）。
> **改完一份记得同步另一份。**
>
> 📄 **另见《云部署-给下一个AI.md》**——如果你要做的是"把这套东西部署到 AutoDL 云 GPU"，
> 读那一份。本文件讲的是**本机（Windows / RTX 5060 8GB）**怎么维护。

---

## 0. 先看这三条（最重要的）

1. **8GB 显存是最大约束**：ComfyUI 必须 `--lowvram`；**永远别同时跑训练 + 出图**（会 OOM，还可能触发笔记本 0x139 蓝屏）。
2. **改 `app.py` 必须重启服务才生效**；改 `templates/index.html` 有 `TEMPLATES_AUTO_RELOAD=True` 但**重启最保险**。
3. **会话是「分支树」模型**（不是线性消息列表）——动手前先读第 5 节，否则会写错。

---

## 1. 机器环境（硬事实）

| 项 | 值 |
|---|---|
| 系统 | Windows 11 Home China（命令用 Git Bash / PowerShell） |
| GPU | **RTX 5060 Laptop，8GB 显存**，Blackwell sm_120（nvfp4 原生） |
| 大数据盘 | `E:\`（ComfyUI、Studio、Next-Trainer 都在 E） |
| 笔记本 | 满载偶发 **0x139 蓝屏** |
| Studio venv | `E:\anima-studio\.venv`（Flask 3.1.3 / requests 2.34.2 / websocket-client 1.9.2） |

> ⚠️ **Python 解释器**：进程可能显示为 uv 管理的 `cpython-3.11`（venv 是 uv 建的），但**启动入口永远是 `.venv\Scripts\python.exe`**。跑脚本/测试一律用它，例如：
> `/e/anima-studio/.venv/Scripts/python.exe xxx.py`

---

## 2. 项目组成（Anime Wen 三子系统）

```
① ComfyUI Anima 出图引擎     E:\ComfyUI                          端口 8188
② Anima Studio 网页应用      E:\anima-studio                     端口 7861  ← 本文件重点
③ Next Trainer 训练环境      E:\next-engine\Next-Trainer-v3.0.0-kohya  端口 28000
```

- ① 生图引擎：Anima 2B base 三件套（`anima-base-v1.0.safetensors` DiT + `qwen_3_06b_base.safetensors` 文本编码器 + `qwen_image_vae.safetensors`），硬链接自 ③，不占双份空间。蓝图 `E:\ComfyUI\blueprints\Text to Image (Anima).json`。采样 30 步 / CFG 4.0 / `er_sde`+`simple` / 1024²。Anima **支持负向**。
- ③ 训练：训 Anima DiT LoRA（Kohya 预装，cu128）；**Anima Fast 引擎未装**。⚠️ 训完别动 `sd-models\anima\`（ComfyUI 硬链接依赖它）。

**启动顺序**：先桌面 `启动ComfyUI.bat`（8188）→ 再桌面 `启动AnimaStudio.bat`（7861）。

---

## 3. Anima Studio 架构与文件

```
Flask 网页 UI  ↔  DeepSeek API（流式 deepseek-flash = V4.1，**支持图片输入**）  ↔  本机 ComfyUI(8188) 生图
```

| 文件 | 说明 |
|---|---|
| `app.py` | 后端，**1718 行**（Flask 路由 + 会话/分支 + 搜索 + ComfyUI 调用 + 图片回传） |
| `templates/index.html` | 前端**单文件** UI，**1219 行**（HTML+CSS+JS 全在一起） |
| `config.json` | 配置：`api_key` / `rules` / `presets` / `search` |
| `sessions.json` | 会话持久化（**分支格式**，见第 5 节），约 400KB。有备份 `sessions.json.bak-*` |
| `images/` | 出图输出。带 `.vision.jpg` 后缀的是**回传给 AI 看的压缩副本**，不是作品（图库已过滤，见 10.6） |
| `tests/test_baike_search.py` | 百度百科搜索源回归测试（橘望三段齐全 / 砂狼白子章节名匹配 / 不存在词条返回 None / 带前缀链路） |
| `tests/test_vision_history.py` | 图片回传回归测试（**纯单元、不联网**：落盘命名/幂等、目录穿越防护、多模态数组、只留最近一次、去重、无图历史回归） |
| `tests/e2e_vision_live.py` | 图片回传**真实链路**端到端：**HTTP 层**打后端（不需要浏览器，会真调 DeepSeek 花额度，需 ComfyUI 的 python 才有 Pillow） |
| `tests/e2e_dragdrop_browser.mjs` | 拖拽回传**浏览器内逻辑**回归（**不花钱**，~10s，需服务在跑 + Chrome）。stub 掉 `streamChat` 只验请求体装对了没 |
| `tests/e2e_dragdrop_live.mjs` | 拖拽回传**浏览器内真链路**（走完整用户路径：拖图→点发送→真请求，**花额度**） |
| `docs/superpowers/specs/` | 设计文档（如 `2026-09-02-regenerate-design.md`） |
| `.venv/` | 独立虚拟环境（uv 创建） |

**启动**：桌面 `启动AnimaStudio.bat` → 内容就是
```bat
cd /d E:\anima-studio
.venv\Scripts\python.exe app.py
```
→ `http://127.0.0.1:7861`（**需 ComfyUI 8188 先开**，否则生图会报「无法连接 ComfyUI」）

---

## 4. 启动 / 重启 / 排障

**手动重启服务（改完 app.py）**：
```bash
PID=$(netstat -ano | grep ':7861' | grep LISTEN | awk '{print $5}' | head -1)
[ -n "$PID" ] && powershell -NoProfile -Command "Stop-Process -Id $PID -Force"
sleep 2 && (netstat -ano | grep ':7861' | grep LISTEN || echo "port free")
cd /e/anima-studio && ./.venv/Scripts/python.exe app.py   # 前台/后台均可
```
> ⚠️ **坑**：`Stop-Process` 偶尔没杀干净，端口仍被占。**务必确认 `port free`** 再启动；否则新进程起不来（或起在旧代码上）。必要时对新查到的 PID 再杀一次。

**分层排障**（别一次堆完）：单机 ComfyUI 出图 → 后端 API → 前端 UI。

---

## 5. 数据模型：分支树 ★核心

**不要再把它当线性 `messages[]`。** 会话是「会话 → 分支 → 分支内线性消息」：

```python
SESSIONS[sid] = {
    "title": str,
    "updated": float,
    "active": bid,                 # 当前活跃分支 id
    "branches": { bid: branch },
}

branch = {
    "id": bid,
    "messages": [ {role, content, prompt?, negative?} ],   # role: user / assistant / search
    "prompt": str,                 # 该分支当前的提示词（右侧面板）
    "negative": str,               # 该分支当前的负向词
    "last_image": str,
    "params": {seed, steps, width, height, cfg},
    "forked_from": {"branch": bid, "index": int, "type": str} | None,   # 分叉来源
    "created": float,
}
```

**两种分叉类型**（`forked_from.type`）：

| type | 触发 | 语义 |
|---|---|---|
| `"rewrite"` | 编辑**用户消息** | 保留 `msgs[:index]`（不含被编辑的那条），新分支随后由 `/api/chat` 追加编辑后的用户消息 |
| `"regenerate"` | **重新生成 AI 回答** | 保留 `msgs[:index]`（正好截到该回答前面的用户提问），新分支随后由 `/api/chat {regenerate:true}` 直接续写回答 |

**版本组**（前端 `‹ 第 X/N 版 ›` 的依据）：
- `_version_groups(s, b)` → 用户消息的版本组，靠 `_lineage_root` 沿 `rewrite` 血缘回溯
- `_assistant_version_groups(s, b)` → AI 回答的版本组，靠 `_regen_root` 沿 `regenerate` 血缘回溯
- 返回 `{ "索引": {"branches": [按 created 排序的分支id], "active": 当前位置} }`
- `GET /api/session/<sid>` 同时返回 `versions`（用户）和 `assistant_versions`（回答）

**要点**：
- 旧分支/旧消息**永不删除**，作为兄弟分支保留，`/switch` 随时切回。
- 切换分支后整个对话长度可能变化（新分支在分叉点截断，旧分支保留后续）——这是**正确行为**。
- 每次调 DeepSeek 时，只把**当前活跃分支**的消息转成 `messages`（`_build_history`）。
- `forked_from` 为 `None` 的是根分支。

**旧数据迁移**：`load_sessions()` 会把老的线性格式（`{messages, prompt, ...}`）自动迁成单分支，原对话完整保留。**无需手动处理**。

---

## 6. API 路由清单（`app.py`）

| 路由 | 方法 | 说明 |
|---|---|---|
| `/` | GET | 前端页面 |
| `/api/key` | GET/POST | 读/写 DeepSeek key |
| `/api/rules` | GET/POST | 读/写当前规则 |
| `/api/rules/presets` | POST | 新建预设（同名 409） |
| `/api/rules/presets/<name>` | PUT/DELETE | 更新/删除预设 |
| `/api/rules/presets/<name>/rename` | POST | 重命名预设 |
| `/api/search/settings` | GET/POST | 搜索来源/开关/serper key |
| `/api/search` | POST | 手动搜索角色（带 `session_id` 则把结果注入会话） |
| `/api/session/new` | POST | 新建会话（含一个根分支） |
| `/api/sessions` | GET | 会话列表 |
| `/api/session/<sid>` | GET/DELETE | 读取（含 versions/assistant_versions）/删除会话 |
| `/api/session/<sid>/prompt` | POST | 设当前分支提示词 |
| `/api/session/<sid>/negative` | POST | 设当前分支负向词 |
| `/api/session/<sid>/rewrite` | POST | **编辑用户消息**：`{index}` 建 rewrite 分支 |
| `/api/session/<sid>/regenerate` | POST | **重新生成 AI 回答**：`{index}` 建 regenerate 分支 |
| `/api/session/<sid>/switch` | POST | 切换活跃分支：`{branch}` |
| `/api/chat` | POST | 流式聊天（SSE）。`{session_id, message}` 或 `{session_id, regenerate:true}`；**可带 `images`**（见 10.6），带图时 `message` 可为空 |
| `/api/generate` | POST | 生成 1 张 |
| `/api/generate_multi` | POST | 对比生成 `{n}` 张 |
| `/api/generate/stop` | POST | 停止（ComfyUI /interrupt） |
| `/api/progress` | GET | 生图进度（前端 800ms 轮询） |
| `/api/logs` | GET | 生成日志 |
| `/api/images` | GET | 图库列表（**已过滤 `.vision.jpg` 副本**） |
| `/images/<name>` | GET | 图片文件 |

---

## 7. 关键后端函数（改代码前先读这些）

| 函数 | 作用 |
|---|---|
| `_new_branch()` / `_branch(sid)` | 建分支 / 取 (会话, 活跃分支) |
| `_lineage_root()` / `_version_groups()` | 用户消息版本组（rewrite 血缘） |
| `_regen_root()` / `_assistant_version_groups()` | AI 回答版本组（regenerate 血缘） |
| `load_sessions()` / `save_sessions()` | 会话读写（含旧格式迁移） |
| `extract_prompt()` / `extract_negative()` | 从 AI 输出提取 `<prompt>/<negative>`，**带容错兜底**（LLM 常写错闭合标签） |
| `_build_history(msgs)` | 把分支消息转成 DeepSeek messages；search 卡片转成「用户提供的资料」；**带图消息转成多模态 content 数组**（见 10.6） |
| `_save_vision_images(items)` | **回传图落盘**：前端压好的 JPEG data URL → `images/<原图主干>.vision.jpg`；校验体积、去重、坏输入走 errors 不炸 |
| `_image_block(fname)` | 把 `images/` 下图片编成 `{type:"image_url",…}` 块；文件不存在/超 32MB 返回 `None`。**必须 basename 防目录穿越** |
| `_clean_query()` | 搜索前去掉系列前缀（`FRANCHISE_WORDS`） |
| `search_character_deadline()` | **带 15s 超时预算**的角色搜索（线程 join 兜底） |
| `_appearance_chunk()` / `_clean_appearance_text()` | 从萌百 extract 提取外貌（优先「人物设定/外貌」章节，限短标题） |
| `_moegirl_infobox()` | **抓萌百渲染 HTML 解析信息框**（发色/瞳色/身高等），带内存缓存 |
| `search_baike()` | **百度百科角色资料**（首选源）：信息框 + 身份背景 + 性格特点 + 衣着外貌；结果带缓存 |
| `_baike_page_data()` | 抓 **wapbaike** 页面，从 `__NEXT_DATA__` 取 `pageData`（结构化正文都在这里） |
| `_baike_sections()` / `_baike_infobox()` | `structuredContent` 拆章节 / `card.content` 取信息框（`_baike_text_of` 做节点转文本） |
| `_baike_pick_lemma()` | **多义词义项消歧**：从 `navigation.lemmas` 挑属于目标作品的义项（见坑 ⑮） |
| `_franchise_hint()` | 从用户原始查询里提作品名（`蔚蓝档案橘光`→`蔚蓝档案`），供消歧/校验 |
| `_baike_relevant()` | 带作品提示时校验词条确实属于该作品，防止撞名给错人的资料 |
| `detect_character()` | 纯角色名提取（**自动搜索已关，默认不走**） |
| `comfy_workflow()` | Anima API 工作流（UNETLoader base / CLIPLoader type=stable_diffusion / EmptyLatentImage / KSampler er_sde / 负向 CLIPTextEncode） |
| `comfy_generate()` / `comfy_wait()` | 提交/等待 ComfyUI |
| `_watch_progress()` | WebSocket 线程，监听 ComfyUI progress → 写全局 `PROGRESS` |
| `_do_generate()` | 生成主流程（读活跃分支的 prompt/negative/params） |

---

## 8. 前端结构（`templates/index.html`，单文件）

- 状态变量：`currentSession` / `currentMessages` / `currentPrompt` / `currentVersions` / `assistantVersions` / `lastSeed` / `currentPreset`
- 渲染：`messageHtml(i, m)`（每条消息 → HTML，含 `‹/›` 版本栏和操作按钮）、`renderChat()`
- 聊天流：`send()` → `sendMessage(msg)`（追加用户气泡）→ **`streamChat(body)`**（公共流式，`sendMessage` 和 `regenerateMessage` 共用）
- 编辑/重生：`editMessage(i)` / `submitEdit(i)` / `cancelEdit(i)`、`regenerateMessage(i)`
- 版本切换：`switchVersion(i, dir, role)` → `switchBranch(bid)`（`role` 选 `versions`/`assistantVersions`）
- 生图：`generate()` / `generateMulti()` / `startProgressPolling()`
- 规则预设 CRUD：`renderRulesList()` / `selectPreset()` / 三个按钮的 handler
- 搜索：`chatSearch()`（聊天区搜索框）、`doManualSearch()`（调试面板）
- **图片回传（拖拽式）**：`pendingImages`（待发送 `[{name,url,blob}]`）、`draggableImg(img,name,url)` / `makeDraggable(root)`（把 `<img>` 变成拖动源）、`addPending` / `removePending` / `clearPending` / `renderPending`（`#pendingRow`）、`handleDrop(dt)`（三种来源）、`initDropZone()`（`main` 上的 drag 监听 + `depth` 计数）、`loadStrip()`（`#stripItems` 缩略图带）、`compressToJpeg(url,...)`（canvas→JPEG，**在发送时才压**）
- 布局：左侧会话栏 / 中间聊天+搜索框+输入框 / 右侧提示词+参数+图 / 底部调试抽屉（规则·搜索·图库·日志）

**消息操作栏**：
- 用户消息：`✏️ 编辑`
- AI 回答：`🎲 重新生成`（+ `📋 恢复提示词`，若有 prompt）

---

## 9. 已实现功能清单

| 功能 | 说明 |
|---|---|
| 对话生图 | DeepSeek 流式中文讨论，提取 `<prompt>/<negative>` |
| 生图进度条 | 后端连 ComfyUI `/ws`，`/api/progress` 轮询渲染（0→30 步） |
| **编辑用户消息 → 版本** | `✏️ 编辑`→就地改→新增 rewrite 分支；`‹ 第 X/N 版 ›` 切换 |
| **重新生成 AI 回答 → 版本** | `🎲 重新生成`→新增 regenerate 分支；`‹ 第 X/N 版 ›` 切换 |
| **手动搜索角色** | 聊天区搜索框 + 调试面板；结果以文字卡片进对话。**首选百度百科**，注入「信息框 + 身份背景 + 性格特点 + 衣着外貌」 |
| 规则预设 CRUD | 调试面板→提示词规则：列表+编辑器+保存/使用/删除 |
| 生成参数 | 步数/CFG/宽高/seed/负向词可调，对比生成、停止 |
| 尺寸预设 | 12 个常用尺寸+自定义；实测 8GB 下 512²~2048² 全可跑（2048² 约 5 分钟） |
| **图片回传（拖拽式）** | **把图拖进聊天区**即可回传给 AI 看：输入框上方常驻的图库缩略图带、完整图库、右侧当前图、以及从文件夹直接拖进来的图片文件都能拖。松手只是**附带**，可以再写关注点、继续拖第 2、3 张，点发送一起发。**这一轮只分析、不出提示词**（见 10.6） |
| 图库 / 日志 | 调试面板查看 |

---

## 10. 关键机制详解

### 10.1 提示词提取（`extract_prompt` / `extract_negative`）
AI 用 `<prompt>...</prompt>` 和 `<negative>...</negative>` 包裹英文正/负向词。**LLM 常把闭合标签写错**（如 `<negative>...</prompt>`），所以提取有**容错兜底**：标准标签优先，找不到闭合就截到下一标签/空行。**改标签格式必须同步改这两个函数**。

### 10.2 版本分支（见第 5 节）
- 编辑用户消息 = `rewrite`；重新生成回答 = `regenerate`；`/switch` 切换；版本组算 `‹/›`。
- 前端 `submitEdit`/`regenerateMessage` 都是：建分支 → `openSession` 重渲染 → `streamChat` 续写。

### 10.3 生图进度
`comfy_generate()` 提交后起 `_watch_progress` 线程连 ComfyUI `/ws`，把 `progress`/`execution_success`/`execution_error` 写进全局 `PROGRESS`；`/api/progress` 返回 `{status,value,max,gen_index,gen_total}`。

### 10.4 联网搜索（已改为**手动为主**）
- **自动搜索默认关**（`config.json` 的 `search.auto=false`）。原因见踩坑 ⑨。
- **手动**：聊天区搜索框 / 调试面板 → `/api/search` → `search_character_deadline()`。
- 搜索前 `_clean_query()` 去掉系列前缀（`蔚蓝档案 砂狼白子` → `砂狼白子`）。
- **15s 超时预算**（`search_character_deadline` 用线程 join 兜底），超时返回 `timeout:true`。
- 来源优先级：**百度百科 → 萌娘百科 → 维基 → Serper（需 key）**，取第一个有结果的。
- **百度百科（质量最高、首选、最快 ≈0.3s）**：`https://wapbaike.baidu.com/item/<名>` → 从页面
  `<script id="__NEXT_DATA__">` 取 `props.pageProps.pageData`，用其中的 `lemmaDesc`（一句话定位）、
  `card.content`（信息框：身高/生日/年龄/登场作品…）、`structuredContent`（章节正文）。
  产出形如 `【信息框】…【身份背景】…【性格特点】…【衣着外貌】…`（≈1400 字，总上限 2200）。
  **章节名各词条不统一**（橘望=`衣着外貌`，砂狼白子=`相貌衣着`），所以按关键词分组匹配：
  身份组 `背景/身份/简介/设定`、性格组 `性格`、外貌组 `外貌/相貌/衣着/外观/造型/服饰/服装/装扮`，
  标签用**原章节名**。抓不到三类章节（如非游戏角色）时兜底给信息框 + 概述段。
- **注入内容已不止外貌**：身份/性格用于让 AI **理解用户指令**（"让她生气"该配什么神态），
  外貌用于写提示词。注入块的 `【…】` 标签名随词条变化，规则里写的是「等章节」。
- **多义词消歧（必读坑 ⑮）**：`_clean_query()` 会把作品名前缀去掉，但**去掉前先用
  `_franchise_hint()` 把作品名留下当 hint**，一起传进 `search_baike(name, hint)`。
  裸词条对多义词会跳到**错误的义项**，必须靠 hint 挑；挑不到就返回 None 让下一个源兜。
  作品名对照表见 `FRANCHISE_ALIASES`（含 `蔚蓝档案`/`碧蓝档案`/`blue archive` 等译名）。
- **萌百外貌提取**：extract API 只有正文（常缺发色/瞳色，且在模板里）。所以当 extract 缺「发色+瞳色」时，`_moegirl_infobox()` 抓**渲染后的页面 HTML** 解析信息框补上，结果内存缓存（首次约 5~10s，重复约 1.2s）。

### 10.5 规则预设
预设存 `config.json` 的 `presets`；页面上「使用当前预设」把内容设为 `rules`（新消息立即生效）。
`rules` 与 4 个预设各自持有一份规则副本，**改条款要一起改**（见坑 ⑯）。预设的章节格式并不统一
（`第二预设` 用 `## 零、…`，`Anima 默认` 用 `【联网搜索说明】`，`我的预设` 用 `【搜索与资料使用规则】`），
**用固定锚点批量改会漏**，得逐个确认。

### 10.6 图片回传（拖拽式）★新
**用户把图拖进聊天区**（不是点按钮——原来那个「🔍 传回去分析」按钮已按用户要求删掉）→ 松手只**附带**到
`#pendingRow`，可继续拖、可写关注点 → 点发送时才**压成 JPEG** 发出去 → AI 看图分析构图。
- **四个拖动源**：① 输入框上方常驻的**图库缩略图带**（`#stripItems`，最近 40 张，主要入口）；
  ② 调试抽屉里的**完整图库**（`.g-item`）；③ 右侧**当前生成图**（`#imagePanel`，`setImage()` 里统一挂）；
  ④ **从系统文件夹直接拖图片文件**进来（`dataTransfer.files` → `URL.createObjectURL`，标记 `blob:true` 以便回收）。
- **投放区是整个中间栏**（`main`），`dragenter/leave` 用 `depth` 计数避免掠过子元素时高亮闪烁；
  悬停加 `.drop-hover`（虚线框）。只在携带图片的拖动上 preventDefault——**别把普通文字拖拽也吃掉**。
- **松手不发送**（用户明确要的）：附带的图显示成带 ✕ 的小缩略图，输入框 placeholder 换成
  「写下你想让它注意什么…」；发送后 `pendingImages` 立刻清空、blob URL 延迟到发完才 `revokeObjectURL`
  （**顺序错了会压不出图**——清空时就把 blob 撤了，`compressToJpeg` 会加载失败）。
- **压缩时机在发送时**，不在拖入时：拖入要秒回，而且用户可能拖完又移除。
- **同图去重**按文件名，重复拖同一张不会叠加。
- **模型**：`DEEPSEEK_MODEL = "deepseek-flash"`（= V4.1），**支持图片输入**。旧值 `deepseek-chat`
  会被服务端**静默别名**成 `deepseek-flash`（响应 `model` 字段会回 `deepseek-flash`），已显式写死。
- **图片不进 `sessions.json`**（那文件已 400KB+，塞 base64 会撑爆）。消息里**只存文件名**：
  `{role:"user", content:"<用户关注点>", images:["anima_00080_.vision.jpg"]}`，
  base64 在 `_build_history()` 里**临时编码**，请求完即释放。
- **命名幂等**：`images/<原图主干>.vision.jpg`（`anima_00080_.png` → `anima_00080_.vision.jpg`），
  重传同一张不堆文件。**图库 `/api/images` 已过滤该后缀**（副本不是作品），但 `/images/<name>` 仍可直链——
  聊天气泡的缩略图靠它。
- **只保留最近一次**：`_build_history` 扫出**最后一条**带 `images` 的消息给真图，
  更早的降级成文字占位 `[此前回传过图片：…]`。原因：多轮带图会让请求体线性膨胀。
- **去重**：同一消息里重复文件名用 `dict.fromkeys()` 只编一次（防重复计费）。
- **MIME 靠扩展名推**（`_image_block`）：`.png`→`image/png`、`.webp`→`image/webp`、其余→`image/jpeg`。
  ⚠️ 所以**必须是真 JPEG**，前端 canvas 导出就是 JPEG，对得上；别拿 PNG 字节存成 `.vision.jpg`。
- **限制**：单张 base64 ≤8MB、一次最多 6 张（`VISION_MAX_IMAGES`）、总量 ≤30MB；
  单张落盘 >32MB 直接拒绝。任一图失败只发 `warning` 事件，**不整体失败**。
- **这一轮不出提示词**：靠两层保证——
  ① 后端每条带图消息都注入 `VISION_INTRO`（明说"不要输出 `<prompt>` 标签，除非用户明确说要生成/修改"）；
  ② `config.json` 的 `rules` 和 4 个预设里都有「图片反馈规则」条款。
  用户下一句说「把背景改成夜晚」时才恢复出 `<prompt>`（E2E 已验证这条边界）。
- **实测数据**（1440p / 1024px JPEG）：单张约 **384 tokens**（**与分辨率无关**，压缩只省传输不省 token）；
  1 张 ≈12~15s，4 张对比 ≈12s。请求体上限 48MiB。支持 JPEG/PNG/GIF/WebP。
- **压缩在前端**：`compressToJpeg()` 用 canvas（最长边 ≤1024、q0.85、白底），**因为服务端 `.venv` 没装 Pillow**
  （只有 flask/requests）。要跑 `tests/e2e_vision_live.py` 得借 ComfyUI 的解释器（它自带 Pillow）。
- **兼容分支树**：`sendMessage(msg, attach)` 只是多传一个 `images` 字段走**同一个 `streamChat()`**，
  流式/版本分支/`✏️ 编辑`/`🎲 重新生成` 全部免费继承（它们只搬消息内容，图跟着 branch 走）。
  不传 `attach` 时行为与改动前**完全一致**（有回归断言守着）。
- **气泡里的缩略图是 `.vision.jpg`**：发送瞬间本地气泡用的是原图 URL，但流结束后
  `streamChat` 会 `openSession()` 重渲染，那时读的是服务端存的文件名（`.vision.jpg`）——两边都对。
- **拖拽 E2E 别用 `innerText` 找 `<prompt>`** ⚠️：`renderContent()` 把它转成
  `<span class="prompt-block">` 了，字面量根本不在文本里，这么写断言会**永远通过**（本功能就写过一次假断言）。
  可靠信号是 `done` 事件有没有把 `promptArea` 填上。
- **回传哪张图由用户拖决定**，不再依赖会话记录：改成拖拽后，`openSession()` 不用再兜底存 `currentImages`
  （多图那组本来就不持久化到前端的问题**顺带消失了**——图库缩略图带里永远有全部图）。

---

## 11. 踩坑记录（本机实测，别再踩）

1. **模板缓存**：`debug=False` 时 Flask 缓存模板，已加 `TEMPLATES_AUTO_RELOAD=True`，但**改完重启最保险**。改 `app.py` **必须重启**。
2. **LLM 闭合标签写错**：`<negative>...</prompt>` 这类，提取逻辑必须有容错兜底；规则里也要强调闭合标签写对。
3. **规则死胡同（最大坑）**：规则里**绝不能出现「停止输出等待 X」**（AI 无法自行搜索/等待）。任何分支都要落到「生成」或「提问」。
4. **搜索资料质量**：元信息行（未公开/画师/同人/多种变化）会让 AI 误判「外貌未知」，已被 `META_WORDS` 过滤。
5. **8GB 显存**：不 `--lowvram` 必 OOM；别同时跑训练+出图（还触发蓝屏）。
6. **萌百反爬**：直接请求 403，必须带浏览器 UA（`SEARCH_UA`）。
7. **模型共享用硬链接**（`cmd //c mklink //H`），不复制。
8. **搜索测试的编码坑** ⚠️：在 Git Bash 用 `python -c "…中文…"` 或 heredoc 传中文**会乱码**，导致「带前缀搜索假超时」这类误判。**测试中文查询必须写成 UTF-8 脚本文件再跑**（用 Write 工具写文件，避免 shell 传参）。
9. **自动搜索会同步阻塞聊天** ⚠️：旧版自动搜索时，消息含角色名会触发 `search_character`，而它同步跑在 SSE 流开始**之前**；中文维基连不上时每个请求干等 15s × 多个 = **67 秒无响应**（表现为「不回复」）。**已改为：自动搜索默认关 + 手动搜索框 + 15s 超时兜底**。
10. **萌百信息框拿不到** ⚠️：`prop=revisions`、`action=parse` 都被萌百封（Unauthorized API call）。信息框（发色/瞳色）只能抓**渲染后的页面 HTML** 解析。
11. **萌百页面 URL 不带 `/wiki/`** ⚠️：规范 URL 是 `https://zh.moegirl.org.cn/<标题>`；用 `/wiki/<标题>` 会 404。
12. **章节匹配别太宽**：「形象」会误配「声库软件版本及形象设定」这类长标题（捞到版权说明）。外貌章节匹配要**限短标题**（`[^=]{0,4}`）。
13. **端口没杀干净**：`Stop-Process` 后务必确认端口释放再启动（见第 4 节）。
14. **百度百科只能走 wapbaike** ⚠️：`baike.baidu.com/item/<名>` 直连被「百度安全验证」拦成
    **403**；`/api/openapi/BaikeLemmaCardApi` 能通但**只有概述 + 信息框**（拿不到三段正文）；
    `api/wikiui/getlemmacontent`、`api/lemmamodule` 等模块接口全是 **404**。
    **唯一可行路径**：`https://wapbaike.baidu.com/item/<名>`，正文全在 HTML 里的
    `<script id="__NEXT_DATA__">` JSON 中（**不用解析 HTML**，直接 `json.loads`）。
    词条不存在 → 404 跳 `error.html`，没有 `__NEXT_DATA__`，按 `None` 处理（已如此实现）。
    360百科 / 搜狗百科的正文是 JS 渲染 / 反爬混淆，**别浪费时间**。实测连续请求无频率限制。
15. **百度百科多义词会跳到「错的人」** ⚠️（血泪教训）：
    `wapbaike/item/<名>` 对多义词会**默认跳到某一个义项，而那个义项经常是错的**——
    搜「橘光」跳到的是**「南京大屠杀战犯」**，根本不是蔚蓝档案角色（用户就是这么被坑的）。
    义项列表在 `pageData["navigation"]["lemmas"]`，每项含 `lemmaId` / `lemmaDesc` / `isCurrent`；
    **必须显式挑**：带作品提示时按 `lemmaDesc` 匹配该作品别名；没提示时挑 `lemmaDesc` 里含
    `FRANCHISE_ALIASES` 已知作品名的那个；**挑不到就返回 None 交给下一个源**
    （给错人的资料比没资料糟得多）。取指定义项 URL 是 `/item/<名>/<lemmaId>`。
    另：单义词也可能撞名，所以带提示时还要用 `_baike_relevant()` 校验
    `lemmaDesc + 概述` 里有没有该作品名。回归测试见 `tests/test_baike_search.py` ⑤⑥⑦。
16. **改注入标记必须同步 config** ⚠️：注入块开头的标记串（现为 `[联网搜索到的角色资料]`）
    在 `config.json` 的 `rules` **和多个预设**里都写死了；改 `_build_history` 的标记而不改
    config，AI 就认不出资料块。且有预设保存的是整套规则副本，要一起替换（已做过一次）：
    `[联网搜索到的角色外貌资料]` → `[联网搜索到的角色资料]`（`唯美厚涂` 预设不含规则，未动）。
17. **`deepseek-chat` 已经不存在了** ⚠️：`GET /models` 只回 `deepseek-flash` 和 `deepseek-v4-pro`。
    但拿 `deepseek-chat` 去调**照样返回 200**——因为服务端把它**静默别名**到 `deepseek-flash`，
    响应的 `model` 字段会老实告诉你 `deepseek-flash`。**别以为写 `deepseek-chat` 就是另一个模型**；
    现在已显式写成 `deepseek-flash`（= V4.1，带视觉）。要确认当前实际模型，看响应里的 `model` 字段。
18. **服务端 `.venv` 没装 Pillow** ⚠️：`requirements.txt` 只有 flask/requests。所以
    **任何图片处理都得放前端**（回传图的压缩就是 canvas 干的）。写服务端脚本要转图片格式时，
    借 ComfyUI 的解释器 `/e/ComfyUI/.venv/Scripts/python.exe`（它有 Pillow 12.x）。
19. **`convert` 不是 ImageMagick** ⚠️：Git Bash 里 `command -v convert` 会命中
    `C:\WINDOWS\system32\convert.exe`，那是 **FAT→NTFS 磁盘转换器**，跑下去后果严重。
    本机**没有** ffmpeg / ImageMagick / cwebp，别指望它们。
20. **`.vision.jpg` 别混进作品列表** ⚠️：回传副本和原图同放在 `images/`，
    `/api/images` 必须按后缀过滤掉，否则图库里每张图都多出一个副本。
    但 `/images/<name>` **不能**过滤——聊天气泡的缩略图要直链它。
21. **前端内联脚本的 `const`/`let` 有 TDZ** ⚠️：`index.html` 的 `<script>` 是一整块，
    顶层 `const` 写在声明之前取用会抛 `ReferenceError`，**整个脚本直接挂掉、页面看起来"没反应"**。
    加拖拽功能时就踩过：`INPUT_PLACEHOLDER = inputEl.placeholder` 写在了 `inputEl` 声明之前。
    ⚠️ **`node --check` 抓不到这个**（它只验语法）。判断办法只有真跑一遍页面——
    用 `tests/e2e_dragdrop_browser.mjs`（无头 Chrome）或手动刷新看控制台。
    另：脚本里两个 `<script>` 块**不存在**，别想着靠块间顺序绕开。
22. **`loadStrip()` 要在每次出图后刷新** ⚠️：缩略图带是常驻的、只在页面加载时拉一次，
    `generate()` / `generateMulti()` 成功后必须调 `loadStrip()`，否则刚生成的图**不在带子里、拖不到**。

---

## 12. 配置文件说明（`config.json`）

```json
{
  "api_key": "sk-…（DeepSeek）",
  "rules": "当前 AI 提示词规则（含联网搜索说明+工作流+构图/动作联想规则）",
  "presets": { "Anima 默认": "…", "唯美厚涂": "…", "我的预设": "…", "第二预设": "…" },
  "search": { "providers": ["baike","moegirl","wikipedia","serper"], "auto": false, "serper_api_key": "" }
}
```
- `api_key` 可网页右上角改，或环境变量 `DEEPSEEK_API_KEY`。
- `search.providers` **默认值在 `load_config` 与 `config.json` 里有两份**：只要文件里有这个键，
  默认值就不生效——**加新源务必同时改文件**，否则重启后还是老的三个。
- `rules`/`presets` 调试面板改，**新消息生效**。`rules` 与 4 个预设现均含**「图片反馈规则」**条款
  （对"必须输出 `<prompt>`"的豁免，见 10.6）；`Anima 默认`/`唯美厚涂`/`我的预设` 是**追加在文末**的，
  `第二预设` 与 `rules` 是插在搜索规则节之后的。
- `search.auto` **默认 false**（手动搜索）。
- 环境变量可覆盖：`COMFY_URL`(8188) / `COMFY_OUTPUT`(E:\ComfyUI\output) / `DEEPSEEK_BASE_URL` / `DEEPSEEK_MODEL`。

**写 AI 提示词规则的检查清单**（用户写规则时对照）：
- A. 系统做的事（搜索/查资料/注入）绝不让 AI 做。
- B. 无死胡同：每个「如果…」落到「生成」或「问用户」，禁止「停止/等待」。
- C. 用客观信号判断（对话里有没有 `[联网搜索到的角色资料]` 标记、有没有回传图片），不写「如果你自信」。
- D. 标签写死 `<prompt>…</prompt><negative>…</negative>` 紧挨、闭合正确、标签内纯英文；**改格式必须同步改后端提取**。
- E. 禁编造角色外貌/官方英文名，不确定就问；一次只问 1-2 个；提示词不含步数/CFG/分辨率。
- F. 写完**发真实消息测**，确认能被后端提取、能出图。

---

## 13. 待办 / 可扩展方向

- [ ] **Turbo LoRA**（`anima-turbo-lora-v0.2`，8-12 步 + CFG 1）可大幅提速，未配置
- [ ] **TeaCache 加速**节点（1.3–2.0×）未装
- [ ] Anima Fast 推理引擎（Next Trainer 内）未装
- [ ] 规则**自动联想**开关（动作/神态/构图的规则已在，未做 UI 开关）
- [ ] 文档《deepseek网页分支.txt》里**未做**的部分：复制按钮、Timeline 分支树视图、`preserve`（保留重放）下游策略
- [ ] （**已评估未做**）数据模型重构为扁平 `Map<id,node>` + `parentId` + `activePath`——用户明确**不要**，现分支模型够用
- [ ] （**已知取舍**）缩略图带只显示最近 40 张，更早的要开调试抽屉的「图库」拖
- [ ] （**已评估未做**）搜索结果卡片上显示百度百科的 `lemmaDesc`，让用户一眼确认选中的义项对不对
- [ ] （**未做**）`images/` 里的 `.vision.jpg` 副本没有清理策略（单张 ≈100KB，幂等不重复堆积）

---

## 14. 给下一个 AI 的开场白

> 目标：维护 **Anima Studio**（`E:\anima-studio`，Flask + DeepSeek + 本机 ComfyUI，7861 端口）。
> **先读本文件**，再 `nvidia-smi` 看显存，再决定改动。
> 会话是**分支树**（第 5 节），不是线性列表——改聊天/版本相关代码前务必理解 `branches` / `forked_from` / 版本组。
> 搜索是**手动为主**（自动已关），有 15s 超时和系列前缀清理；来源首选**百度百科**（走 `wapbaike` 的 `__NEXT_DATA__`，见坑 ⑭），萌百/维基/Serper 兜底。
> 改 `app.py` **必须重启**（第 4 节有重启命令）；改模板有 auto-reload 但重启最保险。
> 测试：`./.venv/Scripts/python.exe -m py_compile app.py` + 内联 JS `node --check`；中文查询测试**必须写 UTF-8 脚本文件**（第 11 节坑 ⑧）。
> 对话模型是 `deepseek-flash`（= **V4.1，支持图片输入**）；用户可以把图**拖进聊天区**回传给 AI 看图分析构图（第 10.6 节），带图那一轮**不出提示词**。
> 改 `index.html` 的 JS 后**务必真跑一遍页面**（`node tests/e2e_dragdrop_browser.mjs`）——`node --check` 抓不到顶层 `const` 的 TDZ，那会让整个脚本静默挂掉（坑 ㉑）。
> 搜索改动后跑 `./.venv/Scripts/python.exe tests/test_baike_search.py` 回归；动了视觉链路跑 `tests/test_vision_history.py`（纯单元、不花钱）。
> 排障按「单机 ComfyUI 出图 → 后端 API → 前端」分层定位。
