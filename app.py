# -*- coding: utf-8 -*-
"""Anima Studio —— 对话式动漫文生图助手（调试工作台版）
聊天用 DeepSeek（流式），生图用本机 ComfyUI（Anima 2B DiT，动漫模型）。
会话采用「会话 → 线性消息」结构（暂无分支功能）。
"""
import base64
import collections
import json
import os
import random
import re
import shutil
import threading
import time
import uuid

import requests
import websocket
from flask import Flask, Response, jsonify, render_template, request, send_from_directory

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
IMAGE_DIR = os.path.join(BASE_DIR, "images")
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
SESSIONS_FILE = os.path.join(BASE_DIR, "sessions.json")
os.makedirs(IMAGE_DIR, exist_ok=True)

app = Flask(__name__)
app.config["TEMPLATES_AUTO_RELOAD"] = True

# ---------- 配置 ----------
COMFY_URL = os.environ.get("COMFY_URL", "http://127.0.0.1:8188")
COMFY_OUTPUT = os.environ.get("COMFY_OUTPUT", r"E:\ComfyUI\output")
DEEPSEEK_BASE = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
# DeepSeek V4.1 Flash：支持图片输入（视觉），也是提示词助手用的对话模型。
# 旧值 "deepseek-chat" 会被服务端静默别名到这个模型，这里写明以免看不出它支持看图。
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-flash")

# Anima 官方推荐负向提示词
DEFAULT_NEGATIVE = (
    "worst quality, low quality, score_1, score_2, score_3, blurry, jpeg artifacts, chromatic aberration"
)

# ---------- 状态 ----------
SESSIONS = {}          # sid -> {"title", "updated", "messages": [...], "prompt", "params", "last_image", "negative"}
LOCK = threading.Lock()
_api_key = ""
_rules = ""
_presets = {}
_search_providers = ["baike", "moegirl", "wikipedia", "serper"]
_search_auto = True
_serper_key = ""
CURRENT_PID = None
LOGS = collections.deque(maxlen=200)

# 生图进度追踪
_generating = False
GEN_META = {"gen_index": 1, "gen_total": 1}
PROGRESS = {}          # pid -> {"value", "max", "status"}

DEFAULT_RULES = (
    "你是专业的二次元/动漫风格文生图助手，为「Anima」模型（CircleStone Labs 2B DiT）编写英文提示词。\n"
    "Anima 训练于 Danbooru 风格标签+自然语言，且【支持负向提示词】。\n"
    "工作方式：\n"
    "1. 先向用户询问必要细节（角色外貌、姿态、表情、背景、画风等），一次只问 1-2 个问题。\n"
    "2. 根据回答逐步完善一段【英文】提示词（含正向与负向两部分）。\n"
    "3. 每次提示词更新时，用 <prompt>...</prompt> 标签把最新英文【正向】提示词包起来，并用 <negative>...</negative> 标签把英文【负向】提示词包起来（两个标签紧跟在一起，标签内只用英文）。系统会自动提取并分别填入「当前提示词」和「负向提示词」框。\n"
    "4. 用户说\"生成/画/出图\"时，直接给出最终 <prompt> 和 <negative>，不要再提问。\n"
    "5. 用户看图后说\"改 XX\"，基于现有提示词定向修改，再给出新的 <prompt> 和 <negative>。\n"
    "6. 正向前缀固定写 masterpiece, best quality, score_7, safe, ；标签小写、逗号+空格分隔、用空格代替下划线。\n"
    "7. 标签顺序：[质量/元/年份/安全标签] [1girl/1boy] [角色] [系列] [画师(加@前缀)] [通用标签]；镜头/构图放最前。\n"
    "8. 负向词以官方推荐为基准：worst quality, low quality, score_1, score_2, score_3, blurry, jpeg artifacts, chromatic aberration，再按画面内容适度增减（去掉无关项、补充画面易出问题的词）。\n"
    "9. 除 <prompt> 和 <negative> 内英文外，其余交流一律用中文。"
)

RULES_TEMPLATES = {
    "Anima 默认": DEFAULT_RULES,
    "唯美厚涂": (
        "你是二次元厚涂风格文生图助手。为 Anima 编写英文提示词：\n"
        "1. 先问清角色外貌、姿态、表情、背景，一次 1-2 问。\n"
        "2. 逐步完善英文提示词（含正负向），用 <prompt>...</prompt> 包正向、<negative>...</negative> 包负向（标签内只用英文）。\n"
        "3. 正向前缀 masterpiece, best quality, score_8, safe, ；画风词用 painterly, soft shading, thick paint strokes, atmospheric, detailed eyes。\n"
        "4. 构图词放前面（close-up, upper body），加低光/氛围光描述（soft rim light, warm lighting）。\n"
        "5. 用户说\"生成\"时直接给最终 <prompt> 和 <negative>。\n"
        "6. 负向以官方推荐为基准并适度增减。其余交流用中文。"
    ),
    "场景插画": (
        "你是动漫场景插画文生图助手。为 Anima 编写英文提示词：\n"
        "1. 先问清场景类型、时间光线、主体角色、氛围，一次 1-2 问。\n"
        "2. 逐步完善英文提示词（含正负向），用 <prompt>...</prompt> 包正向、<negative>...</negative> 包负向（标签内只用英文）。\n"
        "3. 正向前缀 masterpiece, best quality, safe, ；强调 wide shot, establishing shot, detailed background, scenic。\n"
        "4. 时间光线写具体：daytime/evening/night, golden hour, dramatic lighting。\n"
        "5. 用户说\"生成\"时直接给最终 <prompt> 和 <negative>。\n"
        "6. 负向以官方推荐为基准并适度增减。其余交流用中文。"
    ),
}


def log(event, **kw):
    entry = {"ts": time.time(), "event": event}
    entry.update(kw)
    LOGS.append(entry)


# ---------- 会话/分支（版本历史） ----------
def _new_branch(messages=None, prompt="", negative="", forked_from=None):
    return {
        "id": uuid.uuid4().hex[:12],
        "messages": messages if messages is not None else [],
        "prompt": prompt,
        "negative": negative or DEFAULT_NEGATIVE,
        "last_image": "",
        "params": {"seed": None, "steps": 30, "width": 1024, "height": 1024, "cfg": 4.0},
        "forked_from": forked_from,
        "created": time.time(),
    }


def _branch(sid):
    """返回 (session, 当前活跃分支)，不存在则 (None, None)。"""
    s = SESSIONS.get(sid)
    if not s:
        return None, None
    return s, s["branches"].get(s["active"])


def _fork_type(ff, branches, branch):
    """判断一条 forked_from 是「重写(rewrite)」还是「另起(fork)」。"""
    return ff.get("type") or "fork"


def _lineage_root(branches, branch, index):
    """沿「重写(rewrite)」链向上回溯，找到某条 user 消息所属版本组的根分支 id。"""
    cur = branch
    seen = set()
    while True:
        ff = cur.get("forked_from") or {}
        if _fork_type(ff, branches, cur) != "rewrite" or ff.get("index") != index:
            break
        pid = ff.get("branch")
        if not pid or pid not in branches or pid in seen:
            break
        seen.add(pid)
        cur = branches[pid]
    return cur["id"]


def _version_groups(s, active_branch):
    """找出当前分支里被「重写」过的 user 消息，返回 {index: {"branches": [按时间排序的兄弟分支id], "active": 当前位置}}。"""
    branches = s["branches"]
    groups = {}
    for i, m in enumerate(active_branch.get("messages", [])):
        if m.get("role") != "user":
            continue
        root = _lineage_root(branches, active_branch, i)
        siblings = []
        for bid, b in branches.items():
            bmsgs = b.get("messages", [])
            if len(bmsgs) <= i or bmsgs[i].get("role") != "user":
                continue
            if _lineage_root(branches, b, i) == root:
                siblings.append(bid)
        if len(siblings) <= 1:
            continue
        siblings.sort(key=lambda bid: branches[bid].get("created", 0) or 0)
        if active_branch["id"] in siblings:
            groups[str(i)] = {"branches": siblings, "active": siblings.index(active_branch["id"])}
    return groups


def _regen_root(branches, branch, index):
    """沿「重新生成(regenerate)」链向上回溯，找到某条 assistant 消息所属版本组的根分支 id。"""
    cur = branch
    seen = set()
    while True:
        ff = cur.get("forked_from") or {}
        if ff.get("type") != "regenerate" or ff.get("index") != index:
            break
        pid = ff.get("branch")
        if not pid or pid not in branches or pid in seen:
            break
        seen.add(pid)
        cur = branches[pid]
    return cur["id"]


