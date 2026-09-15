# Anima Studio — AI 回答「重新生成」（分支版本）设计

日期：2026-09-02
项目：Anima Studio（`E:\anima-studio`，Flask + DeepSeek + ComfyUI，端口 7861）
参考：桌面《deepseek网页分支.txt》（DeepSeek 网页端分支/重发逻辑）

## 目标

给**每条 AI 回答**加「🎲 重新生成」：在不改变用户提问的前提下，换一个 AI 回答，旧回答作为兄弟分支保留，可用 `‹ 第 X/N 版 ›` 切回。

不做的（本次范围外）：数据模型重构为扁平树 + `activePath`、复制按钮、Timeline 树视图、`preserve` 重放策略。

## 现状（改动前）

- 会话是**分支模型**：`SESSIONS[sid] = {title, updated, active, branches:{bid: branch}}`
- 分支：`{id, messages, prompt, negative, last_image, params, forked_from, created}`
- **用户消息**可编辑：`POST /api/session/<sid>/rewrite {index}` → 新建分支保留 `msgs[:index]`，`forked_from.type="rewrite"`；前端 `✏️ 编辑` + `‹ 第 X/N 版 ›`
- 版本组：`_version_groups()` 按 `rewrite` 血缘把用户消息归组，`get_session` 返回 `versions`
- **AI 回答无重新生成**，无版本切换

## 设计

### 1. 数据模型（扩展现有分支模型，不重构）

新增一种分叉类型 `forked_from.type = "regenerate"`，与现有 `"rewrite"` 对称。

- `POST /api/session/<sid>/regenerate {index}`
  - `index` 为目标 **assistant** 消息在活跃分支中的位置
  - 校验：`0 < index < len(msgs)` 且 `msgs[index].role == "assistant"`，否则 400
  - `prefix = [dict(m) for m in msgs[:index]]`（截到该回答前面的用户消息为止，**不含**原回答）
  - `prompt` / `negative` 回退到 `prefix` 中最后一条 assistant 消息（与该回答之前的上下文一致；通常与当前分支一致）
  - 新建分支：`forked_from={"branch": src_id, "index": index, "type": "regenerate"}`，`s["active"] = 新分支`
  - 原分支**不删除**
  - 返回 `{"ok": true, "branch": bid}`

- `POST /api/chat` 增加 `regenerate` 布尔参数
  - `regenerate=true` 时：**不追加新用户消息**（分支现有历史末尾就是那条用户提问），不做自动搜索；直接用分支历史组装 `messages` 调 DeepSeek，流式返回；完成后把新 assistant 消息追加到当前分支，并更新分支的 `prompt`/`negative`
  - `regenerate=false`（默认）行为不变
  - 校验：`regenerate=true` 时要求 `msgs` 非空且 `msgs[-1].role == "user"`，否则 400

- 新增版本组函数（与 `_version_groups` 对称）
  ```python
  def _regen_root(branches, branch, index):
      # 沿 forked_from.type=="regenerate" 且 index 相同的链向上回溯到根分支 id
      ...

  def _assistant_version_groups(s, active_branch):
      # 遍历活跃分支里的 assistant 消息，按 _regen_root 归组兄弟分支
      # 返回 {str(index): {"branches": [按 created 排序的分支id], "active": 位置}}
  ```
- `get_session` 返回值新增 `"assistant_versions": _assistant_version_groups(s, b)`（`versions` 保持不变）

### 2. 前端

- 每条 **assistant** 消息的操作栏加 `🎲 重新生成` 按钮 → `regenerateMessage(i)`
- assistant 消息下方显示 `‹ 第 X/N 版 ›`（当 `assistantVersions[i].branches.length > 1`），复用现有 `.ver-nav` 样式与 `switchVersion` 逻辑（按角色选 `assistantVersions` 或 `versions`）
- 把流式发送抽成公共函数 `streamChat(body)`：
  - `sendMessage(msg)` = 追加用户气泡 + `streamChat({session_id, message: msg})`
  - `regenerateMessage(i)` = `POST /regenerate {index:i}` → `openSession` → `streamChat({session_id, regenerate:true})`
- `openSession` 读取并保存 `assistantVersions = j.assistant_versions || {}`

### 3. 交互语义

- 重新生成**最后一条**回答：新回答替换显示，旧回答用 `‹/›` 切回
- 重新生成**中间某条**回答：新分支从该回答处截断，其后的对话保留在旧分支，切回旧版可见完整后续
- 旧的 AI 回答永不删除；与「编辑用户消息」的现有行为完全对称

## 边界与错误处理

- `index` 越界或不是 assistant → 400，前端 alert
- 未设 API key → `/api/chat` 返回 400（沿用现有逻辑）
- 流式失败 → 复用现有 error 事件渲染
- 分支存储沿用现有 `save_sessions()`；旧会话数据无需迁移（只是多一种 fork 类型）

## 验证

- 隔离测试（Flask test client + stub `save_sessions`）：
  1. 造 `[u0,a0,u1,a1,u2,a2]`，`/regenerate {index:3}` → 新分支 `[u0,a0,u1]`，`forked_from.type=="regenerate"`，active 切换，原分支仍在
  2. 模拟 chat 追加新 a1' → `assistant_versions["3"]` = `[原分支, 新分支]`，`active=1`
  3. 再重生一次 → 3 个版本，顺序按 created
  4. `/switch` 回原分支 → 恢复 `[u0,a0,u1,a1,u2,a2]`，`assistant_versions["3"].active==0`
  5. 越界 / 非 assistant index → 400
- 语法：`py_compile` + 内联 JS `node --check`
- 重启服务后前端页面含 `regenerateMessage` / `assistantVersions` / `/regenerate`
