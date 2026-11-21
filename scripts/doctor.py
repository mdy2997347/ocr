#!/usr/bin/env python
"""部署自检：在目标机上跑一遍，确认这套服务能不能起来。

为什么需要它
------------
图片翻译链路的依赖比普通 Web 服务杂：PaddleOCR 要模型、Pillow 要复杂文字
整形库、回填要**按语言**找对字体。缺任何一样都不会报"缺依赖"，而是安静地
降级——字体找不到就退化成 10px 位图字体，raqm 缺失就让阿拉伯字母各自孤立。
这类问题在开发机上永远看不到，一到 Linux 容器里就全是。

用法::

    python scripts/doctor.py            # 人读
    python scripts/doctor.py --json     # 给 CI / 监控用

退出码：有 ``FAIL`` 项返回 1，只有 ``WARN`` 返回 0。
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import tempfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from imgtrans.env import load_env  # noqa: E402

OK, WARN, FAIL = "ok", "warn", "fail"

#: 自检覆盖的语言：每种文字系统各取一个代表，加上中/日/韩三套 CJK 字形。
_PROBE_LANGS = ("zh", "ja", "ko", "en", "ar", "th", "hi", "bn", "ka", "he", "ru")

#: 这些文字必须有 raqm 才能正确整形（否则字母孤立、辅音簇位置错）。
_NEEDS_RAQM = {"ar", "hi", "bn", "th"}


class Report:
    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []

    def add(self, level: str, name: str, detail: str) -> None:
        self.items.append({"level": level, "item": name, "detail": detail})

    def ok(self, name: str, detail: str = "") -> None:
        self.add(OK, name, detail)

    def warn(self, name: str, detail: str) -> None:
        self.add(WARN, name, detail)

    def fail(self, name: str, detail: str) -> None:
        self.add(FAIL, name, detail)

    @property
    def worst(self) -> str:
        if any(i["level"] == FAIL for i in self.items):
            return FAIL
        if any(i["level"] == WARN for i in self.items):
            return WARN
        return OK


def check_platform(r: Report) -> None:
    r.ok("运行环境", f"Python {platform.python_version()} / {platform.system()} {platform.machine()}")


def check_packages(r: Report) -> None:
    """依赖是否装齐、版本是否对得上。"""
    from importlib import metadata

    for dist, minv in (
        ("pillow", None),
        ("numpy", None),
        ("opencv-python-headless", None),
        ("paddleocr", None),
        ("paddlepaddle", None),
        ("fastapi", None),
        ("httpx", None),
    ):
        try:
            version = metadata.version(dist)
        except metadata.PackageNotFoundError:
            # paddlepaddle 与 paddlepaddle-gpu 是两个包名，任一存在即可
            if dist == "paddlepaddle":
                try:
                    version = "paddlepaddle-gpu " + metadata.version("paddlepaddle-gpu")
                    r.ok(f"依赖 {dist}", version)
                    continue
                except metadata.PackageNotFoundError:
                    pass
            r.fail(f"依赖 {dist}", "未安装")
            continue
        r.ok(f"依赖 {dist}", version)


def check_pillow(r: Report) -> None:
    """raqm 决定复杂文字能不能正确整形。"""
    try:
        from PIL import Image, features
    except ImportError:
        r.fail("Pillow", "无法 import，后续字体检查跳过")
        return

    r.ok("Pillow", getattr(Image, "__version__", "?"))

    try:
        has_raqm = bool(features.check("raqm"))
    except Exception as exc:  # noqa: BLE001
        r.warn("Pillow raqm", f"检测失败：{exc}")
        return

    if has_raqm:
        r.ok("Pillow raqm", "已启用（孟加拉/天城/泰/阿拉伯/希伯来按 HarfBuzz 规则整形）")
    else:
        # 依赖形态在不同 wheel 上并不一致：有的把 raqm 静态编进去（只差
        # 运行时的 FriBiDi），有的是动态链接 libraqm（本机实测：Pillow 13
        # 的 win_amd64 wheel 导入表里同时有 libraqm 与 fribidi）。所以
        # 两个库都建议装上，别只装一个然后困惑"为什么不绿"。
        try:
            from imgtrans.render.fonts import needs_shaping
            from imgtrans.translator.lang_map import SUPPORTED_LANGS

            affected = [c for c in SUPPORTED_LANGS if needs_shaping(c)]
        except Exception:  # noqa: BLE001
            affected = ["bn", "hi", "th", "ar", "he"]
        r.warn(
            "Pillow raqm",
            f"未启用：{', '.join(affected)} 共 {len(affected)} 个语言会排错"
            "（元音符号位置、辅音连字、RTL 顺序），但图上仍有字形，肉眼不易发现。"
            "Linux: `apt-get install -y libraqm0 libfribidi0`；"
            "Windows: 把 libraqm.dll 与 fribidi-0.dll 放到 python.exe 同目录。"
            "装完必须重启服务（字体与整形能力只在启动时探测）。",
        )


def check_fonts(r: Report) -> None:
    """字体是 Linux 部署最容易翻车的一环：找不到不会报错，只会画成豆腐块。"""
    try:
        from imgtrans.render import fonts as F
    except Exception as exc:  # noqa: BLE001
        r.fail("字体模块", f"import 失败：{exc}")
        return

    book = F.FontBook(F.font_dirs_from_env())
    total = len(book.index)
    dirs = [d for d in F._SYSTEM_FONT_DIRS if Path(d).is_dir()]
    extra = F.font_dirs_from_env()

    if total == 0:
        r.fail(
            "字体索引",
            "一个字体都没找到。Linux 请安装 fonts-noto-cjk（中文）+ fonts-dejavu-core（拉丁），"
            "或用 IMGTRANS_FONT_DIRS 指定字体目录",
        )
        return
    r.ok("字体索引", f"{total} 个文件（系统目录 {len(dirs)} 个可用，额外目录 {len(extra)} 个）")

    for lang in _PROBE_LANGS:
        path = book.find(lang)
        if path is None:
            r.fail(f"字体 {lang}", "无可用字体")
            continue

        family = F.family_for_lang(lang)
        probe = F.FONT_PROBE_TEXT.get(family, "")
        # 判据从"文件名对不对"改成"这些字到底画不画得出来"：
        # 文件名猜不出覆盖。ka 就是活例子——文件名叫 DejaVuSans 也算"专用"，
        # 但如果那台机器装的是个没格鲁吉亚字形的版本，照样一片方框。
        #
        # 这里必须用 uncovered_chars 而不是 missing_chars：渲染会按字符回退到
        # 别的族，只要**某种组合**能画出来就不算缺字。用 missing_chars 会把
        # "本族缺、但 latin 能补"的情况误判成 FAIL。
        resolved = book.resolve_family(lang, probe) if probe else family
        missing = book.uncovered_chars(lang, probe) if probe else ""

        if missing:
            r.fail(
                f"字体 {lang}",
                f"缺字形：{missing} 画不出来 → 会变成方框(□)。"
                "Linux 装 fonts-noto-core（含格鲁吉亚/泰/天城/孟加拉）或对应语种字体",
            )
        elif resolved != family:
            r.warn(
                f"字体 {lang}",
                f"本族（{family}）字体缺失，改用 {resolved} 绘制（能画出来，字形风格可能不一致）",
            )
        else:
            r.ok(f"字体 {lang}", f"{path.name}（{family}）")


def check_device(r: Report) -> None:
    from imgtrans.device import DeviceError, resolve_device

    raw = os.environ.get("IMGTRANS_DEVICE", "(未设置，默认 cpu)")
    try:
        device = resolve_device()
    except DeviceError as exc:
        r.fail("推理设备", f"配置值 {raw} 不可用：{exc}")
        return
    r.ok("推理设备", f"{device}（配置值 {raw}）")


def check_config(r: Report) -> None:
    """配置项只报"有没有"，**绝不打印密钥本身**。"""
    env_path = Path(__file__).resolve().parents[1] / ".env"
    if env_path.is_file():
        r.ok(".env", f"已找到 {env_path}")
    else:
        r.warn(".env", "未找到，将只用环境变量（生产建议用 .env 或 systemd Environment=）")

    if os.environ.get("IMGTRANS_API_KEY"):
        r.ok("API Key", "已配置")
    else:
        r.warn("API Key", "未配置：serve.py 会拒绝启动（除 --dev）")

    proxy = os.environ.get("IMGTRANS_PROXY") or ""
    if proxy:
        # 代理是最容易从开发机照抄、然后在 Linux 上不通的一项
        r.ok("出网代理", proxy)
    else:
        r.warn(
            "出网代理",
            "未配置。若目标机不能直连 translate.googleapis.com，翻译会全部失败",
        )

    host = os.environ.get("IMGTRANS_HOST", "127.0.0.1")
    if host in ("127.0.0.1", "localhost"):
        r.warn(
            "监听地址",
            f"{host}：容器内或需要外部访问时必须改成 0.0.0.0",
        )
    else:
        r.ok("监听地址", host)


def check_writable(r: Report) -> None:
    """输出目录可写：容器里以非 root 运行、只读挂载都会在这里暴露。"""
    try:
        with tempfile.NamedTemporaryFile(
            prefix="imgtrans_doctor_", suffix=".tmp", delete=True
        ) as fh:
            fh.write(b"ok")
    except OSError as exc:
        r.fail("临时目录可写", str(exc))
    else:
        r.ok("临时目录可写", tempfile.gettempdir())

    out = Path(__file__).resolve().parents[1] / "out"
    try:
        out.mkdir(exist_ok=True)
        probe = out / ".doctor_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        r.warn("输出目录 out/", f"不可写：{exc}（命令行 --out 可以指到别处）")
    else:
        r.ok("输出目录 out/", str(out))


def main() -> int:
    parser = argparse.ArgumentParser(description="imgtrans 部署自检")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args()

    load_env()

    report = Report()
    for check in (
        check_platform,
        check_packages,
        check_pillow,
        check_fonts,
        check_device,
        check_config,
        check_writable,
    ):
        try:
            check(report)
        except Exception as exc:  # noqa: BLE001 - 单项崩溃不该让整个自检失败
            report.fail(check.__name__, f"检查本身出错：{exc}")

    if args.json:
        print(json.dumps({"status": report.worst, "checks": report.items}, ensure_ascii=False, indent=2))
    else:
        marks = {OK: "[ OK ]", WARN: "[WARN]", FAIL: "[FAIL]"}
        for item in report.items:
            print(f"{marks[item['level']]} {item['item']}: {item['detail']}")
        print()
        summary = {OK: "全部通过", WARN: "有告警，能跑但可能有问题", FAIL: "有阻塞项，先修再上线"}
        print(f"结论：{summary[report.worst]}")

    return 1 if report.worst == FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
