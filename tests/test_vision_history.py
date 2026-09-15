# -*- coding: utf-8 -*-
"""图片回传（视觉反馈）回归测试。

跑法（⚠️ 中文必须写成 UTF-8 脚本文件再跑，别用 shell 传参 —— 会乱码）：
    cd /e/anima-studio && ./.venv/Scripts/python.exe tests/test_vision_history.py
"""
import base64
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app  # noqa: E402

fails = []
TMP = []            # 测试产生的文件，跑完删掉


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}" + (f"  {detail}" if detail else ""))
    if not cond:
        fails.append(label)


def png_data_url():
    """1x1 透明 PNG，够 _save_vision_images 解码落盘用。"""
    raw = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
    return "data:image/png;base64," + base64.b64encode(raw).decode()


print("① 模型：已显式指向支持视觉的 V4.1 Flash")
check("DEEPSEEK_MODEL == 'deepseek-flash'", app.DEEPSEEK_MODEL == "deepseek-flash", app.DEEPSEEK_MODEL)

print("\n② _save_vision_images：落盘命名从原图主干派生（幂等）")
files, errs = app._save_vision_images([
    {"name": "anima_09999_.png", "data": png_data_url()},
    {"name": "anima_09998_.png", "data": png_data_url()},
])
TMP.extend(files)
check("返回 2 个文件名", len(files) == 2, files)
check("命名为 <主干>.vision.jpg",
      files == ["anima_09999_.vision.jpg", "anima_09998_.vision.jpg"], files)
check("无错误", not errs, errs)
check("文件真的落盘了",
      all(os.path.exists(os.path.join(app.IMAGE_DIR, f)) for f in files))

# 幂等：同一张原图再传一次，不应产生新文件
again, _ = app._save_vision_images([{"name": "anima_09999_.png", "data": png_data_url()}])
check("重传同一张 → 文件名不变（幂等）", again == ["anima_09999_.vision.jpg"], again)

print("\n③ _save_vision_images：坏输入不炸，走 errors")
bad, errs = app._save_vision_images([
    {"name": "a.png", "data": ""},                      # 空 → 静默跳过（前端不会这么发）
    {"name": "b.png", "data": "not-a-data-url"},        # 无逗号
    {"name": "c.png", "data": "data:image/jpeg;base64,%%%invalid%%%"},  # 解不开
])
check("全部跳过", bad == [], bad)
check("空 data 静默跳过、坏格式各报一条", len(errs) == 2, errs)
check("超量图片被截断", len(app._save_vision_images(
    [{"name": f"x{i}.png", "data": png_data_url()} for i in range(10)])[0])
    <= app.VISION_MAX_IMAGES)
for i in range(10):
    TMP.append(f"x{i}.vision.jpg")

print("\n④ _image_block：编码 / 越界防护")
blk = app._image_block(files[0])
check("能编码出 image_url 块", bool(blk) and blk.get("type") == "image_url")
check("是 data URL", (blk or {}).get("image_url", {}).get("url", "").startswith("data:image/"))
check("不存在的文件返回 None", app._image_block("no_such_file.png") is None)
check("空文件名返回 None", app._image_block("") is None)
check("目录穿越受阻（../../app.py）", app._image_block("../../app.py") is None)
check("目录穿越受阻（..\\..\\app.py）", app._image_block("..\\..\\app.py") is None)

print("\n⑤ _build_history：带图消息产出多模态数组")
hist = app._build_history([
    {"role": "user", "content": "分析构图", "images": [files[0]]},
])
content = hist[-1]["content"]
check("content 是数组", isinstance(content, list), type(content).__name__)
check("含 1 个 text + 1 个 image_url",
      len(content) == 2 and content[0]["type"] == "text" and content[1]["type"] == "image_url",
      [b.get("type") for b in content] if isinstance(content, list) else content)
check("text 里带「不要输出 <prompt>」的约束", "不要输出 <prompt>" in content[0]["text"])
check("text 里保留了用户原话", "分析构图" in content[0]["text"])

print("\n⑥ 只保留最近一次回传的图片载荷（更早的降级为文字占位）")
hist = app._build_history([
    {"role": "user", "content": "第一张", "images": [files[0]]},
    {"role": "assistant", "content": "看到了"},
    {"role": "user", "content": "第二张", "images": [files[1]]},
])
first, last = hist[0]["content"], hist[-1]["content"]
check("较早那条是纯文本", isinstance(first, str), type(first).__name__)
check("较早那条含占位标记", "[此前回传过图片：" in first, first[-60:] if isinstance(first, str) else "")
check("较早那条不含 image_url",
      not isinstance(first, list) or all(b.get("type") != "image_url" for b in first))
check("最新那条是数组且带图",
      isinstance(last, list) and any(b.get("type") == "image_url" for b in last))

print("\n⑦ 多图一次全带上（对比生成 4 张）")
four, _ = app._save_vision_images(
    [{"name": f"multi_{i}.png", "data": png_data_url()} for i in range(4)])
TMP.extend(four)
hist = app._build_history([
    {"role": "user", "content": "对比这4张", "images": four},
])
imgs = [b for b in hist[-1]["content"] if b.get("type") == "image_url"]
check("4 张图全部带上", len(imgs) == 4, len(imgs))
check("每张都是 data URL",
      all(b["image_url"]["url"].startswith("data:image/") for b in imgs))

# 同名单重复出现时只编码一次（防重复计费）
hist = app._build_history([
    {"role": "user", "content": "重复传同一张", "images": [four[0], four[0], four[0]]},
])
check("重复文件名去重成 1 张",
      len([b for b in hist[-1]["content"] if b.get("type") == "image_url"]) == 1)

print("\n⑧ 回归：不带图的历史行为完全不变")
hist = app._build_history([
    {"role": "user", "content": "画橘望"},
    {"role": "search", "character": "橘望", "source": "baike", "found": True, "content": "【衣着外貌】…"},
    {"role": "search", "character": "某人", "found": False, "content": ""},
    {"role": "assistant", "content": "<prompt>a</prompt>"},
])
check("普通用户消息仍是字符串", isinstance(hist[0]["content"], str))
check("搜索卡片仍带标记", "[联网搜索到的角色资料]" in hist[1]["content"])
check("未找到资料仍有提示", "但未找到资料" in hist[2]["content"])
check("助手消息原样", hist[3]["content"] == "<prompt>a</prompt>")
check("全程没有数组 content", all(isinstance(m["content"], str) for m in hist))

# 清理
for f in TMP:
    try:
        os.remove(os.path.join(app.IMAGE_DIR, f))
    except OSError:
        pass
print(f"\n(已清理 {len(TMP)} 个测试文件)")
print("全部通过 ✅" if not fails else f"失败 {len(fails)} 项 ❌: {fails}")
sys.exit(1 if fails else 0)
