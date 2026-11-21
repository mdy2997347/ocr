"""命令行入口：便于在部署机器上手工验证翻译通道。

用法示例::

    # 单条 / 多条（命令行参数）
    python -m imgtrans.translator.cli --target zh-CN "Hello" "World"

    # 逐行读取（stdin），输出 JSON 数组
    printf 'Hello\\nWorld\\n' | python -m imgtrans.translator.cli --target ja

    # 指定源语言 / 首选通道 / 关闭限流
    python -m imgtrans.translator.cli --target zh-CN --source en --prefer free --qps 1 "hi"

    # 百度通道（凭证也可用 IMGTRANS_BAIDU_APPID / IMGTRANS_BAIDU_SECRET 环境变量）
    python -m imgtrans.translator.cli --kind baidu --target en \
        --appid 2023xxxxxxxxx --secret xxxxx "你好"

输出默认为 JSON（保持输入顺序），便于脚本消费；``--plain`` 则逐行输出译文。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from typing import Sequence

from . import get_translator

__all__ = ["main", "build_parser"]

logger = logging.getLogger("imgtrans.cli")


def build_parser() -> argparse.ArgumentParser:
    """构造参数解析器。"""
    parser = argparse.ArgumentParser(
        prog="python -m imgtrans.translator.cli",
        description="翻译通道命令行验证工具（imgtrans 翻译层）",
    )
    parser.add_argument(
        "texts",
        nargs="*",
        help="待翻译文本；省略时从 stdin 逐行读取",
    )
    parser.add_argument(
        "--target",
        "-t",
        default="zh-CN",
        help="目标语言代码（默认 zh-CN）；统一用 Google 写法（ja/ko/fr），"
        "百度写法（jp/kor/fra）也接受，内部自动映射",
    )
    parser.add_argument(
        "--source",
        "-s",
        default=None,
        help="源语言代码；省略表示自动检测",
    )
    parser.add_argument(
        "--kind",
        "-k",
        default="web",
        choices=["web", "api", "free", "cloud", "nmt", "baidu", "baidu_api"],
        help="翻译通道（默认 web）",
    )
    parser.add_argument(
        "--appid",
        default=None,
        help="百度通道 APP ID（默认取 IMGTRANS_BAIDU_APPID），纯数字，不是密钥",
    )
    parser.add_argument(
        "--secret",
        default=None,
        help="百度通道密钥（默认取 IMGTRANS_BAIDU_SECRET）",
    )
    parser.add_argument(
        "--premium",
        action="store_true",
        help="百度账号为企业尊享版（默认取 IMGTRANS_BAIDU_PREMIUM=1）。"
        "不开时非常用语种（tr/id/hi/ka/uk/he/hr/sl/ms/no/bn）会在本地被拒",
    )
    parser.add_argument(
        "--prefer",
        default=None,
        choices=["api", "free"],
        help="web 通道的首选子通道",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=8.0,
        help="单次请求超时秒数（默认 8）",
    )
    parser.add_argument(
        "--qps",
        type=float,
        default=4.0,
        help="每秒请求数上限（默认 4，<=0 表示不限流）",
    )
    parser.add_argument(
        "--proxy",
        default=None,
        help="代理地址，例如 http://127.0.0.1:7890",
    )
    parser.add_argument(
        "--plain",
        action="store_true",
        help="逐行输出译文而不是 JSON",
    )
    parser.add_argument(
        "--log-level",
        default="WARNING",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="日志级别（默认 WARNING）",
    )
    return parser


def _read_texts(args: argparse.Namespace) -> list[str]:
    """从命令行参数或 stdin 取待翻译文本。"""
    if args.texts:
        return list(args.texts)
    if sys.stdin is None or sys.stdin.closed:
        return []
    # rstrip 要连 \r 一起去：Windows 上管道/重定向给的是 CRLF，
    # 只去 \n 会让每行译文末尾多一个不可见的 \r（Linux 上是 LF，无此问题）。
    return [line.rstrip("\r\n") for line in sys.stdin]


def main(argv: Sequence[str] | None = None) -> int:
    """CLI 主入口。

    Args:
        argv: 参数列表；``None`` 时取 ``sys.argv[1:]``。

    Returns:
        进程退出码：0 成功，2 参数错误。
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    texts = _read_texts(args)
    if not texts:
        parser.error("no input text: pass positional args or pipe lines via stdin")

    kwargs: dict[str, object] = {"timeout": args.timeout, "qps": args.qps}
    if args.proxy:
        kwargs["proxy"] = args.proxy
    if args.prefer and args.kind in ("web", "api", "free"):
        kwargs["prefer"] = args.prefer
    if args.kind in ("baidu", "baidu_api"):
        # 命令行优先，其次环境变量；都没有就让构造器抛 ValueError，
        # 那里给的提示比"签名错误 54001"有用得多。
        kwargs["appid"] = args.appid or os.environ.get("IMGTRANS_BAIDU_APPID", "")
        kwargs["secret"] = args.secret or os.environ.get("IMGTRANS_BAIDU_SECRET", "")
        premium = args.premium or (
            os.environ.get("IMGTRANS_BAIDU_PREMIUM", "").strip().lower()
            in ("1", "true", "yes", "on")
        )
        kwargs["premium"] = premium

    translator = get_translator(args.kind, **kwargs)
    try:
        results = translator.translate_batch(texts, args.source, args.target)
    except ValueError as exc:
        # 语言方向不受档位支持、语言码不认识——这类是参数问题，不是崩溃，
        # 打一行就够，别让 traceback 把真正的原因淹掉。
        print(f"错误：{exc}", file=sys.stderr)
        return 2

    # 通道整体失败时结果是"原文占位"，直接打印出来看着像翻译成功（只是没变），
    # 拿它验证代理/网络会得出完全相反的结论。所以先看 last_error。
    error = getattr(translator, "last_error", None)
    if error:
        print(f"翻译失败：{error}", file=sys.stderr)
        return 1

    if args.plain:
        for line in results:
            sys.stdout.write(f"{line}\n")
    else:
        sys.stdout.write(
            json.dumps(
                [{"source": src, "target": dst} for src, dst in zip(texts, results)],
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        )
    return 0


if __name__ == "__main__":  # pragma: no cover - 手工执行入口
    raise SystemExit(main())
