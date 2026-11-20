#!/usr/bin/env python
"""免费 Google 翻译通道在线探针。

用途有两个：

1. **部署前置验证**：确认当前出口 + tk 算法真能拿到译文（单测只证明逻辑对，
   不证明服务端接受我们的签名）。
2. **线上巡检**：Google 已从首页移除 ``tkk`` 变量（2026-11-19 实测），
   :data:`DEFAULT_TKK` 是唯一来源。一旦 Google 轮换 TKK，所有请求会 403，
   本脚本是唯一能提前发现该故障的手段。建议 cron 每 10 分钟跑一次，
   非 0 退出码即告警。

用法::

    python scripts/verify_live.py                          # 走环境变量代理
    python scripts/verify_live.py --proxy http://127.0.0.1:10809
    python scripts/verify_live.py --proxy socks5://127.0.0.1:10808  # 需 httpx[socks]

退出码：0 全部通过；1 有失败项。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

# 允许直接从 scripts/ 目录执行（免设 PYTHONPATH）
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from imgtrans.translator import GoogleWebTranslator

#: 探针用例：(源文本, 目标语言, 期望出现在译文里的子串)。
#: 期望串只用于判定"服务端真的翻译了"而非原样返回，不追求精确匹配。
PROBES: tuple[tuple[str, str, str], ...] = (
    ("hello", "zh-CN", "你"),
    ("你好", "en", "hello"),
    # emoji：PHP 版 tk 在此算错必 403，本项失败即说明 tk 实现退化
    ("😀emoji🎉", "zh-CN", "表情"),
)


def _run(translator: GoogleWebTranslator) -> list[str]:
    failures: list[str] = []

    for text, target, expect in PROBES:
        started = time.perf_counter()
        out = translator.translate_batch([text], None, target)
        cost = (time.perf_counter() - started) * 1000
        got = out[0] if out else ""
        ok = got != text and expect.lower() in got.lower()
        mark = "PASS" if ok else "FAIL"
        print(f"  [{mark}] {text!r} -> {target}  {got!r}  ({cost:.0f} ms)")
        if not ok:
            failures.append(f"translate({text!r} -> {target}) returned {got!r}")

    # 批量：验证一次请求多块时 tk 按拼接整串计算、响应顺序不错位
    texts = ["Login", "Register", "Forgot password"]
    out = translator.translate_batch(texts, None, "zh-CN")
    ordered = len(out) == len(texts) and all(o != s for o, s in zip(out, texts))
    mark = "PASS" if ordered else "FAIL"
    print(f"  [{mark}] batch {texts} -> {out}")
    if not ordered:
        failures.append(f"batch order/coverage broken: {out}")

    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description="免费 Google 翻译通道在线探针")
    parser.add_argument("--proxy", default=None, help="代理地址，默认读环境变量")
    parser.add_argument("--timeout", type=float, default=15.0, help="请求超时秒数")
    args = parser.parse_args()

    translator = GoogleWebTranslator(proxy=args.proxy, timeout=args.timeout)
    print(f"probe start (proxy={args.proxy or 'env'}, circuit={translator.circuit_open})")

    failures = _run(translator)

    if translator.circuit_open:
        failures.append("circuit breaker open — 连续 403/429 已触发熔断")

    if failures:
        print("\nRESULT: FAIL")
        for item in failures:
            print(f"  - {item}")
        return 1

    print("\nRESULT: OK — 免费通道可用，tk 签名被服务端接受")
    return 0


if __name__ == "__main__":
    sys.exit(main())
