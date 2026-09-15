# Anima Studio 云部署文档 —— 给接手部署的 AI

> 这份文档的读者是**在 AutoDL 云机器上执行部署的 AI 助手**。
> 另一份 `维护交接-给下一个AI.md` 讲的是**这套软件怎么工作、有哪些坑**（写给本机维护用）。
> 两份配合看：这份讲"怎么在 Linux 云上把它跑起来"，那份讲"跑起来之后怎么改它"。

**最后更新：2026-09-15**

---

## 0. 先读这三句

1. **这套东西在本机（Windows）已经完整跑通了，不要重新发明。** 照着搬即可。
   任何"我觉得这样更好"的改动，先停下来问用户。
2. **前端/后端代码不用改。** 只有两处需要设置（不是改代码）：
   `COMFY_OUTPUT` 环境变量、以及访问方式（走 SSH 隧道就不用动任何代码）。
3. **别急着下模型。** 第 4 步有一个"先验证 torch 能不能用"的检查，跳过了会白下 5.4GB。

---

## 1. 目标架构

```
┌─────────────────── AutoDL 云机器（Linux） ───────────────────┐
│                                                              │
│   ComfyUI  ── 127.0.0.1:8188 ──┐                             │
│   （跑 Anima 模型，出图）        │ HTTP                        │
│                                ▼                             │
│   Anima Studio (Flask) ── 127.0.0.1:7861                     │
│   （网页 UI + DeepSeek 对话）    │                             │
│                                │ 读本地文件系统                │
│                                ▼                             │
│   /root/autodl-tmp/ComfyUI/output/  ──复制──▶ images/        │
│                                                              │
└──────────────────────────────────────────────────────────────┘
                    ▲
                    │ SSH 隧道（推荐）/ 或者暴露端口（不推荐，见 §8）
                    │
              你的浏览器 http://127.0.0.1:7861
```

**为什么全放在一台机器上**：Anima Studio 拿图的方式是
`shutil.copy(COMFY_OUTPUT/文件名, images/)`（`app.py` 第 1117 行）——**从本地文件系统读，不是 HTTP 拉**。
所以 ComfyUI 和 Flask 必须在同一台机器，否则要改代码。

**DeepSeek 是走公网的**，不占云 GPU，云端只要能联网即可。

---

## 2. 云机器怎么选

| 项 | 建议 | 理由 |
|---|---|---|
| 显存 | **≥ 16GB 舒服，24GB 很宽裕** | 本机 8GB 靠 `--lowvram` 硬扛，2048² 要跑 5 分钟。Anima 是 2B DiT，显存上去后 1024² 大约几秒到十几秒 |
| 镜像 | 选**预装 PyTorch + CUDA 12.x** 的 | 省掉 2.5GB 的 torch 下载（见 §4 的 `--system-site-packages` 技巧） |
| 数据盘 | 必须用 `/root/autodl-tmp` | **系统盘 `/` 在"释放实例"后清空**，模型和出图会全没 |
| 磁盘 | 系统盘 ≥ 30GB、数据盘 ≥ 30GB | 模型 5.4GB + ComfyUI + Python 依赖 |

⚠️ **计费**：AutoDL 按小时计费，**用完一定要关机**。"关机"仍收少量存储费，"释放"才停止计费但数据盘也可能没。
建议让用户自己在 AutoDL 控制台设**定时关机**。

---

## 3. 这套软件由什么组成（部署前必须知道的 4 件事）

| 组件 | 说明 |
|---|---|
| `app.py` | Flask 主程序，1700+ 行，**单文件**。端口 7861 |
| `templates/index.html` | 前端，**单文件**，含全部 CSS/JS（内联） |
| `config.json` | 配置。**含 DeepSeek API Key + 用户的提示词规则/预设** |
| `sessions.json` | 对话历史（运行时生成，云端会自动新建） |
| `images/` | 出图目录（运行时生成，`app.py` 第 25 行 `os.makedirs` 会自动建） |
| 依赖 | `flask`、`requests`、`websocket-client`（**只有这三个**） |

**ComfyUI 侧**：workflow 里用的 8 个节点全部是 **ComfyUI 内置节点**
（`UNETLoader` / `CLIPLoader` / `VAELoader` / `CLIPTextEncode` / `EmptyLatentImage` /
`KSampler` / `VAEDecode` / `SaveImage`）——
**不需要装任何自定义节点**，原版 ComfyUI 就够。

---

## 4. 步骤一：装 ComfyUI

### 4.1 开学术加速（下载提速，很重要）

