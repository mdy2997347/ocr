r"""翻译层公共契约。

本模块只定义异常体系与翻译器协议，刻意不引入任何具体实现，
以便下游（OCR 管线、单元测试）只依赖这份稳定契约。

移植来源: `google.php` (namespace ``Otc\Translate``) 的 ``Google`` 类。

关键契约（下游强依赖，不可破坏）
--------------------------------
1. ``translate_batch`` 返回的列表长度 **必须** 与输入 ``texts`` 完全相等；
2. 顺序 **必须** 与输入一致（下游按 index 把译文回填到原图坐标）；
3. 单条翻译失败时，该位置放 **原文**，绝不抛异常、绝不放 ``None``、
   绝不丢元素。整体彻底失败也不允许返回短列表。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

__all__ = [
    "TranslationError",
    "TranslatorUnavailable",
    "BaseTranslator",
]


class TranslationError(Exception):
    """翻译过程中的可恢复错误（HTTP 异常、响应格式错误等）。"""


class TranslatorUnavailable(TranslationError):
    """当前翻译通道整体不可用（缺依赖、缺凭证、通道被下线等）。"""


@runtime_checkable
class BaseTranslator(Protocol):
    """所有翻译通道实现的协议。

    实现类只需提供 ``translate_batch``；构造参数各自定义。
    """

    def translate_batch(
        self,
        texts: list[str],
        source: str | None,
        target: str,
    ) -> list[str]:
        """批量翻译。

        Args:
            texts: 待翻译文本列表，允许为空列表。
            source: 源语言代码；``None`` 或空串表示由服务端自动检测。
            target: 目标语言代码，必填。

        Returns:
            与 ``texts`` 等长、同序的译文列表；失败位置为原文。
        """
        ...
