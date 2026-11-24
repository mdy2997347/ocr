#!/usr/bin/env python
"""启动 HTTP 服务。

用法::

    # 最小启动：自动读取项目根目录的 .env（API Key / 代理 / 端口）
    python scripts/serve.py

    # 覆盖 .env 里的值
    python scripts/serve.py --port 8000 --proxy http://127.0.0.1:10809

    # 指定代理 + 鉴权
    python scripts/serve.py --port 8000 --proxy http://127.0.0.1:10809 --api-key my-secret

    # 多 worker（注意：每个进程都会各自加载一份 OCR 模型，显存/内存要算够）
    python scripts/serve.py --port 8000 --workers 2

设计说明：

* **模型在启动时预热**。OCR 首次加载要 6~8 秒（CPU），放在第一个请求里
  会直接把 P95 顶穿。``--no-warmup`` 可以关掉，用于快速验证接口是否通。
* 启动时先加载项目根目录的 ``.env``（见 :mod:`imgtrans.env`），再读命令行。
  优先级：**命令行 > 环境变量 > .env**，线上临时改值不用动文件。
* ``--api-key`` 不传时读环境变量 ``IMGTRANS_API_KEY``；两者都没有时
  **拒绝启动**，除非显式 ``--dev``。避免默认裸奔上线。
* 单 worker 就够：OCR 推理本身是 CPU/GPU 密集，多 worker 只会抢同一块卡。
  真要提吞吐，靠多实例 + 前置负载均衡。
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from imgtrans.env import apply_model_dir, load_env  # noqa: E402 - 必须在 sys.path 修正之后


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # 先加载 .env，再建 parser：argparse 的 default 是构建时求值的，
    # 顺序反了就拿不到 .env 里的值（这是很容易踩的一脚）。
    load_env()
    # 模型缓存目录也在这里落地：paddlex 在 import 时就定死路径，晚于此无效
    apply_model_dir()

    parser = argparse.ArgumentParser(description="imgtrans HTTP 服务")
    parser.add_argument(
        "--host", default=os.environ.get("IMGTRANS_HOST", "127.0.0.1"), help="监听地址"
    )
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("IMGTRANS_PORT", "8000")), help="监听端口"
    )
    parser.add_argument(
        "--proxy",
        default=os.environ.get("IMGTRANS_PROXY") or None,
        help="访问 Google / 图片下载用的代理（默认取 IMGTRANS_PROXY）",
    )
    parser.add_argument(
        "--api-key", default=None, help="X-Api-Key 校验值（默认取 IMGTRANS_API_KEY）"
    )
    parser.add_argument(
        "--translator",
        default=os.environ.get("IMGTRANS_TRANSLATOR") or "web",
        help=(
            "翻译通道（默认取 IMGTRANS_TRANSLATOR）："
            "web/api/free=免费 web 接口（机房 IP 会被 429 限流）；"
            "cloud/nmt=官方 Cloud Translation v2（需 API Key，稳定）；"
            "baidu=百度通用文本翻译（需 IMGTRANS_BAIDU_APPID + IMGTRANS_BAIDU_SECRET）"
        ),
    )
    parser.add_argument(
        "--device",
        default=os.environ.get("IMGTRANS_DEVICE") or "cpu",
        help="推理设备：cpu（默认）/ gpu / gpu:0 / auto（有卡就用卡，无卡回落 cpu）",
    )
    parser.add_argument("--dev", action="store_true", help="开发模式：不校验 API Key")
    parser.add_argument(
        "--ocr-profile",
        default=os.environ.get("IMGTRANS_OCR_PROFILE") or "quality",
        help=(
            "OCR 速度档位（默认取 IMGTRANS_OCR_PROFILE）："
            "quality=检测与识别都用 PP-OCRv6 medium（默认，最准）；"
            "fast=检测换 PP-OCRv5 mobile，CPU 上约快一倍，识别精度不变"
        ),
    )
    parser.add_argument(
        "--ocr-det-side-len",
        type=int,
        default=int(os.environ.get("IMGTRANS_OCR_DET_SIDE_LEN") or 0) or None,
        help="检测阶段长边上限（像素）。默认用 PaddleOCR 的 736；调小更快但小字会漏",
    )
    parser.add_argument("--workers", type=int, default=1, help="worker 进程数（每个都会加载一份模型）")
    parser.add_argument("--no-warmup", action="store_true", help="跳过模型预热")
    parser.add_argument(
        "--output-format",
        default="jpeg",
        choices=["png", "jpeg", "webp"],
        help="输出格式。默认 jpeg：q95+4:4:4 体积与源图持平，文字边缘不退化；"
        "png 无损但照片型图片会大 2~3 倍",
    )
    parser.add_argument(
        "--output-quality",
        type=int,
        default=95,
        help="jpeg/webp 质量（1-100）。95 以下体积明显变小，低于 85 文字边缘开始发虚",
    )
    parser.add_argument(
        "--row-join-gap",
        type=float,
        default=0.45,
        help="行内碎片拼接的水平间隙上限（相对行高）。0 关闭；参数表密集的图可调到 0.2",
    )
    parser.add_argument(
        "--no-erase-residuals",
        action="store_true",
        default=(os.environ.get("IMGTRANS_ERASE_RESIDUALS", "").strip().lower()
                 in ("0", "false", "no", "off")),
        help="关闭擦除后的残墨复检（默认开启；出问题时用来对照）",
    )
    args = parser.parse_args()

    if args.dev:
        os.environ["IMGTRANS_DEV"] = "1"
    elif args.api_key:
        os.environ["IMGTRANS_API_KEY"] = args.api_key
    elif not os.environ.get("IMGTRANS_API_KEY"):
        print(
            "拒绝启动：没有配置 API Key。\n"
            "  生产：--api-key <secret> 或环境变量 IMGTRANS_API_KEY\n"
            "  开发：--dev（不校验）",
            file=sys.stderr,
        )
        return 2

    from imgtrans.device import DeviceError, resolve_device

    # 设备在这里就解析并校验完，别等到 warmup：配了 gpu 但机器上是 CPU 版
    # paddle 时，应该在打印"监听中"之前就退出，而不是先起服务再炸。
    # 这一步只读配置、不加载模型（cpu 分支不会 import paddle），
    # 所以放在 fastapi/uvicorn 之前——配错时能立刻退出，不用等框架导入。
    try:
        device = resolve_device(args.device)
    except DeviceError as exc:
        print(f"拒绝启动：{exc}", file=sys.stderr)
        return 2

    import uvicorn

    from imgtrans.api.app import create_app
    from imgtrans.pipeline import ImageTranslatePipeline, PipelineConfig
    from imgtrans.translator import build_translator, mask_proxy
    from imgtrans.translator.lang_map import SUPPORTED_LANGS

    logging.info("推理设备 %s（配置值：%s）", device, args.device)

    # 翻译通道在这里就建出来，而不是等 factory()：
    #   * 通道名写错（比如 IMGTRANS_TRANSLATOR=googel）要在加载模型之前退出；
    #   * 启动日志能如实打出"到底走哪条通道"——线上排查 429 时，
    #     第一个要看的就是这句，而不是翻代码猜默认值。
    google_key = os.environ.get("IMGTRANS_GOOGLE_KEY")
    google_credentials = os.environ.get("IMGTRANS_GOOGLE_CREDENTIALS")
    google_project = os.environ.get("IMGTRANS_GOOGLE_PROJECT")
    baidu_appid = os.environ.get("IMGTRANS_BAIDU_APPID")
    baidu_secret = os.environ.get("IMGTRANS_BAIDU_SECRET")
    # 百度**不继承** IMGTRANS_PROXY：那个变量通常是给 Google 配的海外代理，
    # 百度走海外出口反而会被判定为境外 IP（58000 客户端 IP 非法），
    # 或者凭空多绕一圈。要代理请单独设 IMGTRANS_BAIDU_PROXY。
    channel_proxy = args.proxy
    if (args.translator or "").strip().lower() in ("baidu", "baidu_api"):
        channel_proxy = os.environ.get("IMGTRANS_BAIDU_PROXY") or None
    # 标准版 1 QPS，高级版 10 QPS。写错值不该让服务起不来——退到 1 更安全，
    # 但要提示，否则"我明明写了 10 却没提速"会变成一次无效排查。
    raw_qps = (os.environ.get("IMGTRANS_BAIDU_QPS") or "").strip()
    try:
        baidu_qps = float(raw_qps) if raw_qps else 1.0
    except ValueError:
        print(
            f"提示：IMGTRANS_BAIDU_QPS={raw_qps!r} 不是数字，按 1.0 处理",
            file=sys.stderr,
        )
        baidu_qps = 1.0
    # 账号档位：标准版/高级版只认 28 个常见语种，非常用语种（tr/id/hi/ka/uk…）
    # 要企业尊享版才可用。默认按标准版口径前置拒绝，报错就能说清原因；
    # 升级后设 IMGTRANS_BAIDU_PREMIUM=1 放开（否则等于白买了还翻不了）。
    baidu_premium = (os.environ.get("IMGTRANS_BAIDU_PREMIUM") or "").strip().lower()
    baidu_premium = baidu_premium in ("1", "true", "yes", "on")
    try:
        translator = build_translator(
            args.translator,
            proxy=channel_proxy,
            timeout=20,
            google_key=google_key,
            google_credentials=google_credentials,
            google_project=google_project,
            baidu_appid=baidu_appid,
            baidu_secret=baidu_secret,
            baidu_qps=baidu_qps,
            baidu_premium=baidu_premium,
        )
    except ValueError as exc:
        print(f"拒绝启动：{exc}", file=sys.stderr)
        return 2

    # 档位名在这里就校验：写在 .env 里的拼写错误（qualityy / FAST 之外的
    # 乱写）应该在加载模型之前退出，而不是等服务起来、首个请求才炸。
    from imgtrans.ocr.engine import OCR_PROFILES
    from imgtrans.render.fonts import has_raqm as font_has_raqm
    from imgtrans.render.fonts import needs_shaping

    ocr_profile = (args.ocr_profile or "quality").strip().lower()
    if ocr_profile not in OCR_PROFILES:
        print(
            f"拒绝启动：未知的 OCR 档位 {args.ocr_profile!r}；"
            f"可选：{', '.join(sorted(OCR_PROFILES))}",
            file=sys.stderr,
        )
        return 2

    if getattr(translator, "kind", "") == "cloud" and not (
        google_key or google_credentials
    ):
        logging.warning(
            "cloud 通道未配置 IMGTRANS_GOOGLE_KEY / IMGTRANS_GOOGLE_CREDENTIALS，"
            "将回落到 Google 的 ADC 默认凭证（要求机器上已 gcloud auth）"
        )

    # 设备确认之后才报监听地址：日志顺序就是启动顺序，
    # 先喊"监听中"再因为设备配错退出，会让人以为服务起过又挂了。
    logging.info(
        "监听 %s:%s，翻译通道 %s%s，OCR 档位 %s，输出 %s q%d，翻译代理 %s，图片下载代理 %s，鉴权 %s",
        args.host,
        args.port,
        getattr(translator, "kind", args.translator),
        "（企业尊享版）" if baidu_premium else "",
        ocr_profile,
        # 报实际的格式与质量：体积是线上最容易被投诉的指标，日志里没有
        # 这一项就只能靠猜服务到底跑的是 png 还是 jpeg。
        args.output_format,
        args.output_quality,
        # 用翻译器实际生效的代理，而不是配置值：两者不同时（比如自定义
        # transport、或通道不认这个变量）日志会骗人。同时抹掉账号密码。
        getattr(translator, "proxy", None) or "无",
        mask_proxy(args.proxy) or "无",
        "关闭（--dev）" if args.dev else "开启",
    )

    def factory():
        pipeline = ImageTranslatePipeline(
            translator,
            config=PipelineConfig(
                output_format=args.output_format,
                output_quality=args.output_quality,
                device=device,
                ocr_profile=ocr_profile,
                ocr_det_side_len=args.ocr_det_side_len,
                row_join_gap_ratio=args.row_join_gap,
                erase_residuals=not args.no_erase_residuals,
            ),
        )
        # 预热交给 app 的 startup 钩子统一做：两处都调的话 --no-warmup
        # 只跳过了这里那次，startup 里还会照热一遍（真踩过）。
        # proxy 显式传下去：/v1/translate/img 传 URL 时要用它下载图片

        # 字体自检：语言码能映射到字体族，不代表本机真有能画出这些文字的
        # 字体。缺字体时 Pillow 不报错、不抛异常，只把整段文字画成一排 □
        # （ka 就是这样：只装了中文字体的机器上，格鲁吉亚文全是方框）。
        # 放在启动阶段说清楚，好过等用户传图过来才发现。
        problems = pipeline.font_book.check_langs(SUPPORTED_LANGS)
        if problems:
            logging.warning(
                "字体自检：%d/%d 个目标语言在本机画不出来（会渲染成方框）：",
                len(problems),
                len(SUPPORTED_LANGS),
            )
            for line in problems:
                logging.warning("  %s", line)
        else:
            logging.info("字体自检：%d 个目标语言全部有字体覆盖", len(SUPPORTED_LANGS))

        # 整形自检与字体自检是两件事，缺一不可：字体装齐了只保证"有字"，
        # 孟加拉/天城/泰/阿拉伯/希伯来还要 Raqm 才能把字排对（元音重排、
        # 辅音连字、RTL 顺序）。缺 Raqm 时 Pillow 只发一条 Warning 就退回
        # basic layout，图上照样有字——不主动查就会一直错下去。
        if not font_has_raqm():
            shaped = [c for c in SUPPORTED_LANGS if needs_shaping(c)]
            logging.warning(
                "整形自检：Pillow 没有 Raqm（缺 libraqm/FriBiDi），%d 个语言会排错：%s",
                len(shaped),
                ", ".join(shaped),
            )
            logging.warning(
                "  这些语言的元音符号位置、辅音连字、RTL 顺序都会不正确，"
                "但图上仍有字形（不是方框）。"
            )
            logging.warning(
                "  修复：apt-get install -y libraqm0 libfribidi0 后重启；"
                "Windows 需把 libraqm.dll 与 fribidi-0.dll 放到 python.exe 同目录。"
            )
        else:
            logging.info("整形自检：Raqm 可用，复杂文字按 HarfBuzz 规则排版")

        return create_app(pipeline, proxy=args.proxy, warmup=not args.no_warmup)

    uvicorn.run(
        factory(),
        host=args.host,
        port=args.port,
        # 传的是 app 实例（pipeline 需要在这里装配），uvicorn 不允许
        # 实例 + 多 worker 组合，所以这里始终单进程。
        # 要提高吞吐请起多个实例，前面挂负载均衡——OCR 本身就是独占算力的。
        workers=1,
        log_level="info",
    )
    if args.workers > 1:
        print(
            f"提示：已忽略 --workers {args.workers}。每个进程都会加载一份 OCR 模型，"
            "多进程只会抢同一块 CPU/GPU；请改为起多个实例。",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