def _assistant_version_groups(s, active_branch):
    """找出当前分支里被「重新生成」过的 assistant 消息，返回 {index: {"branches": [按时间排序的兄弟分支id], "active": 当前位置}}。"""
    branches = s["branches"]
    groups = {}
    for i, m in enumerate(active_branch.get("messages", [])):
        if m.get("role") != "assistant":
            continue
        root = _regen_root(branches, active_branch, i)
        siblings = []
        for bid, b in branches.items():
            bmsgs = b.get("messages", [])
            if len(bmsgs) <= i or bmsgs[i].get("role") != "assistant":
                continue
            if _regen_root(branches, b, i) == root:
                siblings.append(bid)
        if len(siblings) <= 1:
            continue
        siblings.sort(key=lambda bid: branches[bid].get("created", 0) or 0)
        if active_branch["id"] in siblings:
            groups[str(i)] = {"branches": siblings, "active": siblings.index(active_branch["id"])}
    return groups


def load_sessions():
    global SESSIONS
    if not os.path.exists(SESSIONS_FILE):
        return
    try:
        with open(SESSIONS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return
    SESSIONS = {}
    for sid, s in data.items():
        if "branches" in s:
            # 新版分支格式
            for bid, b in s["branches"].items():
                b.setdefault("negative", DEFAULT_NEGATIVE)
            SESSIONS[sid] = s
        else:
            # 旧线性格式 → 迁移为单个分支（原对话完整保留）
            branch = _new_branch(
                messages=s.get("messages", []),
                prompt=s.get("prompt", ""),
                negative=s.get("negative", DEFAULT_NEGATIVE),
            )
            branch["last_image"] = s.get("last_image", "")
            branch["params"] = dict(s.get("params", {"seed": None, "steps": 30, "width": 1024, "height": 1024, "cfg": 4.0}))
            SESSIONS[sid] = {
                "title": s.get("title", "新对话"),
                "updated": s.get("updated", 0),
                "active": branch["id"],
                "branches": {branch["id"]: branch},
            }


def save_sessions():
    try:
        with open(SESSIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(SESSIONS, f, ensure_ascii=False)
    except Exception:
        pass


def load_config():
    global _api_key, _rules, _presets, _search_providers, _search_auto, _serper_key
    cfg = {}
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            cfg = {}
    _api_key = cfg.get("api_key", "") or os.environ.get("DEEPSEEK_API_KEY", "")
    _rules = cfg.get("rules", "") or DEFAULT_RULES
    _presets = dict(cfg.get("presets", RULES_TEMPLATES))
    sc = cfg.get("search", {}) or {}
    _search_providers = sc.get("providers", ["baike", "moegirl", "wikipedia", "serper"])
    _search_auto = bool(sc.get("auto", False))
    _serper_key = sc.get("serper_api_key", "") or ""


def save_config():
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump({
                "api_key": _api_key,
                "rules": _rules,
                "presets": _presets,
                "search": {"providers": _search_providers, "auto": _search_auto, "serper_api_key": _serper_key},
            }, f, ensure_ascii=False)
    except Exception:
        pass


def extract_prompt(text):
    m = re.findall(r"<\s*prompt\s*>\s*(.*?)\s*</\s*prompt\s*>", text, flags=re.DOTALL)
    if m:
        return m[-1].strip()
    # 兜底：AI 偶尔把闭合标签写错/漏写，从 <prompt> 截到下一个标签或空行
    i = re.search(r"<\s*prompt\s*>", text)
    if i:
        rest = text[i.end():]
        stop = re.search(r"</\s*(?:prompt|negative)\s*>|\n\s*\n", rest)
        if stop:
            rest = rest[:stop.start()]
        return rest.strip()
    return ""


def extract_negative(text):
    m = re.findall(r"<\s*negative\s*>\s*(.*?)\s*</\s*negative\s*>", text, flags=re.DOTALL)
    if m:
        return m[-1].strip()
    # 兜底：AI 偶尔把闭合标签写错（如用 </prompt>）或漏写，从 <negative> 截到下一个标签或空行
    i = re.search(r"<\s*negative\s*>", text)
    if i:
        rest = text[i.end():]
        stop = re.search(r"</\s*(?:prompt|negative)\s*>|\n\s*\n", rest)
        if stop:
            rest = rest[:stop.start()]
        return rest.strip()
    return ""


# ---------- 联网搜索角色外貌 ----------
SEARCH_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AnimaStudio/1.0"}


def _http_json(url, params, timeout=15):
    try:
        r = requests.get(url, params=params, timeout=timeout, headers=SEARCH_UA)
        if r.status_code != 200:
            return None
        return r.json()
    except requests.RequestException:
        return None


VISUAL_WORDS = ("发色", "瞳色", "发型", "头发", "长发", "短发", "金发", "黑发", "白发",
                "眼睛", "眼瞳", "身高", "体型", "身材", "服装", "衣服", "裙子", "制服",
                "外套", "鞋", "袜", "帽子", "缎带", "丝带", "头饰", "马尾", "辫子", "刘海",
                "黑色", "白色", "金色", "蓝色", "红色", "紫色", "粉色", "绿色", "银色", "棕色", "灰色")

# 元信息讨论词：这类内容（未公开/画师/同人/外貌可变等）不是外貌事实，会误导 AI 以为外貌未知
META_WORDS = ("未公开", "未被", "画师", "同人", "调侃", "玩梗", "昵称", "称呼", "读音",
              "声优", "粉丝", "爱好者", "参考隔壁", "二创", "二次创作", "捏他", "neta",
              "多种变化", "难以辨识", "官方设定的基础上", "为了配合")


# 与外貌无关的章节：命中即整节跳过（游戏数据/台词/剧情/参考资料等）
DROP_SECTIONS = ("游戏", "台词", "参考", "外部", "剧情", "经历", "搭配", "杂谈", "MomoTalk",
                 "记忆", "数据", "歌曲", "画廊", "作品", "活动", "音乐", "视频", "同人", "周边", "花絮")


def _clean_appearance_text(text, max_len=1200):
    """清洗搜索到的外貌文本：保留外貌/简介/设定类章节正文，跳过无关章节，去章节标记/元信息行/空行。"""
    if not text:
        return ""
    skip = False
    lines = []
    for ln in text.splitlines():
        s = ln.strip()
        if not s:
            continue
        hm = re.match(r"^={2,}\s*(.*?)\s*={2,}$", s)
        if hm:
            skip = any(d in hm.group(1) for d in DROP_SECTIONS)
            continue
        if skip:
            continue
        if any(w in s for w in META_WORDS):
            continue
        lines.append(s)
    return "\n".join(lines).strip()[:max_len]


def _extract_section_text(text, start, max_len=1500):
    """截取从 start（章节标题行）到下一个章节标题之间的正文并清洗。"""
    nl = text.find("\n", start)
    after = text[nl + 1:] if nl >= 0 else ""
    m = re.search(r"^={2,}\s*.*?={2,}\s*$", after, flags=re.MULTILINE)
    if m:
        after = after[:m.start()]
    return _clean_appearance_text(after, max_len)


def _appearance_chunk(text, keywords, max_len=1500):
    if not text:
        return None
    # 1) 优先：显式外貌/人物设定章节（限短标题，避免误匹配「声库软件版本及形象设定」这类长复合标题）
    for kw in ("外貌", "形象", "外观", "造型", "人物设定", "角色设定", "角色介绍", "人物介绍"):
        m = re.search(r"^==+\s*[^=]{0,4}" + re.escape(kw) + r"[^=]{0,4}==+\s*$", text, flags=re.MULTILINE)
        if m:
            section = _extract_section_text(text, m.start(), max_len)
            if section:
                return section
    # 2) 开头简介段（第一个标题之前，仅当足够短才用——长的多是历史/版权段落，会污染）
    fm = re.search(r"^={2,}\s*.*?={2,}\s*$", text, flags=re.MULTILINE)
    if fm and 0 < fm.start() <= 200:
        intro = _clean_appearance_text(text[:fm.start()], max_len)
        if intro and sum(1 for w in VISUAL_WORDS if w in intro) >= 1:
            return intro
    # 3) 兜底：收集全文含视觉词的非元信息行（外貌散落时也能抓）
    collected = []
    for ln in text.splitlines():
        s = ln.strip()
        if not s or re.match(r"^={2,}\s*.*?={2,}\s*$", s):
            continue
        if s.startswith("注") or any(w in s for w in META_WORDS):
            continue
        if sum(1 for w in VISUAL_WORDS if w in s) >= 1:
            collected.append(s)
    if len(collected) >= 2:
        return "\n".join(collected).strip()[:max_len]
    # 4) 泛化词锚点
    for kw in keywords:
        i = text.find(kw)
        if i >= 0:
            return _clean_appearance_text(text[max(0, i - 80): i + max_len], max_len)
    return _clean_appearance_text(text[:max_len], max_len)


def _wiki_extract_by_title(api_url, title):
    page = _http_json(api_url, {"action": "query", "prop": "extracts", "explaintext": 1,
                                "titles": title, "redirects": 1, "format": "json"})
    pages = (page or {}).get("query", {}).get("pages", {})
    for _pid, p in pages.items():
        if p.get("extract"):
            return p.get("extract")
    return None


def _wiki_opensearch(api_url, name, limit=3):
    j = _http_json(api_url, {"action": "opensearch", "search": name, "limit": limit, "format": "json"})
    if isinstance(j, list) and len(j) >= 2 and isinstance(j[1], list):
        return j[1]
    return []


def _wiki_lookup(api_url, name, keywords):
    text = _wiki_extract_by_title(api_url, name)
    if text:
        chunk = _appearance_chunk(text, keywords)
        if chunk:
            return {"title": name, "text": chunk}
    for title in _wiki_opensearch(api_url, name)[:2]:
        text = _wiki_extract_by_title(api_url, title)
        if text:
            chunk = _appearance_chunk(text, keywords)
            if chunk:
                return {"title": title, "text": chunk}
    return None


MOEGIRL_INFOBOX_KEYS = ("发色", "瞳色", "身高", "种族", "体重", "三围", "年龄", "服装", "印象色", "外观", "纹章", "武器")
MOEGIRL_PAGE_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"}


_moegirl_info_cache = {}


def _moegirl_infobox(title):
    """抓萌百渲染后的页面 HTML，解析信息框外貌字段（发色/瞳色/身高等）。
    extract API 拿不到模板里的信息框，只能抓页面 HTML；抓取/解析失败一律返回 None。
    结果按角色名缓存，避免重复下载整页 HTML。"""
    if title in _moegirl_info_cache:
        return _moegirl_info_cache[title]
    try:
        url = "https://zh.moegirl.org.cn/" + requests.utils.quote(title)
        r = requests.get(url, headers=MOEGIRL_PAGE_UA, timeout=12)
        if r.status_code != 200:
            return None
        r.encoding = "utf-8"
        html = r.text
    except requests.RequestException:
        return None
    fields = {}
    # 信息框行结构：<div style="display:flex; margin:3px 0;">...<span>键</span></div><div>值</div></div>
    pat = re.compile(r'<div style="display:\s*flex; margin: 3px 0;">.*?<span>([^<]{1,12})</span></div><div[^>]*>(.*?)</div></div>', re.DOTALL)
    for m in pat.finditer(html):
        key = m.group(1).strip()
        if key not in MOEGIRL_INFOBOX_KEYS:
            continue
        raw = m.group(2)
        # 优先取 <a title="...">（规范分类名），否则纯文本
        tm = re.search(r'<a[^>]*title="([^"]+)"', raw)
        val = tm.group(1).strip() if tm else re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", raw)).strip()
        if val:
            fields[key] = val
    if not fields:
        return None  # 解析失败不缓存，便于下次重试
    if len(_moegirl_info_cache) > 100:
        _moegirl_info_cache.clear()
    _moegirl_info_cache[title] = fields
    return fields


def search_moegirl(name):
    r = _wiki_lookup("https://zh.moegirl.org.cn/api.php", name,
                     ["外貌", "形象", "外观", "发色", "瞳色", "服饰", "服装", "装扮", "造型"])
    if r:
        r["source"] = "moegirl"
        t = r.get("text", "")
        # extract 缺核心外貌字段（发色 或 瞳色）时补抓信息框——模板里的结构化外貌 extract 拿不到
        if not (("发色" in t or "头发" in t) and ("瞳色" in t or "眼睛" in t)):
            info = _moegirl_infobox(name)
            if info:
                extra = "，".join(f"{k}：{v}" for k, v in info.items())
                r["text"] = "（萌百信息框外貌）" + extra + "\n" + t
                r["infobox"] = info
        return r
    # extract 没搜到页面 → 仍试一次信息框
    info = _moegirl_infobox(name)
    if info:
        return {"title": name, "text": "（萌百信息框外貌）" + "，".join(f"{k}：{v}" for k, v in info.items()),
                "source": "moegirl"}
    return None


def search_wikipedia(name):
    r = _wiki_lookup("https://zh.wikipedia.org/w/api.php", name,
                     ["外貌", "形象", "外观", "发色", "瞳色", "服饰", "服装"])
    if r:
        r["source"] = "zh.wikipedia"
        return r
    r = _wiki_lookup("https://en.wikipedia.org/w/api.php", name,
                     ["Appearance", "appearance", "physical appearance", "outfit", "hair", "eyes"])
    if r:
        r["source"] = "en.wikipedia"
    return r


def search_serper(name):
    if not _serper_key:
        return None
    q = f"{name} 角色 外貌 发色 瞳色 服装 设定"
    try:
        r = requests.post("https://google.serper.dev/search",
                          json={"q": q, "num": 5},
                          headers={"X-API-KEY": _serper_key, "Content-Type": "application/json"},
                          timeout=15)
        if r.status_code != 200:
            return None
        organic = (r.json() or {}).get("organic", [])
        snippets = [f"{o.get('title', '')}: {o.get('snippet', '')}" for o in organic[:5] if o.get("snippet")]
        if snippets:
            return {"source": "serper", "title": name, "text": "\n".join(snippets)[:1500]}
    except requests.RequestException:
        return None
    return None


# ---------- 百度百科（wapbaike）----------
# ⚠️ 桌面版 baike.baidu.com 会被「百度安全验证」拦成 403；官方模块接口 404。
# 只有移动版 wapbaike 能抓，且正文全在页面里的 __NEXT_DATA__ JSON 里，不用解析 HTML。
_baike_result_cache = {}

# 各词条的章节名不统一（衣着外貌 / 相貌衣着 / 角色背景…），按关键词分组匹配，标签用原章节名
BAIKE_IDENTITY_KEYS = ("背景", "身份", "简介", "设定")
BAIKE_PERSONALITY_KEYS = ("性格",)
BAIKE_APPEARANCE_KEYS = ("外貌", "相貌", "衣着", "外观", "造型", "服饰", "服装", "装扮")

# 信息框只留与角色形象有关的字段（攻击属性/战场位置这类游戏数值对出图无用）
BAIKE_INFOBOX_TITLES = ("身高", "生日", "年龄", "性别", "外文名", "别名", "登场作品", "配音")

BAIKE_SECTION_CAPS = {"identity": 500, "personality": 800, "appearance": 700}
BAIKE_TOTAL_CAP = 2200


# 同一作品的不同译名/写法：既用于「多义词义项消歧」，也用于「词条相关性校验」
FRANCHISE_ALIASES = {
    "蔚蓝档案": ("蔚蓝档案", "碧蓝档案", "blue archive", "ブルーアーカイブ"),
    "碧蓝航线": ("碧蓝航线", "azur lane"),
    "原神": ("原神", "genshin"),
    "崩坏星穹铁道": ("崩坏星穹铁道", "崩坏：星穹铁道", "honkai star rail"),
    "崩坏": ("崩坏", "honkai"),
    "明日方舟": ("明日方舟", "arknights"),
    "少女前线": ("少女前线", "girls' frontline", "girls frontline"),
    "公主连结": ("公主连结", "公主连接", "priconne"),
    "赛马娘": ("赛马娘", "umamusume"),
    "偶像大师": ("偶像大师", "idolmaster"),
    "love live": ("love live", "lovelive"),
    "bang dream": ("bang dream", "bangdream", "バンドリ"),
    "东方project": ("东方project", "touhou"),
    "fgo": ("fgo", "fate grand order", "fate/grand order"),
}

# 按别名长度倒序匹配，避免「崩坏」抢在「崩坏星穹铁道」前面命中
_FRANCHISE_MATCH_ORDER = sorted(
    ((a, canon) for canon, group in FRANCHISE_ALIASES.items() for a in group),
    key=lambda p: -len(p[0]),
)


def _franchise_hint(name):
    """从用户原始查询里找出作品名（如「蔚蓝档案橘光」→「蔚蓝档案」）。
    用于百科多义词消歧和相关性校验；找不到返回空串。"""
    low = (name or "").lower()
    for alias, canon in _FRANCHISE_MATCH_ORDER:
        if alias.lower() in low:
            return canon
    return ""


def _baike_fetch(path):
    """抓百度百科移动版 /item/<path>，从 __NEXT_DATA__ 取出 pageData。
    取不到（词条不存在会 404 到 error.html）一律返回 None。"""
    try:
        url = "https://wapbaike.baidu.com/item/" + path
        r = requests.get(url, headers=MOEGIRL_PAGE_UA, timeout=15)
        if r.status_code != 200:
            return None
        r.encoding = "utf-8"
        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.DOTALL)
        if not m:
            return None
        return json.loads(m.group(1))["props"]["pageProps"]["pageData"]
    except (requests.RequestException, ValueError, KeyError, TypeError):
        return None


def _baike_pick_lemma(pd, hint):
    """多义词（义项）消歧：从 navigation.lemmas 里挑出属于目标作品的那个义项。
    ⚠️ 默认跳转的义项**经常是错的**（「橘光」裸链接会跳到「南京大屠杀战犯」），
    所以多义词必须显式挑。挑不出来宁可不给——给错人的资料比没资料更糟。"""
    lemmas = (pd.get("navigation") or {}).get("lemmas") or []
    if not lemmas:
        return None
    if hint:
        aliases = FRANCHISE_ALIASES.get(hint, (hint,))
        for lm in lemmas:
            desc = (lm.get("lemmaDesc") or "").lower()
            if any(a.lower() in desc for a in aliases):
                return lm
    # 用户没带作品名时：退一步，挑简介里带「已知作品名」的义项（如蔚蓝档案）
    for lm in lemmas:
        desc = (lm.get("lemmaDesc") or "").lower()
        for group in FRANCHISE_ALIASES.values():
            if any(a.lower() in desc for a in group):
                return lm
    return None


def _baike_page_data(name, hint=""):
    """取词条 pageData；多义词先消歧到正确义项再取。"""
    pd = _baike_fetch(requests.utils.quote(name))
    if not pd:
        return None
    if not (pd.get("tips") or {}).get("isPolyseme"):
        return pd
    lm = _baike_pick_lemma(pd, hint)
    if not lm:
        log("baike_polyseme_unresolved", character=name, hint=hint)
        return None
    if lm.get("isCurrent"):
        return pd          # 当前页正好就是这个义项
    path = requests.utils.quote(name) + "/" + str(lm.get("lemmaId"))
    return _baike_fetch(path) or None


def _baike_text_of(node):
    """把一个内容节点转成纯文本；list 节点取 items 并用「；」连接。"""
    if not isinstance(node, dict):
        return ""
    if node.get("tag") == "list":
        parts = [_baike_text_of(it) for it in (node.get("items") or [])]
        return "；".join(p for p in parts if p)
    content = node.get("content")
    if isinstance(content, list):
        parts = []
        for c in content:
            if isinstance(c, dict):
                t = c.get("text")
                parts.append(t if isinstance(t, str) else _baike_text_of(c))
            elif isinstance(c, str):
                parts.append(c)
        return "".join(parts).strip()
    t = node.get("text")
    return t.strip() if isinstance(t, str) else ""


def _baike_sections(pd):
    """pageData.structuredContent → [{"h1": 一级标题, "h2": 二级标题, "text": 正文}]。
    header 节点开新节，其余节点（paragraph/list/module…）的正文归到当前节。"""
    out = []
    h1 = ""
    for block in pd.get("structuredContent") or []:
        if not isinstance(block, list):
            continue
        for node in block:
            if not isinstance(node, dict):
                continue
            tag = node.get("tag")
            if tag == "header":
                title = (node.get("title") or "").strip()
                if node.get("level", 1) <= 1:
                    h1 = title
                else:
                    out.append({"h1": h1, "h2": title, "text": []})
                continue
            if tag == "module" or not out:
                continue
            txt = _baike_text_of(node)
            if txt:
                out[-1]["text"].append(txt)
    return [{"h1": s["h1"], "h2": s["h2"], "text": "\n".join(s["text"])}
            for s in out if s["text"]]


def _baike_infobox(pd):
    """pageData.card.content → {字段: 值}，只保留形象相关字段。"""
    card = pd.get("card") or {}
    fields = {}
    for item in card.get("content") or []:
        if not isinstance(item, dict):
            continue
        title = (item.get("title") or "").strip()
        if title not in BAIKE_INFOBOX_TITLES:
            continue
        vals = []
        for d in item.get("data") or []:
            if not isinstance(d, dict):
                continue
            nodes = d.get("text")
            if isinstance(nodes, list):
                t = _baike_text_of({"content": nodes})
                if t:
                    vals.append(t)
        if vals:
            delim = item.get("delimiter") or "、"
            fields[title] = delim.join(vals) if len(vals) > 1 else vals[0]
    return fields


def _baike_pick(sections, keys):
    """按关键词挑首个命中的二级章节（章节名各词条不统一，不能写死）。"""
    for key in keys:
        for s in sections:
            if key in s["h2"]:
                return s
    return None


def _baike_abstract_text(pd):
    return "\n".join(t for t in (_baike_text_of(a) for a in (pd.get("abstract") or [])) if t)


def _baike_relevant(pd, hint):
    """有作品提示时，校验词条确实属于该作品——防止同名但不相干的人/物被当成角色。
    没有提示就放行（无信息可判断，交给下游）。"""
    if not hint:
        return True
    text = ((pd.get("lemmaDesc") or "") + " " + _baike_abstract_text(pd)).lower()
    aliases = FRANCHISE_ALIASES.get(hint, (hint,))
    return any(a.lower() in text for a in aliases)


def search_baike(name, hint=""):
    """百度百科角色资料：信息框 + 身份背景 + 性格特点 + 衣着外貌。
    身份/性格让 AI 能理解用户指令（如“让她生气”该配什么神态），外貌用于写提示词。
    hint 是查询里带的作品名，用于多义词消歧与相关性校验（见 _baike_pick_lemma）。"""
    key = (name, hint)
    if key in _baike_result_cache:
        return _baike_result_cache[key]
    pd = _baike_page_data(name, hint)
    if not pd:
        return None
    if not _baike_relevant(pd, hint):
        log("baike_irrelevant", character=name, hint=hint,
            desc=pd.get("lemmaDesc"))
        return None

    sections = _baike_sections(pd)
    lines = []
    desc = (pd.get("lemmaDesc") or "").strip()
    if desc:
        lines.append("（来源：百度百科）" + desc)
    info = _baike_infobox(pd)
    if info:
        lines.append("【信息框】" + "；".join(f"{k}：{v}" for k, v in info.items()))

    picked = []
    for group, keys in (("identity", BAIKE_IDENTITY_KEYS),
                        ("personality", BAIKE_PERSONALITY_KEYS),
                        ("appearance", BAIKE_APPEARANCE_KEYS)):
        s = _baike_pick(sections, keys)
        if s and all(s is not p[1] for p in picked):
            picked.append((group, s))
    for group, s in picked:
        lines.append(f"【{s['h2']}】" + s["text"][:BAIKE_SECTION_CAPS[group]])

    # 兜底：词条没有这三类章节（如非游戏角色）时，至少给概述段（信息框上面已加）
    if not picked:
        abstract = _baike_abstract_text(pd)
        if abstract:
            lines.append("【概述】" + abstract[:600])

    text = "\n".join(lines).strip()[:BAIKE_TOTAL_CAP]
    if not text:
        return None
    result = {"title": (pd.get("lemmaTitle") or name), "text": text, "source": "baike"}
    if len(_baike_result_cache) > 100:
        _baike_result_cache.clear()
    _baike_result_cache[key] = result
    return result


def search_character(name, hint=""):
    for prov in _search_providers:
        try:
            if prov == "baike":
                r = search_baike(name, hint)
            elif prov == "moegirl":
                r = search_moegirl(name)
            elif prov == "wikipedia":
                r = search_wikipedia(name)
            elif prov == "serper":
                r = search_serper(name)
            else:
                continue
        except Exception:
            r = None
        if r and r.get("text"):
            r["provider"] = prov
            return r
    return None


FRANCHISE_WORDS = (
    "蔚蓝档案", "blue archive", "碧蓝航线", "azur lane", "原神", "genshin impact", "genshin",
    "崩坏星穹铁道", "崩坏", "honkai star rail", "honkai", "明日方舟", "arknights",
    "少女前线", "girls' frontline", "girls frontline", "公主连结", "公主连接", "priconne",
    "赛马娘", "umamusume", "love live", "lovelive", "偶像大师", "idolmaster",
    "bang dream", "东方project", "touhou", "fgo", "fate grand order",
)


def _clean_query(name):
    """去掉常见系列/游戏前缀，避免「蔚蓝档案 砂狼白子」这种带前缀的查询搜不到萌百页面。"""
    q = re.sub(r"\s+", " ", name or "").strip()
    for w in FRANCHISE_WORDS:
        q = re.sub(re.escape(w), " ", q, flags=re.IGNORECASE)
    q = re.sub(r"\s+", " ", q).strip()
    q = re.sub(r"\(\s*\)", "", q).strip()
    return q


def search_character_deadline(name, budget=15.0):
    """带时间预算的角色搜索：budget 秒内完成返回 (result, False)，超时放弃返回 (None, True)。
    自动去掉常见系列前缀（如「蔚蓝档案 砂狼白子」→「砂狼白子」）后再搜，提高命中率；
    同时把作品名作为 hint 传下去，供百度百科消歧（多义词「橘光」必须靠它才不会搜到同名的战犯）。
    防止搜索网络卡顿（如维基百科无法访问）把聊天/手动搜索拖到 1 分钟以上。"""
    box = {}
    def _run():
        cleaned = _clean_query(name)
        box["r"] = search_character(cleaned or name, _franchise_hint(name))
    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(budget)
    if t.is_alive():
        log("search_timeout", character=name)
        return None, True
    return box.get("r"), False


def detect_character(msg):
    if not _api_key:
        return None
    try:
        resp = requests.post(
            DEEPSEEK_BASE.rstrip("/") + "/chat/completions",
            headers={"Authorization": f"Bearer {_api_key}", "Content-Type": "application/json"},
            json={
                "model": DEEPSEEK_MODEL,
                "messages": [
                    {"role": "system", "content":
                        "你只负责从用户消息里提取动漫/游戏/影视角色名。\n"
                        "规则：\n"
                        "1. 如果消息里提到了具体角色名，输出该角色的名字（一个即可，用最常见的中文或英文写法，如\"古关忧\"或\"Kozeki Ui\"）。\n"
                        "2. 如果消息里没有提到任何具体角色（只是描述画面/场景/风格，或泛指\"一个少女\"\"猫娘\"\"白毛\"这类），输出 NONE。\n"
                        "3. 只输出角色名或 NONE，不要任何其它文字。"},
                    {"role": "user", "content": msg},
                ],
                "temperature": 0,
                "max_tokens": 30,
                "stream": False,
            },
            timeout=30,
        )
        if resp.status_code != 200:
            return None
        name = (resp.json().get("choices") or [{}])[0].get("message", {}).get("content", "").strip()
        if not name or name.upper() == "NONE":
            return None
        return name
    except (requests.RequestException, ValueError, KeyError, IndexError):
        return None


# ---------- 图片回传（视觉反馈） ----------
# 用户把刚生成的图传回给 AI 看构图/神态/服装。⚠️ 图片**不存进 sessions.json**（会被
# base64 撑爆），消息里只存文件名，请求时才临时编码。压缩由前端 canvas 完成（本机没装 Pillow），
# 落盘为 <原图主干>.vision.jpg（幂等：重传同一张图不会重复堆文件）。
VISION_SUFFIX = ".vision.jpg"
VISION_MAX_IMAGES = 6                # 一次最多回传几张
VISION_MAX_BYTES = 8 * 1024 * 1024   # 单张 base64 解码前的字符数上限
VISION_TOTAL_BYTES = 30 * 1024 * 1024  # 一次请求的图片总量上限（DeepSeek 请求体上限 48MiB）

VISION_DEFAULT_NOTE = "分析这张图的构图元素：镜头距离与视角、人物姿态与神态、服装细节、背景与配色"

VISION_INTRO = (
    "（用户回传了刚生成的图片，让你据此理解他想调整的构图/神态/服装。"
    "按他的关注点分析并指出与描述不符之处；"
    "这一轮不要输出 <prompt>/<negative> 标签，除非他明确说要生成或修改提示词。）"
)


def _save_vision_images(items):
    """把前端压缩好的图片落盘，返回文件名列表。
    items: [{"name": 原图文件名, "data": "data:image/jpeg;base64,..."}]
    返回 (filenames, errors)。"""
    filenames, errors = [], []
    total = 0
    for it in (items or [])[:VISION_MAX_IMAGES]:
        if not isinstance(it, dict):
            continue
        data = (it.get("data") or "").strip()
        if not data:
            continue
        if len(data) > VISION_MAX_BYTES:
            errors.append(f"{it.get('name', '?')} 太大，已跳过")
            continue
        total += len(data)
        if total > VISION_TOTAL_BYTES:
            errors.append("图片总量超限，剩余已跳过")
            break
        if "," not in data:
            errors.append(f"{it.get('name', '?')} 格式不对，已跳过")
            continue
        try:
            raw = base64.b64decode(data.split(",", 1)[1], validate=False)
        except Exception:
            errors.append(f"{it.get('name', '?')} 解码失败，已跳过")
            continue
        # 用原图主干命名，去掉扩展名和自己的 .vision 后缀，保证幂等
        stem = os.path.splitext(os.path.basename(it.get("name") or "image"))[0]
        stem = stem.replace(VISION_SUFFIX, "") or "image"
        fname = stem + VISION_SUFFIX
        try:
            with open(os.path.join(IMAGE_DIR, fname), "wb") as f:
                f.write(raw)
        except OSError as e:
            errors.append(f"{fname} 写入失败：{e}")
            continue
        filenames.append(fname)
    # 去重保序（对比生成里同名图不会出现，但保险起见）
    seen, uniq = set(), []
    for f in filenames:
        if f not in seen:
            seen.add(f)
            uniq.append(f)
    return uniq, errors


def _image_block(fname):
    """把 images/ 下的图片编码成 OpenAI 多模态 image_url 块；读不到返回 None。
    ⚠️ 必须 basename：文件名来自消息记录，防止目录穿越。"""
    safe = os.path.basename(fname or "")
    if not safe:
        return None
    path = os.path.join(IMAGE_DIR, safe)
    try:
        if os.path.getsize(path) > 32 * 1024 * 1024:
            return None
        with open(path, "rb") as f:
            raw = f.read()
    except (OSError, ValueError):
        return None
    ext = os.path.splitext(safe)[1].lower()
    mime = "image/png" if ext == ".png" else ("image/webp" if ext == ".webp" else "image/jpeg")
    return {"type": "image_url",
            "image_url": {"url": f"data:{mime};base64," + base64.b64encode(raw).decode()}}


def _build_history(msgs):
    """把分支消息转成 DeepSeek 的 messages。
    ⚠️ 回传的图片**只有最近一次**带载荷（更早的降级成文字占位）——
    否则从第二轮起每轮都要重传几 MB，越聊越慢。"""
    last_img_idx = -1
    for i, m in enumerate(msgs):
        if m.get("role") == "user" and m.get("images"):
            last_img_idx = i

    out = []
    for i, m in enumerate(msgs):
        role = m.get("role")
        if role == "search":
            if m.get("found"):
                out.append({"role": "user", "content":
                            f"[联网搜索到的角色资料] {m.get('character', '')}（来源：{m.get('source', '')}）\n"
                            f"以下是系统已搜索并注入的该角色资料（身份背景/性格特点/衣着外貌）。"
                            f"请用其中的外貌描述编写提示词；并用身份与性格理解用户的意图、推断合适的表情神态与动作；"
                            f"不要重复要求搜索或要求用户再提供外貌。\n{m.get('content', '')}"})
            else:
                out.append({"role": "user", "content":
                            f"[尝试联网搜索「{m.get('character', '')}」但未找到资料，请按规则向用户提问确认]"})
            continue

        text = m.get("content", "")
        imgs = m.get("images") or []
        if role == "user" and imgs:
            if i == last_img_idx:
                blocks = [{"type": "text", "text": f"{VISION_INTRO}\n{text}"}]
                # 去重：同名图只编码一次，免得同一张被当多张重复计费
                blocks += [b for b in (_image_block(f) for f in dict.fromkeys(imgs)) if b]
                if len(blocks) > 1:      # 至少真编码出一张才用数组形式
                    out.append({"role": "user", "content": blocks})
                    continue
                text = f"{VISION_INTRO}\n{text}\n[图片读取失败]"
            else:
                text = f"{text}\n[此前回传过图片：{'、'.join(imgs)}]"
        out.append({"role": role, "content": text})
    return out


# ---------- ComfyUI 生图（Anima） ----------
def _watch_progress(pid, max_steps):
    """后台线程：连 ComfyUI WebSocket，把指定 prompt 的采样进度写进 PROGRESS。"""
    ws = None
    try:
        ws = websocket.create_connection(COMFY_URL.replace("http://", "ws://") + "/ws", timeout=10)
    except Exception as e:
        log("ws_error", pid=pid, error=str(e))
        return
    try:
        while True:
            try:
                raw = ws.recv()
            except websocket.WebSocketTimeoutException:
                continue
            except Exception:
                break
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            mtype = msg.get("type")
            data = msg.get("data") or {}
            if mtype == "progress" and data.get("prompt_id") == pid:
                with LOCK:
                    m = data.get("max") or max_steps
                    PROGRESS[pid] = {"value": int(data.get("value", 0)), "max": int(m), "status": "running"}
            elif mtype == "execution_success" and data.get("prompt_id") == pid:
                with LOCK:
                    p = PROGRESS.get(pid)
                    PROGRESS[pid] = {"value": (p or {}).get("max", max_steps), "max": (p or {}).get("max", max_steps), "status": "done"}
                break
            elif mtype == "execution_error" and data.get("prompt_id") == pid:
                with LOCK:
                    p = PROGRESS.get(pid)
                    PROGRESS[pid] = {"value": (p or {}).get("value", 0), "max": (p or {}).get("max", max_steps), "status": "error"}
                break
    finally:
        try:
            ws.close()
        except Exception:
            pass


def comfy_workflow(prompt, negative, seed, steps, width, height, cfg):
    return {
        "44": {"class_type": "UNETLoader", "inputs": {"unet_name": "anima-base-v1.0.safetensors", "weight_dtype": "default"}},
        "45": {"class_type": "CLIPLoader", "inputs": {"clip_name": "qwen_3_06b_base.safetensors", "type": "stable_diffusion", "device": "default"}},
        "15": {"class_type": "VAELoader", "inputs": {"vae_name": "qwen_image_vae.safetensors"}},
        "11": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["45", 0], "text": prompt}},
        "12": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["45", 0], "text": negative}},
        "28": {"class_type": "EmptyLatentImage", "inputs": {"width": width, "height": height, "batch_size": 1}},
        "19": {"class_type": "KSampler", "inputs": {"model": ["44", 0], "positive": ["11", 0], "negative": ["12", 0], "latent_image": ["28", 0], "seed": seed, "steps": steps, "cfg": cfg, "sampler_name": "er_sde", "scheduler": "simple", "denoise": 1.0}},
        "8": {"class_type": "VAEDecode", "inputs": {"samples": ["19", 0], "vae": ["15", 0]}},
        "1": {"class_type": "SaveImage", "inputs": {"images": ["8", 0], "filename_prefix": "anima"}},
    }


