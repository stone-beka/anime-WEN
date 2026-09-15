# -*- coding: utf-8 -*-
"""图片回传「真实链路」端到端验证（会真调 DeepSeek，消耗 API 额度）。

和 tests/test_vision_history.py 的分工：
  - test_vision_history.py：纯单元，不联网、不起服务，改代码就该跑。
  - 本文件：真发图给 DeepSeek，验证「AI 确实看得见图」和「该不该出提示词」。
    只在动了视觉链路（_save_vision_images / _image_block / _build_history /
    /api/chat 的 images 分支 / rules 里的图片反馈条款）之后手动跑一次。

前置条件：
  1. E:\\anima-studio 服务已在 7861 运行（`.venv/Scripts/python.exe app.py`）
  2. images/ 目录里有至少 4 张生成图
  3. 有 JPEG 编码能力。⚠️ 服务用的 .venv **没装 Pillow**（只有 flask/requests），
     所以本脚本借用 ComfyUI 的解释器转格式（它自带 Pillow）：
        /e/ComfyUI/.venv/Scripts/python.exe tests/e2e_vision_live.py
     直接用 .venv 跑会在第 ① 步给出提示并退出。

⚠️ 别用 shell 传中文参数（交接文档坑 ⑧，会 HTTP 400）——所有文本都写在文件里。
"""
import base64
import io
import json
import os
import sys
import time

import requests

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BASE = "http://127.0.0.1:7861"
IMAGE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "images")
JPEG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_e2e_tmp")

fails = []


def chk(label, cond, extra=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}" + (f"  {extra}" if extra else ""))
    if not cond:
        fails.append(label)


# ── ① 准备真 JPEG（前端是 canvas→JPEG，所以测试也必须是真 JPEG：
#       _image_block 按扩展名推 mime，PNG 字节塞进 .vision.jpg 会被标成 image/jpeg）
print("① 准备测试用 JPEG")
try:
    from PIL import Image
except ImportError:
    print("  ❌ 当前解释器没有 Pillow。服务用的 .venv 本来就没装，请改用 ComfyUI 的解释器：")
    print("     /e/ComfyUI/.venv/Scripts/python.exe tests/e2e_vision_live.py")
    sys.exit(2)

os.makedirs(JPEG_DIR, exist_ok=True)
pngs = sorted((f for f in os.listdir(IMAGE_DIR) if f.endswith(".png")),
              key=lambda f: os.path.getmtime(os.path.join(IMAGE_DIR, f)), reverse=True)[:4]
if len(pngs) < 2:
    print(f"  ❌ images/ 里只有 {len(pngs)} 张 PNG，至少需要 2 张")
    sys.exit(2)

jpgs = []
for p in pngs:
    im = Image.open(os.path.join(IMAGE_DIR, p)).convert("RGB")
    im.thumbnail((1024, 1024))
    out = os.path.join(JPEG_DIR, os.path.splitext(p)[0] + ".jpg")
    im.save(out, "JPEG", quality=85)
    jpgs.append((p, out))
print(f"  {len(jpgs)} 张：{', '.join(os.path.basename(j) for _, j in jpgs)}")


def payload(names_paths, note):
    items = []
    for orig, jp in names_paths:
        with open(jp, "rb") as f:
            items.append({"name": orig,
                          "data": "data:image/jpeg;base64," + base64.b64encode(f.read()).decode()})
    return items


def stream(body, label):
    """发一次 /api/chat，返回 (拼接文本, 事件类型集合)。"""
    print(f"\n===== {label} =====")
    text, types, done = "", set(), {}
    with requests.post(f"{BASE}/api/chat", json=body, stream=True, timeout=300) as r:
        if r.status_code != 200:
            print("  HTTP", r.status_code, r.text[:300])
            return None, set(), {}
        for line in r.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data: "):
                continue
            ev = json.loads(line[6:])
            types.add(ev.get("type"))
            if ev.get("type") == "delta":
                text += ev.get("content", "")      # ⚠️ 字段是 content 不是 text
            elif ev.get("type") == "done":
                done = ev
            elif ev.get("type") in ("warning", "error"):
                print(f"  {'⚠️' if ev['type'] == 'warning' else '❌'} {ev.get('error')}")
    print(f"  事件 {sorted(t for t in types if t)} | done.prompt={done.get('prompt','')[:60]!r}")
    print("  回复:", text.strip()[:500].replace("\n", " "), "…" if len(text) > 500 else "")
    return text, types, done