```bash
source /etc/network_turbo
```

这是 AutoDL 提供的加速，**每次新开终端都要重新执行一次**。不生效的话 GitHub / HuggingFace 会非常慢或直接失败。

### 4.2 确认 torch 能用（**别跳过这步**）

AutoDL 的 GPU 镜像通常已在 conda `base` 环境预装好 torch，先用它，**不要重新下 torch**：

```bash
python -c "import torch; print('torch', torch.__version__, '| cuda', torch.version.cuda, '| 显卡可用:', torch.cuda.is_available())"
```

- ✅ 打印出 `cuda 12.x` 且 `显卡可用: True` → 继续，**记住这个 python 是哪个**（`which python`）
- ❌ 报错或 `False` → 停下，去 AutoDL 控制台换一个预装 PyTorch 的镜像重开实例

### 4.3 拉 ComfyUI

```bash
cd /root/autodl-tmp
git clone https://github.com/comfyanonymous/ComfyUI.git
cd ComfyUI
```

> 本机 Windows 上的 ComfyUI 是 commit `e7051b0`（2026-08-28），torch 2.9.1+cu128。
> 云端拉最新版也可以，但如果出问题，可以切到这个 commit 对齐。

### 4.4 建虚拟环境（**关键技巧**）

不要直接 `pip install -r requirements.txt` 装进 base，也不要新建一个干净的 venv
（那会白下 2.5GB torch）。用 `--system-site-packages` **继承**系统已有的 torch：

```bash
python -m venv --system-site-packages /root/autodl-tmp/comfy-venv
source /root/autodl-tmp/comfy-venv/bin/activate

# 验证 venv 里也能看到 torch（应该和 4.2 输出一致）
python -c "import torch; print(torch.__version__, torch.cuda.is_available())"
```

确认无误后再装 ComfyUI 自己的依赖：

```bash
pip install -r /root/autodl-tmp/ComfyUI/requirements.txt
```

---

## 5. 步骤二：下模型

### 5.1 需要哪三个文件（**精确清单，别下错**）

Anima 跟 z-image 是**两套不同的模型**，别混。

| 文件 | 大小 | 放到 `ComfyUI/models/` 下的 |
|---|---|---|
| `anima-base-v1.0.safetensors` | **3.9 GB** | `diffusion_models/` |
| `qwen_3_06b_base.safetensors` | **1.2 GB** | `text_encoders/` |
| `qwen_image_vae.safetensors` | **243 MB** | `vae/` |

**合计约 5.4 GB。**

> ⚠️ 用户本机 `text_encoders/` 里还有一个 `qwen_3_4b_fp8_mixed.safetensors`（5.3G）、
> `vae/` 里有个 `ae.safetensors`（320M）——**那两个是 z-image 用的，本应用不需要，别下。**

### 5.2 为什么用"按文件名搬运"而不是"按仓库路径下载"

官方仓库 `circlestone-labs/Anima` 的文件放在 `split_files/` 下的子目录里，
但**子目录结构可能变**。所以稳妥做法是：**整包下下来，再按文件名搬**（文件名是固定的）。

### 5.3 下载

```bash
source /etc/network_turbo          # 再执行一次，新终端必须重来
pip install -U huggingface_hub

cd /root/autodl-tmp
huggingface-cli download circlestone-labs/Anima \
    --include "split_files/*" \
    --local-dir /root/autodl-tmp/Anima-dl
```

如果 HuggingFace 连不上，改用国内镜像（任选其一）：

```bash
# 方案 A：HF 镜像站
export HF_ENDPOINT=https://hf-mirror.com
# 然后重跑上面的 huggingface-cli download

# 方案 B：ModelScope 国内源（社区量化版，速度最快）
# 搜 "Anima" 可找到 Abiray/Anima-base-v1.0-FP8 等镜像仓库
```

### 5.4 按文件名搬到正确位置（**并核对大小**）

```bash
cd /root/autodl-tmp
M=/root/autodl-tmp/ComfyUI/models

find Anima-dl -name "anima-base-v1.0.safetensors" -exec mv {} $M/diffusion_models/ \;
find Anima-dl -name "qwen_3_06b_base.safetensors"  -exec mv {} $M/text_encoders/ \;
find Anima-dl -name "qwen_image_vae.safetensors"   -exec mv {} $M/vae/ \;

# 核对：三个都该在，且大小对得上
ls -lh $M/diffusion_models/anima-base-v1.0.safetensors \
       $M/text_encoders/qwen_3_06b_base.safetensors \
       $M/vae/qwen_image_vae.safetensors
```