def comfy_generate(prompt, negative, params):
    global CURRENT_PID
    seed = params.get("seed")
    if seed is None:
        seed = random.randint(0, 2**31 - 1)
    wf = comfy_workflow(prompt, negative, seed, params["steps"], params["width"], params["height"], params["cfg"])
    try:
        r = requests.post(COMFY_URL + "/prompt", json={"prompt": wf}, timeout=60)
    except requests.RequestException as e:
        log("submit_error", error=f"无法连接 ComfyUI：{e}")
        return None, seed, f"无法连接 ComfyUI（{COMFY_URL}）：{e}"
    if r.status_code != 200:
        log("submit_error", error=r.text[:300])
        return None, seed, f"ComfyUI 提交失败 {r.status_code}：{r.text[:300]}"
    pid = r.json().get("prompt_id")
    if not pid:
        return None, seed, "ComfyUI 未返回 prompt_id"
    CURRENT_PID = pid
    with LOCK:
        PROGRESS.clear()
        PROGRESS[pid] = {"value": 0, "max": params["steps"], "status": "running"}
    threading.Thread(target=_watch_progress, args=(pid, params["steps"]), daemon=True).start()
    log("submit", pid=pid, seed=seed, steps=params["steps"], size=f"{params['width']}x{params['height']}")
    return pid, seed, None


