r"""推理设备解析：``.env`` 里的 ``IMGTRANS_DEVICE`` -> Paddle 认的 device 串。

为什么要有这一层
----------------
PaddleOCR 的 ``device`` 参数只认 ``cpu`` / ``gpu:N`` 两种字面量，而部署时
人手写的配置五花八门（``gpu``、``cuda``、``cuda:0``、``GPU:1``）。
归一化集中在这里，比在每个调用点散着 if 判断靠谱。

为什么默认 ``cpu`` 而不是自动探测
--------------------------------
显式才可预期。自动挑卡最典型的翻车场景：一台被别的训练任务占满显存的
机器上，服务启动时抢不到显存，报一个和配置完全无关的 paddle 内部错误。
想"有卡就用卡"请写 ``auto``——它会探测，并在日志里说清结论。

为什么"配了 gpu 但用不了"要直接失败
----------------------------------
CPU 版 paddle 收到 ``device="gpu:0"`` 会抛一个信息量很低的内部异常，或者
（更糟）在某些版本上静默跑回 CPU。后者让人以为在吃卡，实际慢了十几倍。
这里统一提前抛 :class:`DeviceError`，把"你装的是 CPU 版 paddle"直接写在
错误信息里，并给出 ``auto`` 这条退路。
"""

from __future__ import annotations

import logging
import os
from typing import Callable

from .types import ImageTranslateError

__all__ = [
    "DEFAULT_DEVICE",
    "ENV_VAR",
    "CudaProbe",
    "DeviceError",
    "normalize_device",
    "probe_cuda",
    "resolve_device",
]

logger = logging.getLogger(__name__)

#: 配置项名。默认值刻意是 CPU：生产机上有没有卡、卡是否空闲，
#: 都不该由框架替使用者猜。
ENV_VAR = "IMGTRANS_DEVICE"
DEFAULT_DEVICE = "cpu"

#: ``auto`` 的内部哨兵值——归一化后仍需要知道"要不要探测"，
#: 用字符串 ``"auto"`` 会在后续分支里和用户输入混淆。
_AUTO = "__auto__"

#: 探测函数签名：返回 ``(CUDA 设备数, 说明)``。设备数 <= 0 即不可用。
#: 抽成参数只为了单测能注入假结果，不必真装一块卡。
CudaProbe = Callable[[], tuple[int, str]]


class DeviceError(ImageTranslateError):
    """设备配置错误：取值写错，或配了 gpu 但当前 paddle 用不了卡。

    单独一个错误码，是因为这类问题**必须在启动时就炸**。等到第一个请求
    才在识别阶段抛 paddle 内部异常，排查成本会高得多。
    """

    code = "E_DEVICE"


def normalize_device(raw: str | None) -> str:
    """把各种写法归一成 ``cpu`` / ``gpu:N`` / 内部 ``auto`` 哨兵。

    接受：``cpu``、``gpu``（= ``gpu:0``）、``gpu:1``、``cuda``、``cuda:0``、
    ``auto``；大小写与空格不敏感。空值按 ``cpu`` 处理——``.env`` 里写了
    ``IMGTRANS_DEVICE=`` 这种手滑，不该让服务起不来。

    Raises:
        DeviceError: 无法识别时。**绝不猜**，写错 ``gpu0`` 和写 ``gpu:0``
            的差别会让人查半天。
    """
    text = (raw or "").strip().lower().replace(" ", "")
    if not text or text in ("default", "none"):
        return DEFAULT_DEVICE
    if text in ("auto", "detect"):
        return _AUTO
    if text == "cpu":
        return "cpu"

    head, sep, index = text.partition(":")
    if head in ("gpu", "cuda"):
        if not sep:
            return "gpu:0"
        if index.isdigit():
            return f"gpu:{int(index)}"

    raise DeviceError(
        f"无法识别的推理设备 {raw!r}。可用取值：cpu / gpu / gpu:0 / gpu:1 / auto"
    )


def probe_cuda() -> tuple[int, str]:
    """探测 CUDA 是否可用，返回 ``(设备数, 说明)``。

    ``说明`` 是给人看的：配了 gpu 却跑在 CPU 版 paddle 上，是最常见的
    部署事故，报错里必须直接点明"你装的是 CPU 版"。
    """
    try:
        import paddle
    except ImportError:  # pragma: no cover - 依赖缺失由 OcrEngine 统一报
        return 0, "未安装 paddle"

    try:
        version = getattr(paddle, "__version__", "未知版本")
        if not paddle.device.is_compiled_with_cuda():
            return 0, f"当前 paddle {version} 是 CPU 版（is_compiled_with_cuda() 为 False）"
        count = int(paddle.device.cuda.device_count())
    except Exception as exc:  # noqa: BLE001 - 探测本身失败也只能当不可用
        return 0, f"探测 CUDA 失败：{exc}"

    if count <= 0:
        return 0, "paddle 编译时带了 CUDA，但运行时没检测到可用设备（驱动或容器 GPU 透传问题）"
    return count, f"检测到 {count} 个 CUDA 设备"


def resolve_device(
    raw: str | None = None,
    *,
    probe: CudaProbe | None = None,
    check: bool = True,
) -> str:
    """把配置解析成最终生效的 device 串。

    优先级：**显式参数 > 环境变量 / ``.env`` > 默认 cpu**。``raw`` 传 ``None``
    时才去读环境变量，所以调用方传了值就不会被配置文件悄悄盖掉。

    Args:
        raw: 配置值；``None`` 表示读 ``IMGTRANS_DEVICE``。
        probe: 注入的 CUDA 探测函数，默认 :func:`probe_cuda`。测试用。
        check: 是否校验"配了 gpu 卡是否真的可用"。默认校验；关掉只在
            需要"先解析后验证"的两段式场景用。

    Returns:
        ``cpu`` 或 ``gpu:N``。

    Raises:
        DeviceError: 取值非法，或配了 gpu 但该卡不可用。
    """
    if raw is None:
        raw = os.environ.get(ENV_VAR)

    device = normalize_device(raw)
    if device == DEFAULT_DEVICE:
        return device

    look = probe or probe_cuda

    if device == _AUTO:
        count, reason = look()
        if count > 0:
            logger.info("%s=auto：%s，使用 gpu:0", ENV_VAR, reason)
            return "gpu:0"
        logger.info("%s=auto：%s，回落到 cpu", ENV_VAR, reason)
        return DEFAULT_DEVICE

    if not check:
        return device

    count, reason = look()
    if count <= 0:
        raise DeviceError(
            f"配置的推理设备是 {device}，但{reason}。"
            f"请安装 GPU 版 paddlepaddle-gpu，或把 {ENV_VAR} 改为 cpu"
            f"（想有卡就用卡、没卡自动回落，写 auto）。"
        )

    index = int(device.partition(":")[2])
    if index >= count:
        raise DeviceError(
            f"配置的推理设备是 {device}，但只检测到 {count} 个 CUDA 设备"
            f"（编号 0~{count - 1}）。"
        )

    logger.info("推理设备 %s（%s）", device, reason)
    return device