**必须核对大小**：`3.9G` / `1.2G` / `243M`。对不上就是下漏了或下坏了，别往下走
（否则会在出图时报找不到节点/加载失败，很难查）。

### 5.5 备选：从用户本机直接传

如果云端下载太慢，可以让用户从本机 `E:\ComfyUI\models\` 用 `scp` / AutoDL 的文件传输上传。
⚠️ 注意上传带宽通常比下载慢很多，5.4GB 可能要很久——**优先用云端直接下载**。

---

## 6. 步骤三：装 Anima Studio

### 6.1 拉代码

代码在用户的 **GitHub 私有仓库**（用户已设为 Private）。

```bash
source /etc/network_turbo
cd /root/autodl-tmp

# 私有仓库需要凭证，两种方式任选：
#  ① 用带 token 的 URL（让用户提供一个有 repo 读权限的 GitHub Personal Access Token）
#     git clone https://<TOKEN>@github.com/<用户名>/<仓库名>.git anima-studio
#  ② 或者在 AutoDL 上配好 SSH key，加进 GitHub 的 Deploy keys
#     git clone git@github.com:<用户名>/<仓库名>.git anima-studio

cd anima-studio
```

### 6.2 装依赖

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

这个 venv **不用** `--system-site-packages`——它只需要 flask / requests / websocket-client，
很小，而且**不需要 torch**。

> ⚠️ 仓库里**没有** `.venv/`（已 gitignore），云端必须自己建。
> 也**没有** `images/` 和 `sessions.json`，运行时自动生成，属正常。

### 6.3 建 config.json

仓库里只有 `config.example.json`（**空壳模板，密钥是占位符**）。
真实 `config.json` 被 gitignore 挡在外面了，**必须手动建**：

```bash
cp config.example.json config.json
nano config.json          # 或 vim
```

**只改一处**：

```json
{
  "api_key": "sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",   ← 换成用户的真实 DeepSeek Key
  ...
}
```

`rules` 和 `presets`（4 个预设）**照搬，不要动**——
那是用户的提示词工程成果，是本应用的核心价值，改了行为就变了。

### 6.4 更好的做法：密钥走环境变量（**推荐**）

`app.py` 第 259 行是这么读密钥的：

```python
_api_key = cfg.get("api_key", "") or os.environ.get("DEEPSEEK_API_KEY", "")
```

也就是说 **`config.json` 里的 `api_key` 留空、改用环境变量也完全可以**：

```bash
export DEEPSEEK_API_KEY=sk-用户的真实key
python app.py
```

**为什么推荐这个**：密钥**不落盘**，`config.json` 里干干净净。
这样万一哪天这个文件被误传到别处，也不会连带泄露密钥。
配合 §7.2 的 `start.sh` 写法，把 `export DEEPSEEK_API_KEY=...` 也写进去即可
（⚠️ 那个 `start.sh` 要放进 `.gitignore`，别把带密钥的脚本提交上去）。

> `search.serper_api_key` 留空即可（用户没配，搜索走百度百科/萌娘百科/维基，不用它）。

---

## 7. 步骤四：启动

### 7.1 先起 ComfyUI

```bash
cd /root/autodl-tmp/ComfyUI
source /root/autodl-tmp/comfy-venv/bin/activate
python main.py --listen 127.0.0.1 --port 8188
```

⚠️ **和本机 Windows 启动脚本的差异（重要）**：
本机是 `python main.py --listen 127.0.0.1 --port 8188 --lowvram`。
**云端要去掉 `--lowvram`**——那个是为了在 8GB 显存上硬扛才加的，
云上 16/24GB 再开着反而拖慢速度。

等它打印出 `To see the GUI go to: http://127.0.0.1:8188` 就算起来了。

### 7.2 再起 Anima Studio（**新开一个终端**）

```bash
source /etc/network_turbo
cd /root/autodl-tmp/anima-studio
source .venv/bin/activate

# ★ 唯一必须设的环境变量：告诉后端去哪儿找出图
export COMFY_OUTPUT=/root/autodl-tmp/ComfyUI/output

python app.py
```

看到 `Anima Studio: http://127.0.0.1:7861` 就成功了。