def comfy_wait(pid, timeout=600):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            r = requests.get(f"{COMFY_URL}/history/{pid}", timeout=30)
        except requests.RequestException:
            time.sleep(2)
            continue
        if r.status_code == 200:
            h = r.json().get(pid)
            if h:
                st = h.get("status", {}).get("status_str")
                if st == "success":
                    with LOCK:
                        p = PROGRESS.get(pid)
                        if p:
                            p["status"] = "done"
                    for out in h.get("outputs", {}).values():
                        for im in out.get("images", []):
                            src = os.path.join(COMFY_OUTPUT, im.get("subfolder", ""), im["filename"])
                            dst = os.path.join(IMAGE_DIR, im["filename"])
                            try:
                                shutil.copy(src, dst)
                            except Exception as e:
                                log("copy_error", pid=pid, error=str(e))
                                return None, f"复制图片失败：{e}"
                            log("done", pid=pid, file=im["filename"], secs=round(time.time() - t0, 1))
                            return im["filename"], None
                    log("no_image", pid=pid)
                    return None, "生成成功但未找到图片"
                if st == "error":
                    with LOCK:
                        p = PROGRESS.get(pid)
                        if p:
                            p["status"] = "error"
                    log("error", pid=pid, error=json.dumps(h.get("status"), ensure_ascii=False)[:300])
                    return None, "ComfyUI 生成失败：" + json.dumps(h.get("status"), ensure_ascii=False)[:500]
        time.sleep(2)
    log("timeout", pid=pid)
    return None, "生成超时"