sid = None
try:
    sid = requests.post(f"{BASE}/api/session/new", timeout=10).json()["session_id"]
    print(f"\n会话 {sid}（跑完会删掉，不会留在你的历史记录里）")

    # ── ② 回传图片 + 自定义关注点 → 只分析，不出提示词
    t1, ty1, d1 = stream(
        {"session_id": sid, "message": "看看这张图，视角和人物神态怎么样",
         "images": payload(jpgs[:1], None)},
        "② 回传图片（应只分析、不出 <prompt>）")
    chk("② 无 error/warning", "error" not in ty1 and "warning" not in ty1)
    chk("② 回复不含 <prompt>", "<prompt>" not in t1)
    chk("② done.prompt 为空", not d1.get("prompt"))
    chk("② 回复有实质内容（>80 字）", len(t1.strip()) > 80, f"{len(t1.strip())} 字")
    hit = [k for k in ("视角", "神态", "镜头", "构图", "景别", "服装", "配色", "背景", "光线") if k in t1]
    chk("② 谈到构图要素", len(hit) >= 2, "命中: " + "、".join(hit))

    # ── ③ 明确要求修改 → 这时才该出提示词
    t2, ty2, d2 = stream({"session_id": sid, "message": "把背景改成夜晚"},
                         "③ 明确要求修改（这时才该出 <prompt>）")
    chk("③ 输出了 <prompt>", "<prompt>" in t2)
    chk("③ done.prompt 非空", bool(d2.get("prompt")))

    # ── ④ 多图对比（生成多张时全部回传）
    if len(jpgs) >= 2:
        t3, ty3, d3 = stream(
            {"session_id": sid, "message": "对比这些图：视角、神态、配色各有什么差别？",
             "images": payload(jpgs, None)},
            f"④ 一次回传 {len(jpgs)} 张（对比生成）")
        chk("④ 无 error/warning", "error" not in ty3 and "warning" not in ty3)
        chk("④ 不含 <prompt>", "<prompt>" not in t3)
        chk("④ 回复有实质内容（>150 字）", len(t3.strip()) > 150, f"{len(t3.strip())} 字")
        chk("④ 每张都落了 .vision.jpg 副本",
            all(os.path.exists(os.path.join(IMAGE_DIR, os.path.splitext(o)[0] + ".vision.jpg"))
                for o, _ in jpgs))

    # ── ⑤ 图库不漏副本
    names = [i["name"] for i in requests.get(f"{BASE}/api/images", timeout=10).json()["images"]]
    chk("⑤ 图库列表已过滤 .vision.jpg", not [n for n in names if n.endswith(".vision.jpg")])
    vname = os.path.splitext(jpgs[0][0])[0] + ".vision.jpg"
    chk("⑤ 副本本身仍可直链访问（聊天缩略图依赖它）",
        requests.get(f"{BASE}/images/{vname}", timeout=10).status_code == 200)
finally:
    # 删掉本次测试建的会话 —— 别把垃圾留在用户的对话列表里
    if sid:
        try:
            requests.delete(f"{BASE}/api/session/{sid}", timeout=10)
            print(f"（已删除测试会话 {sid}）")
        except Exception as e:
            print(f"⚠️ 测试会话 {sid} 没删掉：{e}")
    # 顺手清掉本次测出来的 .vision.jpg 副本（纯缓存，用户真要回传时会按需重建）
    for orig, _ in jpgs:
        v = os.path.join(IMAGE_DIR, os.path.splitext(orig)[0] + ".vision.jpg")
        try:
            os.remove(v)
        except OSError:
            pass
    for _, jp in jpgs:
        try:
            os.remove(jp)
        except OSError:
            pass
    try:
        os.rmdir(JPEG_DIR)
    except OSError:
        pass

print("\n" + ("全部通过 ✅" if not fails else f"失败 ❌ {fails}"))
sys.exit(1 if fails else 0)