> **`COMFY_OUTPUT` 是这次部署最关键的一行。** 它的代码默认值是 Windows 路径
> `E:\ComfyUI\output`（`app.py` 第 32 行 `os.environ.get("COMFY_OUTPUT", r"E:\ComfyUI\output")`）。
> **不设这个环境变量，出图后拿不到文件**（`shutil.copy` 会失败），
> 表现是"图在 ComfyUI 里生成了，但网页上永远是空的/报错"。
>
> 建议把这两行写进一个 `start.sh`，省得每次记：
> ```bash
> #!/bin/bash
> source /etc/network_turbo
> cd /root/autodl-tmp/anima-studio
> source .venv/bin/activate
> export COMFY_OUTPUT=/root/autodl-tmp/ComfyUI/output
> python app.py
> ```

**其他可选环境变量**（都有合理默认值，一般不用设）：

| 变量 | 默认 | 什么时候要改 |
|---|---|---|
| `COMFY_URL` | `http://127.0.0.1:8188` | 同机部署**不用改** ✅ |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | 不用改 |
| `DEEPSEEK_MODEL` | `deepseek-flash` | 不用改（这是支持看图的 V4.1） |

---

## 8. 怎么访问网页

### ✅ 推荐：SSH 隧道（**不用改任何代码**）

在**用户自己的电脑**上开一个终端：

```bash
ssh -L 7861:127.0.0.1:7861 -p <AutoDL的SSH端口> root@<AutoDL的地址>
```

然后浏览器打开 **http://127.0.0.1:7861** 就能用了。

好处：Flask 保持绑在 `127.0.0.1`，公网上看不到，最安全。
（AutoDL 控制台能看到 SSH 端口和地址。）

如果还想看 ComfyUI 的原生界面，多加一条转发：

```bash
ssh -L 7861:127.0.0.1:7861 -L 8188:127.0.0.1:8188 -p <端口> root@<地址>
```

### ⚠️ 不推荐：暴露到公网

要这么做的话，先读这段：

1. **代码要改**：`app.py` 最后一行现在是
   `app.run(host="127.0.0.1", port=7861, ...)`，得改成 `host="0.0.0.0"`。
2. **⚠️ 这个应用没有任何登录/鉴权。** 一旦挂到公网 URL 上，
   **任何知道这个网址的人都能**：
   - 拿用户的 DeepSeek Key 随便调 API（**花用户的钱**）
   - 翻用户的全部出图作品和对话记录
   - 改用户的提示词规则

所以：**要走公网就必须先加一层认证**（至少给 AutoDL 的自定义服务套个 Basic Auth），
或者干脆用 SSH 隧道。**默认建议用户用隧道。**

---

## 9. ⚠️ Windows → Linux 差异清单

从本机搬过来时，下面这些地方和本机不一样。已经逐条在源码里查证过：

| # | 项 | 本机（Windows） | 云端（Linux） | 处理 |
|---|---|---|---|---|
| 1 | `COMFY_OUTPUT` | 代码里硬编码 `E:\ComfyUI\output` | 无 `E:\` | **必须设环境变量**（§7.2） |
| 2 | `app.run` 绑定地址 | `127.0.0.1` | 同左 | 走隧道则**不用改**；公网才改（§8） |
| 3 | ComfyUI 启动参数 | 带 `--lowvram` | 要去掉 | §7.1 |
| 4 | Python 路径 | `.venv\Scripts\python.exe` | `.venv/bin/python` | 反斜杠 → 正斜杠 |
| 5 | 启动脚本 | 桌面 `启动AnimaStudio.bat` | `.bat` 是 Windows 专用 | 写个 `.sh`（§7.2 末尾） |
| 6 | 依赖安装 | 清华镜像 | AutoDL 默认源即可 | — |
| 7 | 中文文件名 | `维护交接-给下一个AI.md` 有中文 | Linux 支持良好 | 注意别用 GBK 编码读写 |
| 8 | 终端编码 | Windows GBK，**shell 传中文参数会乱码** | Linux 默认 UTF-8 | 云端反而没这个坑 |

**好消息（这几处本来就是可移植的，不用管）**：

- `BASE_DIR` 由 `os.path.dirname(os.path.abspath(__file__))` 推导，
  `images/`、`config.json`、`sessions.json` **全部跟着 `app.py` 走**，搬到哪都行 ✅
- `COMFY_URL` / `DEEPSEEK_BASE_URL` / `DEEPSEEK_MODEL` 都已经是环境变量，默认值在云端也对 ✅
- workflow 全用内置节点，**不用装自定义节点** ✅

---

## 10. 验收清单（按顺序做完再用）

部署完**逐条验证**，不要只看"没报错"就宣布成功：

- [ ] `python -c "import torch; print(torch.cuda.is_available())"` → `True`
- [ ] 三个模型文件都在，大小是 `3.9G` / `1.2G` / `243M`
- [ ] ComfyUI 打得开 `http://127.0.0.1:8188`
- [ ] ComfyUI 的**模型下拉列表里能看到 `anima-base-v1.0.safetensors`**
      （看不到 = 放错目录了）