# ---------- 路由 ----------
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/key", methods=["GET"])
def get_key():
    return jsonify({"has_key": bool(_api_key), "masked": (_api_key[:6] + "..." + _api_key[-4:]) if _api_key else ""})


@app.route("/api/key", methods=["POST"])
def set_key():
    global _api_key
    k = (request.get_json() or {}).get("api_key", "").strip()
    _api_key = k
    save_config()
    return jsonify({"ok": True, "has_key": bool(_api_key)})


@app.route("/api/rules", methods=["GET"])
def get_rules():
    return jsonify({"rules": _rules, "presets": _presets})


@app.route("/api/rules", methods=["POST"])
def set_rules():
    global _rules
    data = request.get_json() or {}
    if "rules" in data:
        _rules = (data["rules"] or "").strip() or DEFAULT_RULES
        save_config()
    return jsonify({"ok": True})


@app.route("/api/rules/presets", methods=["POST"])
def create_preset():
    """新建一个命名预设规则（同名则返回 409）。"""
    global _presets
    data = request.get_json() or {}
    name = (data.get("name") or "").strip()
    content = (data.get("content") or "").strip()
    if not name or not content:
        return jsonify({"error": "预设名称和内容不能为空"}), 400
    if name in _presets:
        return jsonify({"error": f"预设「{name}」已存在，请直接编辑或换名"}), 409
    _presets[name] = content
    save_config()
    return jsonify({"ok": True, "presets": _presets})


