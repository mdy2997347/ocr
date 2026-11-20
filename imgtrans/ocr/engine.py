"""文字检测 + 识别（PaddleOCR 封装）。

为什么要包一层适配：

* PaddleOCR 2.x 返回 ``[[[poly], (text, score)], ...]`` 这种嵌套 list；
  3.x 返回 ``OCRResult``（dict-like，字段 ``rec_texts`` / ``rec_scores`` /
  ``rec_polys``）。直接调库会让上层代码被版本绑死。
* 模型加载是一次性重活（数百 MB），必须进程级单例，绝不能每请求 ``PaddleOCR()``。
* 检测/识别的开关（方向分类、文档矫正）要能按配置关掉——图片翻译
  不处理整页文档，开文档矫正反而会把商品图"拉直"。

本模块只负责"图像 -> list[TextBlock]"，不做任何语言/业务判断。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

import numpy as np

from ..device import resolve_device
from ..types import OcrError, Polygon, TextBlock

logger = logging.getLogger(__name__)

#: PaddleOCR 的语言参数。PP-OCRv6 单模型覆盖中英日 + 46 种拉丁语系，
#: 其余文字系统仍走 PP-OCRv5 多语种 rec，所以 lang 要按图片实际语言选。
#: 这里只放主链路会用到的几个，其余按需追加。
LANG_ALIASES: dict[str, str] = {
    "zh": "ch",
    "zh-cn": "ch",
    "zh-hans": "ch",
    "zh-tw": "chinese_cht",
    "zh-hant": "chinese_cht",
    "en": "en",
    "ja": "japan",
    "ko": "korean",
    "ru": "cyrillic",
    "ar": "arabic",
    "th": "th",
    "vi": "latin",
    "de": "latin",
    "fr": "latin",
    "es": "latin",
}


def resolve_ocr_lang(lang: str | None) -> str:
    """把业务语言码映射成 PaddleOCR 的 lang 参数。

    未识别的语言一律回落 ``ch``：PP-OCRv6 的 ``ch`` 模型本身覆盖中英日，
    对拉丁语系也有不错的召回，是"猜错也不至于全丢"的安全默认。
    """
    if not lang:
        return "ch"
    return LANG_ALIASES.get(lang.lower(), "ch")


def _polygon_from_raw(raw: Any) -> Polygon | None:
    """把各种形态的四边形归一成 4 个点的元组。"""
    try:
        pts = [(float(p[0]), float(p[1])) for p in raw]
    except (TypeError, ValueError, IndexError):
        return None
    if len(pts) == 4:
        return (pts[0], pts[1], pts[2], pts[3])  # type: ignore[return-value]
    if len(pts) >= 4:
        # 少数情况会给出多于 4 点（曲线拟合），取轴对齐外接框的四角
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
        return ((x0, y0), (x1, y0), (x1, y1), (x0, y1))
    return None


def _get(obj: Any, key: str) -> Any:
    """兼容 dict-like 与 attribute 两种访问方式。"""
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def normalize_ocr_output(raw_result: Any) -> list[tuple[Polygon, str, float]]:
    """把 PaddleOCR 的返回归一成 ``[(polygon, text, score), ...]``。

    支持三种形态：

    1. 3.x ``predict()``：列表元素是 dict-like，含 ``rec_texts``/``rec_scores``/
       ``rec_polys``（或 ``dt_polys``）；
    2. 2.x ``ocr()``：``[[[poly], (text, score)], ...]``；
    3. 单个 dict（部分小版本）。
    """
    if raw_result is None:
        return []

    # 形态 3：直接是 dict-like
    if not isinstance(raw_result, (list, tuple)):
        raw_result = [raw_result]
    elif raw_result and not isinstance(raw_result[0], (list, tuple, dict)) and not hasattr(
        raw_result[0], "rec_texts"
    ):
        # 形如 [[poly, (text, score)], ...] 的整体
        raw_result = [raw_result]

    out: list[tuple[Polygon, str, float]] = []

    for page in raw_result:
        # --- 形态 1：3.x ---
        texts = _get(page, "rec_texts")
        if texts is not None:
            scores = _get(page, "rec_scores") or []
            polys = _get(page, "rec_polys")
            if polys is None:
                polys = _get(page, "dt_polys") or []
            for i, text in enumerate(texts):
                if not text:
                    continue
                poly = _polygon_from_raw(polys[i]) if i < len(polys) else None
                if poly is None:
                    continue
                score = float(scores[i]) if i < len(scores) else 0.0
                out.append((poly, str(text), score))
            continue

        # --- 形态 2：2.x ---
        lines = page if isinstance(page, (list, tuple)) else []
        for line in lines:
            if not isinstance(line, (list, tuple)) or len(line) < 2:
                continue
            poly = _polygon_from_raw(line[0])
            payload = line[1]
            if poly is None or not isinstance(payload, (list, tuple)) or not payload:
                continue
            text = str(payload[0] or "")
            if not text:
                continue
            score = float(payload[1]) if len(payload) > 1 else 0.0
            out.append((poly, text, score))

    return out


#: OCR 速度档位 -> 追加到 ``PaddleOCR(...)`` 的构造参数。
#:
#: 实测基线（790×1235 中文商品图，paddle 3.3.1 / paddleocr 3.7.0，CPU）：
#:
#: ======== ======== ======== ========
#: 档位     检测     识别     合计
#: ======== ======== ======== ========
#: quality  11.9s    3.1s     15.0s
#: fast      4.3s    3.1s      7.5s
#: ======== ======== ======== ========
#:
#: **瓶颈在检测，不在识别。** PP-OCRv6 只发布 medium 一档，而且检测模型
#: 比识别模型重得多（6.7 倍）。所以 ``fast`` 只换检测模型、识别仍用默认档——
#: 识别精度**一点没降**，省下的全是从检测里抠出来的。
#:
#: 6 张真实图逐块比对（换前 vs 换后）：中文正文持平或更好，典型如
#: ``霍尼书小的PTM7950是790余列中的高等热柏变材`` -> ``霍尼韦尔的PTM7950是
#: 7900系列中的高导热相变材科``（旧档把商标名整个认错，新档全对）。
#: 已知代价：紧挨着的两行可能被检成一个框（进而并成一段）、照片里的
#: 英文小字召回略降（``Compound Softens`` -> ``und Soften``）。
#:
#: 因此默认仍是 ``quality``：这是有取舍的开关，不该替使用者做决定。
OCR_PROFILES: dict[str, dict[str, Any]] = {
    "quality": {},
    "fast": {"text_detection_model_name": "PP-OCRv5_mobile_det"},
}

#: 默认档位。详见 :data:`OCR_PROFILES`。
DEFAULT_OCR_PROFILE = "quality"

#: 档位加载失败时退化到的档位。
#:
#: 单独定义成常量，是因为它不只用于回退，还用于**如实上报**：``/healthz``
#: 的 ``ocr_profile_effective`` 要从这里取名，硬编码两次迟早写歪。
FALLBACK_OCR_PROFILE = "quality"


class OcrEngine:
    """线程安全的 PaddleOCR 单例封装。

    首次 :meth:`recognize` 才真正加载模型（惰性），避免 import 就吃掉几百 MB
    显存/内存。多语言实例按 ``lang`` 分别缓存——不同语言的 rec 模型不同。
    """

    def __init__(
        self,
        *,
        use_textline_orientation: bool = True,
        use_doc_unwarping: bool = False,
        use_doc_orientation_classify: bool = False,
        device: str | None = None,
        enable_mkldnn: bool = False,
        profile: str = DEFAULT_OCR_PROFILE,
        det_limit_side_len: int | None = None,
    ) -> None:
        """
        Args:
            use_textline_orientation: 开方向分类（处理 180° 倒置文字）。
                图片翻译里很常见（截图翻转），建议开。
            use_doc_unwarping: 文档矫正。**默认关**——商品图/海报被"拉直"
                会破坏版面，回填坐标就全错。
            use_doc_orientation_classify: 整页方向分类，同上，默认关。
            device: 推理设备。``cpu`` / ``gpu`` / ``gpu:1`` / ``auto`` 均可，
                交给 :func:`imgtrans.device.resolve_device` 归一化；
                ``None`` 表示读 ``IMGTRANS_DEVICE``（默认 ``cpu``）。
                **不传就不猜**——自动挑卡在显存被占的机器上会炸得莫名其妙。
            enable_mkldnn: 是否启用 oneDNN 加速。**默认关**，原因是实测踩到的坑：
                paddlepaddle 3.3.1 + PIR 执行器下开 oneDNN 会在检测阶段直接崩
                （``ConvertPirAttribute2RuntimeAttribute not support
                [pir::ArrayAttribute<pir::DoubleAttribute>]``），
                这是 paddle 内部 oneDNN 指令转换的已知不兼容，不是参数用错。
                关掉后 CPU 推理正常（640x400 图 5.3s，含首次预热）。
                若将来 paddle 修好了可显式传 True 提速。GPU 上该项无意义，
                会被强制关掉并告警。
            profile: 速度档位，见 :data:`OCR_PROFILES`。``quality``（默认）
                识别与检测都用 PP-OCRv6 medium；``fast`` 只把检测换成
                PP-OCRv5 mobile，CPU 上实测 15.0s -> 7.5s，识别精度不变。
                档位名非法时**在构造时就抛**，不留到第一个请求才炸。
            det_limit_side_len: 检测阶段的长边上限（像素），``None`` 用
                PaddleOCR 默认（736 / min，短边不足会放大）。调小可再快
                约 15%，但密集小字的召回会下降——属于细调项，默认不碰。

        Raises:
            DeviceError: 设备取值非法，或配了 gpu 但当前 paddle 用不了卡。
                **在构造时就抛**，而不是等第一个请求——配置错误不该表现成
                "第一次调用很慢"。
            OcrError: ``profile`` 不在 :data:`OCR_PROFILES` 里。
        """
        self._use_textline_orientation = use_textline_orientation
        self._use_doc_unwarping = use_doc_unwarping
        self._use_doc_orientation_classify = use_doc_orientation_classify
        self._device = resolve_device(device)
        if enable_mkldnn and self._device != "cpu":
            logger.warning(
                "enable_mkldnn 只对 CPU 生效，已忽略（当前 device=%s）", self._device
            )
            enable_mkldnn = False
        self._enable_mkldnn = enable_mkldnn

        key = (profile or DEFAULT_OCR_PROFILE).strip().lower()
        if key not in OCR_PROFILES:
            raise OcrError(
                f"未知的 OCR 档位 {profile!r}；可选：{', '.join(sorted(OCR_PROFILES))}"
            )
        self._profile = key
        #: 深拷贝一份：调用方改 OCR_PROFILES 不该影响已建好的实例。
        self._profile_kwargs: dict[str, Any] = dict(OCR_PROFILES[key])
        self._det_limit_side_len = det_limit_side_len

        self._engines: dict[str, Any] = {}
        #: ``lang -> 实际生效的档位``。fast 的检测模型是**额外下载**的，
        #: 拿不到时会静默回退 quality——如果只报配置值，线上就会出现
        #: "healthz 说 fast、实际跑 quality"这种自相矛盾的读数，
        #: 2026-11 排查"fast 怎么没提速"时正是被这个读数误导了半天。
        self._effective_profiles: dict[str, str] = {}
        self._lock = threading.Lock()

    @property
    def profile(self) -> str:
        """**配置**的档位（意图），供启动日志展示。

        想知道实际跑了哪一档，看 :attr:`profile_effective`。
        """
        return self._profile

    @property
    def degraded_langs(self) -> list[str]:
        """已加载且实际回退到慢档的语言。空列表 = 已加载的全部生效。"""
        return sorted(
            lang
            for lang, effective in self._effective_profiles.items()
            if effective != self._profile
        )

    @property
    def profile_effective(self) -> str:
        """**实际**生效的档位。

        任一语言回退即视为未生效（返回回退档位）：部分生效比全不生效更难发现，
        耗时介于两档之间，看数字只会觉得"快了，但没快那么多"。

        模型尚未加载时无从得知，返回配置值；此时 :attr:`degraded_langs` 为空
        不代表已确认生效，只代表还没开始加载。
        """
        if not self._profile_kwargs:
            # quality 档没有额外参数，它本来就是回退目标，不存在"被降级"。
            return self._profile
        if self.degraded_langs:
            return FALLBACK_OCR_PROFILE
        return self._profile

    @property
    def device(self) -> str:
        """最终生效的推理设备（``cpu`` / ``gpu:N``），已归一化。

        保留为只读属性：启动日志和 ``/healthz`` 要能如实报告"到底跑在哪"，
        光看 ``.env`` 是看不出 ``auto`` 落在哪一边的。
        """
        return self._device

    def _build(self, lang: str) -> Any:
        try:
            from paddleocr import PaddleOCR
        except ImportError as exc:
            raise OcrError(
                "未安装 paddleocr，请执行: pip install paddleocr paddlepaddle"
            ) from exc

        kwargs: dict[str, Any] = {
            "lang": lang,
            "use_doc_orientation_classify": self._use_doc_orientation_classify,
            "use_doc_unwarping": self._use_doc_unwarping,
            "use_textline_orientation": self._use_textline_orientation,
            "enable_mkldnn": self._enable_mkldnn,
            # 恒显式传：交给 paddle 猜的话，GPU 版 paddle 会自行占卡，
            # 与"默认 cpu"的约定不一致。
            "device": self._device,
        }
        kwargs.update(self._profile_kwargs)
        if self._det_limit_side_len:
            kwargs["text_det_limit_side_len"] = int(self._det_limit_side_len)

        try:
            engine = PaddleOCR(**kwargs)
        except TypeError as exc:
            # 2.x 不认这些参数，退化成最小构造。2.x 用 use_gpu 布尔而非 device 串，
            # 传错会二次 TypeError——这个分支已经不指望能测，只能按官方签名写。
            logger.warning("PaddleOCR 新版参数不被接受(%s)，退化为最小构造", exc)
            fallback: dict[str, Any] = {
                "lang": lang,
                "use_angle_cls": True,
                "use_gpu": self._device != "cpu",
            }
            engine = PaddleOCR(**fallback)
            # 参数都不认，档位自然也落不下去。
            self._effective_profiles[lang] = FALLBACK_OCR_PROFILE
            return engine
        except Exception as exc:  # noqa: BLE001 - 见下
            # fast 档的检测模型要额外下载，也可能与当前 paddle 版本不匹配。
            # 这种时候退化成 quality（慢但能跑）远好过整站不可用：
            # 档位是"想更快"的优化项，不该成为"服务起不来"的原因。
            if not self._profile_kwargs:
                raise OcrError(f"PaddleOCR 初始化失败：{exc}") from exc
            logger.warning(
                "OCR 档位 %s 的检测模型加载失败(%s)，本语言回退到 quality",
                self._profile,
                exc,
            )
            degraded = {k: v for k, v in kwargs.items() if k not in self._profile_kwargs}
            try:
                engine = PaddleOCR(**degraded)
            except Exception as inner:  # noqa: BLE001 - 回退也失败，只能报错
                raise OcrError(f"PaddleOCR 初始化失败（含回退 quality）：{inner}") from inner
            self._effective_profiles[lang] = FALLBACK_OCR_PROFILE
            return engine

        self._effective_profiles[lang] = self._profile
        return engine

    def get(self, lang: str) -> Any:
        """取（或惰性构造）指定语言的引擎实例。"""
        engine = self._engines.get(lang)
        if engine is not None:
            return engine
        with self._lock:
            engine = self._engines.get(lang)
            if engine is None:
                started = time.perf_counter()
                engine = self._build(lang)
                self._engines[lang] = engine
                logger.info(
                    "PaddleOCR(lang=%s) 加载完成，耗时 %.2fs", lang, time.perf_counter() - started
                )
            return engine

    def recognize(
        self,
        image_bgr: np.ndarray,
        *,
        lang: str = "ch",
        min_confidence: float = 0.5,
    ) -> list[TextBlock]:
        """对一帧 BGR 图像做检测 + 识别。

        Args:
            image_bgr: OpenCV 风格的 BGR ndarray。
            lang: PaddleOCR 语言参数（用 :func:`resolve_ocr_lang` 转换）。
            min_confidence: 低于该置信度的块直接丢弃——识别错的文字
                拿去翻译再画上去，比不翻译更糟。

        Returns:
            按阅读顺序（自上而下、自左而右）编号的 :class:`TextBlock` 列表。
        """
        if image_bgr is None or image_bgr.size == 0:
            raise OcrError("输入图像为空")

        engine = self.get(lang)

        # 3.x 用 predict，2.x 用 ocr。先试新版，失败回退。
        raw: Any = None
        predict = getattr(engine, "predict", None)
        if callable(predict):
            try:
                raw = predict(input=image_bgr)
            except TypeError:
                raw = predict(image_bgr)
            except Exception as exc:  # noqa: BLE001 - 记录后回退到旧 API
                logger.warning("predict() 调用失败(%s)，尝试 ocr()", exc)
                raw = None
        if raw is None:
            ocr = getattr(engine, "ocr", None)
            if not callable(ocr):
                raise OcrError("PaddleOCR 实例既无 predict 也无 ocr 方法")
            try:
                raw = ocr(image_bgr, cls=self._use_textline_orientation)
            except TypeError:
                raw = ocr(image_bgr)

        triples = normalize_ocr_output(raw)

        blocks: list[TextBlock] = []
        for poly, text, score in triples:
            if score < min_confidence:
                logger.debug("drop low-confidence block %.2f %r", score, text[:20])
                continue
            blocks.append(
                TextBlock(
                    index=len(blocks),
                    polygon=poly,
                    text=text,
                    confidence=score,
                    vertical=_looks_vertical(poly),
                )
            )

        return sort_reading_order(blocks)


def _looks_vertical(poly: Polygon) -> bool:
    """用宽高比粗判竖排。竖排文字的宽明显小于高。

    注意：这不是可靠的竖排检测（单个汉字也是正方形），只是给排版层
    一个"这行可能不该横排回填"的提示。
    """
    xs = [p[0] for p in poly]
    ys = [p[1] for p in poly]
    width = max(xs) - min(xs)
    height = max(ys) - min(ys)
    return height > 0 and width / height < 0.6


def sort_reading_order(blocks: list[TextBlock]) -> list[TextBlock]:
    """按阅读顺序重排并**重新编号**。

    策略：先按行聚类（y 中心差在半行高内视为同一行），行内按 x 排序。
    简单的 ``sorted(key=lambda b: (y, x))`` 在文字略微倾斜时会打乱行序，
    所以要先聚类。

    重编号是必须的：OCR 原始顺序不保证稳定，而后续所有回填都依赖
    ``index`` 作主键。
    """
    if not blocks:
        return []

    # 以中位行高作为聚类阈值，比固定像素值更适应不同分辨率
    heights = sorted(b.height for b in blocks)
    median_h = heights[len(heights) // 2] or 1.0
    row_tol = median_h * 0.5

    def y_center(b: TextBlock) -> float:
        ys = [p[1] for p in b.polygon]
        return (min(ys) + max(ys)) / 2

    def x_left(b: TextBlock) -> float:
        return min(p[0] for p in b.polygon)

    ordered = sorted(blocks, key=lambda b: (y_center(b), x_left(b)))

    rows: list[list[TextBlock]] = []
    for block in ordered:
        if not rows:
            rows.append([block])
            continue
        row_y = sum(y_center(b) for b in rows[-1]) / len(rows[-1])
        if abs(y_center(block) - row_y) <= row_tol:
            rows[-1].append(block)
        else:
            rows.append([block])

    result: list[TextBlock] = []
    for row in rows:
        for block in sorted(row, key=x_left):
            block.index = len(result)
            result.append(block)
    return result