- [ ] Anima Studio 打得开 `http://127.0.0.1:7861`，页面正常渲染
      （不是白屏/不是一堆 JS 报错）
- [ ] **端到端出图**：在网页里让 AI 写个提示词 → 点生成 → **图出现在聊天区**
      （这一步才真正验证了 `COMFY_OUTPUT` 设对了）
- [ ] 生成时**进度条会动**（0→30）——验证 `websocket-client` 装上了
- [ ] 图库（`#galleryStrip` 缩略图带）里有刚才出的图
- [ ] **拖一张图到聊天区 → 写关注点 → 发送**，AI 能说出图里的内容
      （验证视觉回传链路，这是这个应用的核心功能之一）

---

## 11. 排错

| 现象 | 原因 | 处理 |
|---|---|---|
| 出图后网页上什么都没有 / 报文件找不到 | **`COMFY_OUTPUT` 没设** | §7.2，这是最常见的坑 |
| 启动就崩，`ModuleNotFoundError: No module named 'websocket'` | 依赖没装全 | `pip install -r requirements.txt`（含 `websocket-client`） |
| 进度条不动 | 同上，或缺 `websocket-client` | 同上 |
| ComfyUI 里看不到 Anima 模型 | 文件放错子目录 | §5.4，按文件名核对 |
| 出图报 `er_sde` 找不到 / KSampler 报错 | ComfyUI 版本太老 | `git pull` 更新 ComfyUI |
| 模型加载失败、显存爆 | 显存不够 | 加回 `--lowvram`；或降分辨率 |
| 改 `index.html` 不生效 | Flask 模板缓存 | 代码里已设 `TEMPLATES_AUTO_RELOAD=True`，一般不用管；不行就重启 |
| 对话一直不回复 / 卡住 | 联网搜索超时（有 15s 预算兜底） | 看 `维护交接-给下一个AI.md` 的第 10 节 |
| 下载模型极慢 | 忘了学术加速 | `source /etc/network_turbo`，**每个新终端都要** |

---

## 12. 交接给这个 AI 时，用户需要准备的东西

部署前跟用户确认这几样（缺了会卡住）：

1. **GitHub 私有仓库地址** + 一个有读权限的 token（或配好 SSH key）
2. **DeepSeek API Key**（填进 `config.json`）
3. **AutoDL 实例信息**：SSH 地址、SSH 端口
4. 确认用户接受：**出图和对话历史都存在云上数据盘**，
   实例释放就没了 → 建议定期把 `images/` 和 `sessions.json` 同步回本机

---

## 13. 部署完之后

告诉用户：

1. **怎么开**：SSH 隧道命令（§8）+ 浏览器地址
2. **怎么关**：AutoDL 控制台关机，**别忘**（按小时计费）
3. **数据在哪**：`/root/autodl-tmp/anima-studio/{images,sessions.json}`
4. **本机那份没被破坏**：`E:\anima-studio` 原封不动，随时可以切回来本地跑

> ⚠️ **改代码请改一处、两处同步**：GitHub 仓库和本机 `E:\anima-studio` 是两份。
> 如果在云端改了代码，记得让用户把改动同步回本机（或者以 GitHub 为准，本机 `git pull`）。
> `维护交接-给下一个AI.md` 也要跟着更新——**它是本机维护的入口文档**。

---

## 附：相关文档

- `维护交接-给下一个AI.md` —— **本机维护入口**。软件怎么工作、所有已知的坑、函数表、API 路由表。改代码前必读。
- `README`（仓库根） —— 如果存在的话
- Anima 官方模型页：<https://huggingface.co/circlestone-labs/Anima>
- ComfyUI 官方仓库：<https://github.com/comfyanonymous/ComfyUI>

**Anima 模型要点**（来自官方说明，供参考）：
- 2B 参数 DiT，CircleStone Labs × Comfy Org 合作，动漫特化，**不适合写实**
- 推荐参数：**30–50 步、CFG 4–5**（本应用默认 30/CFG4 ✅）
- 推荐采样器：`er_sde`、`euler_a`、`dpmpp_2m_sde_gpu`（本应用用 `er_sde` ✅）
- 正向前缀：`masterpiece, best quality, score_7, safe, `；画师标签要加 `@` 前缀
- **许可证：CircleStone Labs Non-Commercial License（非商用）**——注意用途