@app.route("/api/rules/presets/<name>", methods=["PUT"])
def update_preset(name):
    """更新某个预设的内容。"""
    global _presets
    if name not in _presets:
        return jsonify({"error": "预设不存在"}), 404
    data = request.get_json() or {}
    content = (data.get("content") or "").strip()
    if not content:
        return jsonify({"error": "预设内容不能为空"}), 400
    _presets[name] = content
    save_config()
    return jsonify({"ok": True, "presets": _presets})


@app.route("/api/rules/presets/<name>/rename", methods=["POST"])
def rename_preset(name):
    """重命名预设。"""
    global _presets
    if name not in _presets:
        return jsonify({"error": "预设不存在"}), 404
    data = request.get_json() or {}
    new_name = (data.get("new_name") or "").strip()
    if not new_name:
        return jsonify({"error": "新名称不能为空"}), 400
    if new_name == name:
        return jsonify({"ok": True, "presets": _presets})
    if new_name in _presets:
        return jsonify({"error": f"预设「{new_name}」已存在"}), 409
    _presets[new_name] = _presets.pop(name)
    save_config()
    return jsonify({"ok": True, "presets": _presets})


@app.route("/api/rules/presets/<name>", methods=["DELETE"])
def delete_preset(name):
    global _presets
    if name not in _presets:
        return jsonify({"error": "预设不存在"}), 404
    del _presets[name]
    save_config()
    return jsonify({"ok": True, "presets": _presets})


@app.route("/api/search/settings", methods=["GET"])
def get_search_settings():
    masked = ""
    if _serper_key:
        masked = (_serper_key[:4] + "..." + _serper_key[-3:]) if len(_serper_key) > 7 else (_serper_key[:3] + "...")
    return jsonify({"providers": _search_providers, "auto": _search_auto,
                    "has_serper_key": bool(_serper_key), "serper_key_masked": masked})


