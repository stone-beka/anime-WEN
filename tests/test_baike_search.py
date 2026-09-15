# -*- coding: utf-8 -*-
"""百度百科搜索源回归测试。

跑法（⚠️ 中文查询必须写成 UTF-8 脚本文件再跑，别用 shell 传参 —— 会乱码）：
    cd /e/anima-studio && ./.venv/Scripts/python.exe tests/test_baike_search.py
"""
import sys
import os
import time

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app  # noqa: E402

fails = []


def check(label, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}" + (f"  {detail}" if detail else ""))
    if not cond:
        fails.append(label)


print("① 橘望：三段齐全 + 具体外貌")
t0 = time.time()
r = app.search_baike("橘望")
dt = time.time() - t0
check("返回非空", bool(r))
if r:
    txt = r["text"]
    check("来源为 baike", r["source"] == "baike", r["source"])
    check("含【身份背景】", "【身份背景】" in txt)
    check("含【性格特点】", "【性格特点】" in txt)
    check("含【衣着外貌】", "【衣着外貌】" in txt)
    check("含【信息框】", "【信息框】" in txt)
    check("抓到具体外貌细节「浅青柠」", "浅青柠" in txt)
    check("抓到身高", "147" in txt)
    check("含性格关键词「雌小鬼」", "雌小鬼" in txt)
    now = time.time()
    app.search_baike("橘望")
    check("命中缓存（第二次 <0.1s）", time.time() - now < 0.1)
    print(f"  --- 耗时 {dt:.2f}s，正文 {len(txt)} 字 ---")
    print("  " + txt.replace("\n", "\n  ")[:900])

print("\n② 砂狼白子：验证章节名关键词匹配（该词条用「相貌衣着」而非「衣着外貌」）")
r = app.search_baike("砂狼白子")
check("返回非空", bool(r))
if r:
    txt = r["text"]
    check("含【相貌衣着】或【衣着外貌】", ("【相貌衣着】" in txt) or ("【衣着外貌】" in txt))
    check("含【性格特点】", "【性格特点】" in txt)
    check("含【角色背景】或【身份背景】", ("【角色背景】" in txt) or ("【身份背景】" in txt))
    print("  --- 标签:", [l.split("】")[0] + "】" for l in txt.splitlines() if l.startswith("【")])

print("\n③ 不存在的词条：应返回 None 且不抛异常")
try:
    r = app.search_baike("不存在的角色xyzabc123")
    check("返回 None", r is None, repr(r))
except Exception as e:
    check("不抛异常", False, f"{type(e).__name__}: {e}")

print("\n④ 带系列前缀走完整链路（_clean_query 去前缀 → baike 命中）")
r, timed_out = app.search_character_deadline("蔚蓝档案 橘望")
check("未超时", not timed_out)
check("命中 baike", bool(r) and r.get("provider") == "baike", (r or {}).get("provider", "None"))

print("\n⑤ 多义词消歧：百度裸词条「橘光」是南京大屠杀战犯，必须靠作品提示挑对义项")
check("_franchise_hint('蔚蓝档案橘光') == '蔚蓝档案'",
      app._franchise_hint("蔚蓝档案橘光") == "蔚蓝档案", app._franchise_hint("蔚蓝档案橘光"))
check("_franchise_hint('橘光') 为空", app._franchise_hint("橘光") == "")

for label, hint in (("带作品提示", "蔚蓝档案"), ("不带提示（靠已知作品名兜底）", "")):
    r = app.search_baike("橘光", hint)
    check(f"[{label}] 返回非空", bool(r))
    if r:
        txt = r["text"]
        check(f"[{label}] 不是南京大屠杀战犯", "南京大屠杀" not in txt)
        check(f"[{label}] 含【衣着外貌】", "【衣着外貌】" in txt)
        check(f"[{label}] 是蔚蓝档案角色", "蔚蓝档案" in txt)
        print(f"    {label} 标题链: {txt.splitlines()[0][:60]}")

print("\n⑥ 完整链路搜「蔚蓝档案 橘光」（用户报的 bug）")
r, timed_out = app.search_character_deadline("蔚蓝档案 橘光")
check("未超时", not timed_out)
check("拿到结果", bool(r))
if r:
    check("来源为 baike", r.get("provider") == "baike", r.get("provider"))
    check("不是战犯词条", "南京大屠杀" not in r.get("text", ""))
    check("含【衣着外貌】", "【衣着外貌】" in r.get("text", ""))
    print("    来源:", r.get("provider"), "|", r.get("text", "").splitlines()[0][:70])

print("\n⑦ 相关性校验：作品提示对不上时必须拒绝（宁可没资料，也不能给错人的词条）")
r = app.search_baike("砂狼白子", "原神")
check("白子 + 原神提示 → 拒绝并返回 None", r is None, repr(r)[:80])
r = app.search_baike("砂狼白子", "蔚蓝档案")
check("白子 + 蔚蓝档案提示 → 正常返回", bool(r))

print("\n" + ("全部通过 ✅" if not fails else f"失败 {len(fails)} 项 ❌: {fails}"))
sys.exit(1 if fails else 0)
