# Anima Studio

对话式动漫文生图网页应用。用自然语言和 AI 聊,它把你说的话写成英文提示词,交给本地
**ComfyUI** 跑 **Anima 2B DiT** 模型出图;出完还能**把图拖回去给 AI 看**,让它分析构图、
视角、人物神态,再按你的意思改。

是「**Anime Wen**」项目的一部分(整项目 = ComfyUI 出图 + Anima Studio 网页 + Next Trainer 训练)。

---

## 先读哪份文档

| 你要做的事 | 读这份 |
|---|---|
| **把它部署到云 GPU(AutoDL)** | [**云部署-给下一个AI.md**](云部署-给下一个AI.md) |
| **在本机维护/改代码** | [**维护交接-给下一个AI.md**](维护交接-给下一个AI.md) |

两份都是写给**接手的 AI** 的,含大量实测结论和已踩过的坑。改代码前请先读维护交接文档。

---

## 这个仓库里有什么

| 文件 | 说明 |
|---|---|
| `app.py` | Flask 后端(1700+ 行,单文件)。DeepSeek 流式对话、提示词提取、ComfyUI 调度、分支树会话、图片回传视觉分析、角色外貌搜索 |
| `templates/index.html` | 全部前端(单文件,内联 CSS/JS)。聊天流、拖拽回传图片、图库缩略图带、提示词规则编辑器 |
| `config.example.json` | 配置**空壳模板**。含完整的提示词规则 + 4 个预设,**密钥位置是占位符** |
| `tests/` | 6 个测试(含图片回传的单元测试与 E2E) |
| `requirements.txt` | Python 依赖(只有 flask / requests / websocket-client 三个) |

## 这个仓库里**没有**什么

以下都被 `.gitignore` 排除,属**正常**——它们是数据或密钥,不属于代码:

| 没有的东西 | 原因 | 怎么得到 |
|---|---|---|
| `config.json` | **含真实 DeepSeek API Key**(以及它的 `.bak` 备份) | `cp config.example.json config.json`,再填密钥。或改用环境变量 `DEEPSEEK_API_KEY` |
| `sessions.json` | 私人对话历史 | 运行时自动生成 |
| `images/` | 用户生成的图(180MB) | 运行时自动生成 |
| `.venv/` | Windows 专用虚拟环境 | `python -m venv .venv && pip install -r requirements.txt` |

**ComfyUI 本体和 Anima 模型也不在这里**——那些是可重新获取的:
ComfyUI 用 `git clone` 拉,Anima 模型从
[huggingface.co/circlestone-labs/Anima](https://huggingface.co/circlestone-labs/Anima) 下
(确切要哪三个文件、多大、放哪个目录,见云部署文档 §5)。

---

## 快速开始(本机 Windows)

```bash
# 1. 配置
cp config.example.json config.json
#    编辑 config.json,把 api_key 换成你的 DeepSeek Key

# 2. 装依赖
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt

# 3. 先起 ComfyUI(8188),再起本应用
.venv/Scripts/python.exe app.py     # → http://127.0.0.1:7861
```

> 8GB 显存需给 ComfyUI 加 `--lowvram`。云上 16GB+ 请去掉。

---

## 技术要点

- **模型**:Anima 2B DiT(CircleStone Labs × Comfy Org),动漫特化,30 步 / CFG 4 / `er_sde`
- **对话模型**:DeepSeek `deepseek-flash`(V4.1,支持图片输入)
- **会话是分支树**,不是线性消息列表——每条消息可以编辑/重新生成,旧版本保留可切换
  (动手前务必读维护交接文档第 5 节)
- workflow 只用 ComfyUI **内置节点**,**不需要装任何自定义节点**

## 许可

本仓库代码为用户自有项目。**注意 Anima 模型本身是
[CircleStone Labs Non-Commercial License(非商用)](https://huggingface.co/circlestone-labs/Anima)**,
使用前请确认用途符合其条款。