@app.route("/api/search/settings", methods=["POST"])
def set_search_settings():
    global _search_providers, _search_auto, _serper_key
    data = request.get_json() or {}
    if "providers" in data:
        provs = [p for p in data["providers"] if p in ("baike", "moegirl", "wikipedia", "serper")]
        if provs:
            _search_providers = provs
    if "auto" in data:
        _search_auto = bool(data["auto"])
    if "serper_api_key" in data:
        k = (data["serper_api_key"] or "").strip()
        if k:
            _serper_key = k
    save_config()
    return jsonify({"ok": True})


@app.route("/api/search", methods=["POST"])
def do_search():
    data = request.get_json() or {}
    sid = data.get("session_id")
    query = (data.get("query") or "").strip()
    if not query:
        return jsonify({"error": "请输入要搜索的角色名"}), 400
    result, timed_out = search_character_deadline(query)
    if not result:
        return jsonify({"found": False, "query": query, "providers": _search_providers, "timeout": timed_out})
    if sid:
        with LOCK:
            s = SESSIONS.get(sid)
            if s:
                b = s["branches"].get(s["active"])
                if b:
                    b["messages"].append({
                        "role": "search", "character": query,
                        "content": result["text"], "source": result["provider"], "found": True,
                    })
                    s["updated"] = time.time()
                    save_sessions()
    return jsonify({"found": True, "query": query, "source": result["provider"],
                    "title": result.get("title", ""), "text": result["text"]})


@app.route("/api/session/new", methods=["POST"])
def new_session():
    sid = uuid.uuid4().hex[:12]
    branch = _new_branch()
    with LOCK:
        SESSIONS[sid] = {"title": "新对话", "updated": time.time(), "active": branch["id"], "branches": {branch["id"]: branch}}
        save_sessions()
    return jsonify({"session_id": sid})


@app.route("/api/sessions", methods=["GET"])
def list_sessions():
    with LOCK:
        items = [{"id": sid, "title": s.get("title", "新对话"), "updated": s.get("updated", 0)}
                 for sid, s in SESSIONS.items()]
    items.sort(key=lambda x: x["updated"], reverse=True)
    return jsonify({"sessions": items})


@app.route("/api/session/<sid>", methods=["GET"])
def get_session(sid):
    with LOCK:
        s = SESSIONS.get(sid)
        if not s:
            return jsonify({"error": "会话不存在"}), 404
        b = s["branches"].get(s["active"])
        if not b:
            return jsonify({"error": "会话不存在"}), 404
        branches = [{"id": bid, "forked_from": br.get("forked_from"), "count": len(br.get("messages", []))}
                    for bid, br in s["branches"].items()]
        resp = {
            "session_id": sid,
            "title": s.get("title", "新对话"),
            "active": s["active"],
            "branches": branches,
            "messages": b.get("messages", []),
            "prompt": b.get("prompt", ""),
            "negative": b.get("negative", DEFAULT_NEGATIVE),
            "last_image": ("/images/" + b["last_image"]) if b.get("last_image") else "",
            "params": b.get("params", {}),
            "versions": _version_groups(s, b),
            "assistant_versions": _assistant_version_groups(s, b),
        }
    return jsonify(resp)


@app.route("/api/session/<sid>/prompt", methods=["POST"])
def set_prompt(sid):
    data = request.get_json() or {}
    prompt = (data.get("prompt") or "").strip()
    with LOCK:
        s = SESSIONS.get(sid)
        if not s:
            return jsonify({"error": "会话不存在"}), 404
        b = s["branches"].get(s["active"])
        if not b:
            return jsonify({"error": "会话不存在"}), 404
        b["prompt"] = prompt
        s["updated"] = time.time()
        save_sessions()
    return jsonify({"ok": True, "prompt": prompt})


@app.route("/api/session/<sid>/negative", methods=["POST"])
def set_negative(sid):
    data = request.get_json() or {}
    neg = (data.get("negative") or "").strip()
    with LOCK:
        s = SESSIONS.get(sid)
        if not s:
            return jsonify({"error": "会话不存在"}), 404
        b = s["branches"].get(s["active"])
        if not b:
            return jsonify({"error": "会话不存在"}), 404
        if neg:
            b["negative"] = neg
            s["updated"] = time.time()
            save_sessions()
    return jsonify({"ok": True, "negative": b.get("negative", DEFAULT_NEGATIVE)})


@app.route("/api/session/<sid>/rewrite", methods=["POST"])
def rewrite_message(sid):
    """重写第 index 条 user 消息：保留 index 之前的内容，新建分支并设为 active（原分支保留，可 ‹/› 切回）。
    前端随后用 /api/chat 发送修改后的内容，会追加到新分支并引出新回答。"""
    data = request.get_json() or {}
    try:
        index = int(data.get("index", -1))
    except (TypeError, ValueError):
        return jsonify({"error": "index 必须是整数"}), 400
    with LOCK:
        s = SESSIONS.get(sid)
        if not s:
            return jsonify({"error": "会话不存在"}), 404
        src = s["branches"].get(s["active"])
        if not src:
            return jsonify({"error": "会话不存在"}), 404
        msgs = src.get("messages", [])
        if index < 0 or index >= len(msgs):
            return jsonify({"error": "消息不存在"}), 400
        prefix = [dict(m) for m in msgs[:index]]
        # 回退 prompt/negative 到 index 前最后一条助理消息
        new_prompt = ""
        new_negative = DEFAULT_NEGATIVE
        for m in reversed(prefix):
            if m.get("role") == "assistant":
                if m.get("prompt"):
                    new_prompt = m["prompt"]
                if m.get("negative"):
                    new_negative = m["negative"]
                break
        branch = _new_branch(messages=prefix, prompt=new_prompt, negative=new_negative,
                             forked_from={"branch": src["id"], "index": index, "type": "rewrite"})
        s["branches"][branch["id"]] = branch
        s["active"] = branch["id"]
        s["updated"] = time.time()
        save_sessions()
    return jsonify({"ok": True, "branch": branch["id"]})


@app.route("/api/session/<sid>/regenerate", methods=["POST"])
def regenerate_message(sid):
    """重新生成第 index 条 assistant 消息：保留其前面的上下文（含对应的 user 提问）新建分支，原分支保留。
    前端随后调用 /api/chat {regenerate:true} 在无新用户消息的情况下续写新回答。"""
    data = request.get_json() or {}
    try:
        index = int(data.get("index", -1))
    except (TypeError, ValueError):
        return jsonify({"error": "index 必须是整数"}), 400
    with LOCK:
        s = SESSIONS.get(sid)
        if not s:
            return jsonify({"error": "会话不存在"}), 404
        src = s["branches"].get(s["active"])
        if not src:
            return jsonify({"error": "会话不存在"}), 404
        msgs = src.get("messages", [])
        if index <= 0 or index >= len(msgs) or msgs[index].get("role") != "assistant":
            return jsonify({"error": "只能重新生成 AI 回答"}), 400
        prefix = [dict(m) for m in msgs[:index]]
        new_prompt = ""
        new_negative = DEFAULT_NEGATIVE
        for m in reversed(prefix):
            if m.get("role") == "assistant":
                if m.get("prompt"):
                    new_prompt = m["prompt"]
                if m.get("negative"):
                    new_negative = m["negative"]
                break
        branch = _new_branch(messages=prefix, prompt=new_prompt, negative=new_negative,
                             forked_from={"branch": src["id"], "index": index, "type": "regenerate"})
        s["branches"][branch["id"]] = branch
        s["active"] = branch["id"]
        s["updated"] = time.time()
        save_sessions()
    return jsonify({"ok": True, "branch": branch["id"]})


@app.route("/api/session/<sid>/switch", methods=["POST"])
def switch_branch(sid):
    """切换到指定分支（‹/› 版本切换用）。"""
    data = request.get_json() or {}
    bid = data.get("branch")
    with LOCK:
        s = SESSIONS.get(sid)
        if not s or bid not in s.get("branches", {}):
            return jsonify({"error": "分支不存在"}), 404
        s["active"] = bid
        s["updated"] = time.time()
        save_sessions()
    return jsonify({"ok": True})


@app.route("/api/session/<sid>", methods=["DELETE"])
def delete_session(sid):
    with LOCK:
        if sid not in SESSIONS:
            return jsonify({"error": "会话不存在"}), 404
        del SESSIONS[sid]
        save_sessions()
    return jsonify({"ok": True})


@app.route("/api/chat", methods=["POST"])
def chat():
    data = request.get_json() or {}
    sid = data.get("session_id")
    regen = bool(data.get("regenerate"))
    msg = (data.get("message") or "").strip()
    # 图片回传：前端已 canvas 压成 JPEG 的 data URL；落盘后只在消息里存文件名
    vision_files, vision_errors = _save_vision_images(data.get("images"))
    if not regen and not msg and vision_files:
        msg = VISION_DEFAULT_NOTE
    if not regen and not msg:
        return jsonify({"error": "消息为空"}), 400
    if not _api_key:
        return jsonify({"error": "尚未设置 DeepSeek API key，请在右上角填写后点“保存 Key”"}), 400

    with LOCK:
        s = SESSIONS.get(sid)
        if not s:
            return jsonify({"error": "会话不存在"}), 404
        b = s["branches"].get(s["active"])
        if not b:
            return jsonify({"error": "会话不存在"}), 404
        if regen:
            # 重新生成：不追加用户消息，分支末尾应已是那条用户提问
            if not b["messages"] or b["messages"][-1].get("role") != "user":
                return jsonify({"error": "没有可重新生成的提问"}), 400
        else:
            first = not b["messages"]
            entry = {"role": "user", "content": msg}
            if vision_files:
                entry["images"] = vision_files
            b["messages"].append(entry)
            if first:
                s["title"] = msg[:20]
        s["updated"] = time.time()
        save_sessions()

    search_event = None
    # 回传图片是让 AI 看图，不是提新角色，别触发自动搜索
    if _search_auto and not regen and not vision_files:
        cname = detect_character(msg)
        if cname:
            already = any(m.get("role") == "search" and m.get("character") == cname for m in b["messages"])
            if not already:
                r, _ = search_character_deadline(cname)
                search_event = {
                    "character": cname,
                    "found": bool(r),
                    "source": (r or {}).get("provider", ""),
                    "title": (r or {}).get("title", ""),
                    "text": (r or {}).get("text", ""),
                }
                with LOCK:
                    b["messages"].append({
                        "role": "search", "character": cname,
                        "content": search_event["text"],
                        "source": search_event["source"], "found": search_event["found"],
                    })
                    s["updated"] = time.time()
                    save_sessions()

    with LOCK:
        history = _build_history(b["messages"])
    messages = [{"role": "system", "content": _rules}] + history

    def gen():
        if search_event:
            yield f"data: {json.dumps({'type': 'search', **search_event}, ensure_ascii=False)}\n\n"
        if vision_errors:
            yield f"data: {json.dumps({'type': 'warning', 'error': '；'.join(vision_errors)}, ensure_ascii=False)}\n\n"
        full = ""
        try:
            resp = requests.post(
                DEEPSEEK_BASE.rstrip("/") + "/chat/completions",
                headers={"Authorization": f"Bearer {_api_key}", "Content-Type": "application/json"},
                json={"model": DEEPSEEK_MODEL, "messages": messages, "stream": True, "temperature": 0.7},
                stream=True, timeout=300,
            )
            if resp.status_code != 200:
                yield f"data: {json.dumps({'type': 'error', 'error': f'DeepSeek 返回 {resp.status_code}: {resp.text[:300]}'}, ensure_ascii=False)}\n\n"
                return
            for raw in resp.iter_lines():
                if not raw:
                    continue
                line = raw.decode("utf-8", errors="ignore")
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    delta = json.loads(payload)["choices"][0].get("delta", {}).get("content", "")
                except Exception:
                    continue
                if delta:
                    full += delta
                    yield f"data: {json.dumps({'type': 'delta', 'content': delta}, ensure_ascii=False)}\n\n"
            prompt = extract_prompt(full)
            negative = extract_negative(full)
            with LOCK:
                b["messages"].append({"role": "assistant", "content": full,
                                      "prompt": prompt or None, "negative": negative or None})
                if prompt:
                    b["prompt"] = prompt
                if negative:
                    b["negative"] = negative
                s["updated"] = time.time()
                save_sessions()
            yield f"data: {json.dumps({'type': 'done', 'prompt': prompt, 'negative': negative}, ensure_ascii=False)}\n\n"
        except requests.RequestException as e:
            yield f"data: {json.dumps({'type': 'error', 'error': f'网络错误：{e}'}, ensure_ascii=False)}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'error': str(e)}, ensure_ascii=False)}\n\n"

    return Response(gen(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _do_generate(sid, data, n=1):
    global _generating
    with LOCK:
        s = SESSIONS.get(sid)
        if not s:
            return jsonify({"error": "会话不存在"}), 404
        b = s["branches"].get(s["active"])
        if not b:
            return jsonify({"error": "会话不存在"}), 404
        prompt = (data.get("prompt") or "").strip() or b.get("prompt", "")
        negative = (data.get("negative") or "").strip() or b.get("negative", DEFAULT_NEGATIVE)
        base = dict(b.get("params", {}))
    if not prompt:
        return jsonify({"error": "还没有提示词，请先和助手讨论或手动填写"}), 400

    steps = int(data.get("steps", base.get("steps", 30)))
    width = int(data.get("width", base.get("width", 1024)))
    height = int(data.get("height", base.get("height", 1024)))
    cfg = float(data.get("cfg", base.get("cfg", 4.0)))
    fixed_seed = data.get("seed")
    if fixed_seed in ("", None):
        fixed_seed = None
    else:
        try:
            fixed_seed = int(fixed_seed)
        except (TypeError, ValueError):
            fixed_seed = None

    _generating = True
    try:
        results = []
        for i in range(n):
            with LOCK:
                GEN_META["gen_index"] = i + 1
                GEN_META["gen_total"] = n
            params = {"seed": fixed_seed if n == 1 else None, "steps": steps, "width": width, "height": height, "cfg": cfg}
            pid, seed, err = comfy_generate(prompt, negative, params)
            if err:
                return jsonify({"error": err}), 500
            fname, err = comfy_wait(pid)
            if err:
                return jsonify({"error": err}), 500
            results.append({"image": f"/images/{fname}", "seed": seed})
    finally:
        _generating = False

    with LOCK:
        b["prompt"] = prompt
        b["negative"] = negative
        b["params"] = {"seed": results[-1]["seed"] if results else None, "steps": steps, "width": width, "height": height, "cfg": cfg}
        if results:
            b["last_image"] = results[-1]["image"].split("/")[-1]
        s["updated"] = time.time()
        save_sessions()
    return jsonify({"images": results, "prompt": prompt}), 200


@app.route("/api/generate", methods=["POST"])
def generate():
    data = request.get_json() or {}
    sid = data.get("session_id")
    return _do_generate(sid, data, n=1)


@app.route("/api/generate_multi", methods=["POST"])
def generate_multi():
    data = request.get_json() or {}
    sid = data.get("session_id")
    n = min(max(int(data.get("n", 4)), 1), 4)
    return _do_generate(sid, data, n=n)


@app.route("/api/generate/stop", methods=["POST"])
def stop_generate():
    pid = CURRENT_PID
    try:
        if pid:
            requests.post(COMFY_URL + "/interrupt", json={}, timeout=10)
            log("interrupt", pid=pid)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True})


@app.route("/api/progress", methods=["GET"])
def get_progress():
    """返回当前生成进度（供前端轮询渲染进度条）。"""
    with LOCK:
        if not _generating:
            return jsonify({"status": "idle"})
        pid = CURRENT_PID
        p = PROGRESS.get(pid, {})
        return jsonify({
            "status": p.get("status", "running"),
            "value": p.get("value", 0),
            "max": p.get("max", 30),
            "gen_index": GEN_META.get("gen_index", 1),
            "gen_total": GEN_META.get("gen_total", 1),
        })


@app.route("/api/logs", methods=["GET"])
def get_logs():
    return jsonify({"logs": list(LOGS)})


@app.route("/api/images", methods=["GET"])
def list_images():
    try:
        files = os.listdir(IMAGE_DIR)
        files.sort(key=lambda f: os.path.getmtime(os.path.join(IMAGE_DIR, f)), reverse=True)
    except Exception:
        files = []
    # ⚠️ 排除 .vision.jpg —— 那是回传给 AI 看的压缩副本（见 _save_vision_images），不是作品
    imgs = [{"name": f, "url": "/images/" + f} for f in files
            if f.lower().endswith((".png", ".jpg", ".jpeg", ".webp")) and not f.endswith(VISION_SUFFIX)]
    return jsonify({"images": imgs})


@app.route("/images/<path:name>")
def images(name):
    return send_from_directory(IMAGE_DIR, name)


load_config()
load_sessions()

if __name__ == "__main__":
    print("Anima Studio: http://127.0.0.1:7861")
    app.run(host="127.0.0.1", port=7861, debug=False, threaded=True)
