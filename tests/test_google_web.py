"""``GoogleWebTranslator`` 的 HTTP 层单测（全部注入假 transport，不联网）。

覆盖点（对应需求第五节）：

* 多 ``q`` 参数真的发成重复 key；
* ``format=html`` 的实体反转义；
* 大小写不被篡改；
* 批内顺序与 index 映射（第 2 批结果不串到第 1 批）；
* 字符数 / 条数双维度切批；
* api 通道 403 → 刷新 TKK 重试 → 仍失败 → 降级 free；
* free 通道 503 → 重试一次；
* 全部失败 → 原文占位且长度不变；
* 令牌桶限流生效。
"""

from __future__ import annotations

import inspect
import json
import logging
import time
import unittest
from dataclasses import dataclass
from typing import Any, Callable
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from imgtrans.translator.google_tk import DEFAULT_TKK, build_tk
from imgtrans.translator.google_web import (
    API_DOMAIN,
    FREE_DOMAIN,
    FREE_PATH,
    FREE_USER_AGENT,
    GoogleWebTranslator,
    _CircuitBreaker,
    _HttpXTransport,
)

HOME_HTML = "<html><script>window.TKK = '406398.2087938574';</script></html>"


def setUpModule() -> None:
    """静音库内日志，保持 ``-v`` 输出可读（``assertLogs`` 会自行接管级别）。"""
    logging.getLogger("imgtrans").setLevel(logging.CRITICAL)


@dataclass
class Call:
    """一次被记录的假请求。"""

    method: str
    url: str
    params: dict[str, Any] | None
    headers: dict[str, str] | None
    data: bytes | None
    timeout: float = 0.0

    @property
    def query(self) -> dict[str, list[str]]:
        """URL 查询串（保留重复 key）。"""
        return parse_qs(urlsplit(self.url).query, keep_blank_values=True)

    @property
    def path(self) -> str:
        """URL 路径。"""
        return urlsplit(self.url).path

    @property
    def body(self) -> str:
        """请求体文本。"""
        return (self.data or b"").decode("utf-8")

    @property
    def form(self) -> dict[str, list[str]]:
        """请求体表单（保留重复 key）。"""
        return parse_qs(self.body, keep_blank_values=True)

    @property
    def qs(self) -> list[str]:
        """请求体里按顺序排列的 ``q`` 值。"""
        return self.form.get("q", [])


class FakeTransport:
    """可编排的假传输层，记录每次调用。

    ``handler(call) -> (status, body)``；未指定时返回空 JSON 数组。
    """

    def __init__(self, handler: Callable[[Call], tuple[int, str]] | None = None) -> None:
        self.calls: list[Call] = []
        self._handler = handler or (lambda call: (200, "[]"))

    def __call__(
        self,
        method: str,
        url: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        data: bytes | None = None,
        timeout: float = 8.0,
    ) -> tuple[int, str]:
        call = Call(
            method=method,
            url=url,
            params=params,
            headers=dict(headers or {}),
            data=data,
            timeout=timeout,
        )
        self.calls.append(call)
        return self._handler(call)

    # -- 便捷查询 ---------------------------------------------------- #

    def api_calls(self) -> list[Call]:
        """``/translate_a/t`` 调用。"""
        return [call for call in self.calls if call.path == "/translate_a/t"]

    def free_calls(self) -> list[Call]:
        """``/translate_a/single`` 调用。"""
        return [call for call in self.calls if call.path == FREE_PATH]

    def home_calls(self) -> list[Call]:
        """抓 TKK 的首页 GET。"""
        return [call for call in self.calls if call.method == "GET"]


def free_ok(call: Call) -> tuple[int, str]:
    """把请求体里的 ``q`` 包一层，模拟 free 通道成功响应。"""
    text = call.form.get("q", [""])[0]
    return 200, json.dumps({"sentences": [{"trans": f"译:{text}", "orig": text}]})


def api_ok(call: Call) -> tuple[int, str]:
    """把每个 ``q`` 包一层，模拟 api 通道成功响应（顺序一一对应）。"""
    return 200, json.dumps([f"译:{text}" for text in call.qs])


def router(
    *,
    api: Callable[[Call], tuple[int, str]] | None = None,
    free: Callable[[Call], tuple[int, str]] | None = None,
    home: Callable[[Call], tuple[int, str]] | None = None,
) -> FakeTransport:
    """按 URL 路由的假传输层。"""

    def handler(call: Call) -> tuple[int, str]:
        if call.method == "GET":
            return home(call) if home else (200, HOME_HTML)
        if call.path == "/translate_a/t":
            return api(call) if api else (200, "[]")
        if call.path == FREE_PATH:
            return free(call) if free else (200, json.dumps({"sentences": []}))
        raise AssertionError(f"unexpected request: {call.method} {call.url}")

    return FakeTransport(handler)


def make_translator(transport: FakeTransport, **kwargs: Any) -> GoogleWebTranslator:
    """构造一个把退避与惩罚都关掉的翻译器（避免单测变慢）。

    ``fetch_tkk=True`` 是测试的默认：这些用例注入的是假 transport，
    抓首页不产生真实网络成本，而它们要验证的正是"抓取 / 刷新 / 退避"这套逻辑。
    生产默认是 ``False``（Google 首页已无 tkk 变量，抓也是白抓），
    那个行为由 :class:`TkkFetchDisabledTest` 单独覆盖。
    """
    params: dict[str, Any] = {
        "timeout": 2.0,
        "qps": 0.0,
        "transport": transport,
        "fetch_tkk": True,
    }
    params.update(kwargs)
    translator = GoogleWebTranslator(**params)
    translator.BACKOFF_BASE = 0.0
    translator.BACKOFF_JITTER = 0.0
    translator.RATE_LIMIT_PENALTY = 0.0
    return translator


class RequestShapeTest(unittest.TestCase):
    """请求形状：重复 key、query 参数、请求头。"""

    def test_multiple_q_params_are_repeated_keys(self) -> None:
        transport = router(api=api_ok)
        translator = make_translator(transport)
        result = translator.translate_batch(["one", "two", "three"], None, "zh-CN")

        self.assertEqual(result, ["译:one", "译:two", "译:three"])
        api_calls = transport.api_calls()
        self.assertEqual(len(api_calls), 1)
        call = api_calls[0]
        # 请求体是 "q=one&q=two&q=three" —— 重复 key，顺序保持
        self.assertEqual(call.body, "q=one&q=two&q=three")
        self.assertEqual(call.qs, ["one", "two", "three"])
        self.assertEqual(
            call.headers.get("Content-Type"), "application/x-www-form-urlencoded"
        )

    def test_api_query_parameters(self) -> None:
        transport = router(api=api_ok)
        translator = make_translator(transport)
        translator.translate_batch(["hello"], "en", "zh-CN")

        call = transport.api_calls()[0]
        self.assertTrue(call.url.startswith(f"{API_DOMAIN}/translate_a/t?"))
        self.assertEqual(call.query["client"], ["te"])
        self.assertEqual(call.query["format"], ["html"])
        self.assertEqual(call.query["v"], ["1.0"])
        self.assertEqual(call.query["sl"], ["en"])
        self.assertEqual(call.query["tl"], ["zh-CN"])
        self.assertEqual(call.query["tc"], ["3"])
        self.assertEqual(call.query["ctt"], ["1"])
        # tk 用"所有文本拼接串"计算（PHP implode("", $text)）
        self.assertEqual(call.query["tk"], [build_tk("hello")])
        self.assertEqual(call.method, "POST")

    def test_source_defaults_to_auto(self) -> None:
        transport = router(api=api_ok)
        translator = make_translator(transport)
        translator.translate_batch(["hi"], None, "ja")
        self.assertEqual(transport.api_calls()[0].query["sl"], ["auto"])

    def test_tk_uses_concatenation_of_batch(self) -> None:
        transport = router(api=api_ok)
        translator = make_translator(transport)
        translator.translate_batch(["你", "好"], None, "en")
        call = transport.api_calls()[0]
        self.assertEqual(call.query["tk"], [build_tk("你好")])
        self.assertNotEqual(call.query["tk"], [build_tk("你")])

    def test_query_encoding_of_non_ascii(self) -> None:
        transport = router(api=api_ok)
        translator = make_translator(transport)
        translator.translate_batch(["你好 世界"], None, "en")
        # PHP urlencode 语义：空格 → "+"，非 ASCII → %XX
        self.assertEqual(transport.api_calls()[0].body, "q=%E4%BD%A0%E5%A5%BD+%E4%B8%96%E7%95%8C")


class PostProcessingTest(unittest.TestCase):
    """响应后处理：实体反转义、大小写保真。"""

    def test_html_entities_are_unescaped(self) -> None:
        payload = ["&quot;Hi&quot; &amp; &lt;b&gt; &#39;x&#39; &#039;y&#039;"]
        transport = router(api=lambda call: (200, json.dumps(payload)))
        translator = make_translator(transport)
        result = translator.translate_batch(["ignored"], None, "en")
        self.assertEqual(result, ['"Hi" & <b> \'x\' \'y\''])

    def test_case_is_not_modified(self) -> None:
        """PHP 的 ucfirst(strtolower()) 必须被丢弃：大小写原样保留。"""
        transport = router(api=lambda call: (200, json.dumps(["Hello WORLD"])))
        translator = make_translator(transport)
        result = translator.translate_batch(["Hello WORLD"], None, "de")
        self.assertEqual(result, ["Hello WORLD"])
        self.assertNotEqual(result, ["Hello world"])

    def test_german_nouns_keep_capitalisation(self) -> None:
        transport = router(api=lambda call: (200, json.dumps(["Die Übersetzung"])))
        translator = make_translator(transport)
        self.assertEqual(
            translator.translate_batch(["x"], None, "de"), ["Die Übersetzung"]
        )

    def test_empty_translation_falls_back_to_original(self) -> None:
        transport = router(api=lambda call: (200, json.dumps([""])))
        translator = make_translator(transport)
        self.assertEqual(translator.translate_batch(["Hello"], None, "ja"), ["Hello"])


class BatchingTest(unittest.TestCase):
    """切批与 index 映射。"""

    def test_thirty_texts_split_by_item_limit(self) -> None:
        transport = router(api=api_ok)
        translator = make_translator(transport, max_items_per_request=20)
        texts = [f"t{i:02d}" for i in range(30)]

        result = translator.translate_batch(texts, None, "en")

        self.assertEqual(result, [f"译:{text}" for text in texts])
        api_calls = transport.api_calls()
        self.assertEqual(len(api_calls), 2)
        self.assertEqual(len(api_calls[0].qs), 20)
        self.assertEqual(len(api_calls[1].qs), 10)
        # 第 2 批结果不能串到第 1 批
        self.assertEqual(api_calls[0].qs, texts[:20])
        self.assertEqual(api_calls[1].qs, texts[20:])
        self.assertEqual(result[20], "译:t20")
        self.assertEqual(result[0], "译:t00")

    def test_split_by_char_limit(self) -> None:
        transport = router(api=api_ok)
        translator = make_translator(transport, max_chars_per_request=10)
        texts = ["1234567890", "1234567890", "abc"]

        result = translator.translate_batch(texts, None, "en")

        self.assertEqual(result, [f"译:{text}" for text in texts])
        api_calls = transport.api_calls()
        self.assertEqual(len(api_calls), 3)  # 10 / 10 / 3
        self.assertEqual([len(call.qs) for call in api_calls], [1, 1, 1])

    def test_char_limit_packs_while_fits(self) -> None:
        transport = router(api=api_ok)
        translator = make_translator(transport, max_chars_per_request=15)
        texts = ["1234567890", "abc", "abcdefgh"]

        translator.translate_batch(texts, None, "en")

        api_calls = transport.api_calls()
        self.assertEqual(len(api_calls), 2)
        self.assertEqual(api_calls[0].qs, ["1234567890", "abc"])  # 10 + 3 <= 15
        self.assertEqual(api_calls[1].qs, ["abcdefgh"])

    def test_overlong_single_text_sent_alone(self) -> None:
        transport = router(api=api_ok)
        translator = make_translator(transport, max_chars_per_request=5)
        long_text = "abcdefghij"
        translator.translate_batch(["ab", long_text], None, "en")

        api_calls = transport.api_calls()
        self.assertEqual(len(api_calls), 2)
        self.assertEqual(api_calls[1].qs, [long_text])  # 不截断

    def test_middle_batch_failure_keeps_order(self) -> None:
        """中间批次整体失败时，只有该批次位置回填原文，顺序不变。"""

        def api(call: Call) -> tuple[int, str]:
            if "t10" in call.qs:
                return 500, "server error"
            return api_ok(call)

        transport = router(api=api, free=lambda call: (500, "server error"))
        translator = make_translator(transport, max_items_per_request=10)
        texts = [f"t{i:02d}" for i in range(30)]

        result = translator.translate_batch(texts, None, "en")

        self.assertEqual(len(result), 30)
        self.assertEqual(result[:10], [f"译:{text}" for text in texts[:10]])
        self.assertEqual(result[10:20], texts[10:20])  # 原文占位
        self.assertEqual(result[20:], [f"译:{text}" for text in texts[20:]])

    def test_hundred_and_thirty_seven_blocks(self) -> None:
        """T2：137 块按 max_items=50 切成 50/50/37，且全程 index 正确。"""
        transport = router(api=api_ok)
        translator = make_translator(transport)
        texts = [f"块{i:03d}" for i in range(137)]

        result = translator.translate_batch(texts, None, "en")

        self.assertEqual(len(result), 137)
        self.assertEqual([len(call.qs) for call in transport.api_calls()], [50, 50, 37])
        self.assertEqual(result, [f"译:{text}" for text in texts])


class ChannelFallbackTest(unittest.TestCase):
    """通道降级链路。"""

    def test_api_403_refresh_tkk_then_free(self) -> None:
        """api 403 → 刷新 TKK 重试 1 次 → 仍失败 → 降级 free。"""
        home_state = {"count": 0}

        def home(call: Call) -> tuple[int, str]:
            home_state["count"] += 1
            if home_state["count"] == 1:
                return 200, f"<html>tkk:'{DEFAULT_TKK}'</html>"
            return 200, "<html>tkk:'999999.1234567'</html>"

        transport = router(
            api=lambda call: (403, "forbidden"),
            free=free_ok,
            home=home,
        )
        translator = make_translator(transport)

        result = translator.translate_batch(["Hello"], "en", "zh-CN")

        self.assertEqual(result, ["译:Hello"])
        api_calls = transport.api_calls()
        self.assertEqual(len(api_calls), 2, "should retry api exactly once after TKK refresh")
        self.assertEqual(api_calls[0].query["tk"], [build_tk("Hello", DEFAULT_TKK)])
        # 第二次用的是刷新后的 TKK
        self.assertEqual(api_calls[1].query["tk"], [build_tk("Hello", "999999.1234567")])
        self.assertNotEqual(api_calls[0].query["tk"], api_calls[1].query["tk"])

        free_calls = transport.free_calls()
        self.assertEqual(len(free_calls), 1)
        self.assertEqual(free_calls[0].form["q"], ["Hello"])
        self.assertEqual(free_calls[0].form["sl"], ["en"])
        self.assertEqual(free_calls[0].form["tl"], ["zh-CN"])

    def test_free_channel_503_retries_once(self) -> None:
        state = {"count": 0}

        def free(call: Call) -> tuple[int, str]:
            state["count"] += 1
            if state["count"] == 1:
                return 503, "Service Unavailable"
            return free_ok(call)

        transport = router(api=lambda call: (403, ""), free=free)
        translator = make_translator(transport)
        # MAX_ATTEMPTS=1 让内部的 HTTP 级重试不吞掉 503，专测通道级重试
        translator.MAX_ATTEMPTS = 1

        result = translator.translate_batch(["Hello"], None, "ja")

        self.assertEqual(result, ["译:Hello"])
        self.assertEqual(len(transport.free_calls()), 2)
        # 两次请求用了不同的 iid（"刷新 cookie" 后的新会话标识）
        first, second = transport.free_calls()
        self.assertNotEqual(first.query["iid"], second.query["iid"])

    def test_free_channel_request_shape(self) -> None:
        transport = router(api=lambda call: (403, ""), free=free_ok)
        translator = make_translator(transport)
        translator.translate_batch(["Hi"], None, "ko")

        call = transport.free_calls()[0]
        self.assertTrue(call.url.startswith(f"{FREE_DOMAIN}{FREE_PATH}?"))
        self.assertEqual(call.headers.get("User-Agent"), FREE_USER_AGENT)
        self.assertEqual(call.query["client"], ["at"])
        self.assertEqual(call.query["dj"], ["1"])
        for flag in ("t", "ld", "qca", "rm", "bd"):
            self.assertIn(flag, call.query["dt"])
        self.assertEqual(len(call.query["iid"]), 1)

    def test_free_channel_joins_sentences(self) -> None:
        def free(call: Call) -> tuple[int, str]:
            return 200, json.dumps(
                {"sentences": [{"trans": "第一句。"}, {"trans": " Second"}, {"trans": "。"}]}
            )

        transport = router(api=lambda call: (403, ""), free=free)
        translator = make_translator(transport)
        self.assertEqual(
            translator.translate_batch(["abc"], None, "en"), ["第一句。 Second。"]
        )

    def test_free_channel_failure_marks_item(self) -> None:
        transport = router(api=lambda call: (403, ""), free=lambda call: (400, "bad"))
        translator = make_translator(transport)
        translator.MAX_ATTEMPTS = 1
        self.assertEqual(translator.translate_batch(["abc"], None, "en"), ["abc"])

    def test_all_channels_fail_returns_originals(self) -> None:
        """全部失败 → 原文占位、长度不变、顺序不变。"""
        transport = router(
            api=lambda call: (500, "boom"),
            free=lambda call: (500, "boom"),
        )
        translator = make_translator(transport, max_items_per_request=2)
        texts = ["alpha", "beta", "gamma"]

        result = translator.translate_batch(texts, None, "en")

        self.assertEqual(result, texts)
        self.assertEqual(len(result), len(texts))

    def test_api_length_mismatch_triggers_fallback(self) -> None:
        transport = router(
            api=lambda call: (200, json.dumps(["only-one"])),
            free=free_ok,
        )
        translator = make_translator(transport)
        result = translator.translate_batch(["a", "b"], None, "en")
        self.assertEqual(result, ["译:a", "译:b"])
        self.assertEqual(len(transport.api_calls()), 2)  # 初次 + 刷新 TKK 后重试

    def test_api_non_list_payload_triggers_fallback(self) -> None:
        transport = router(
            api=lambda call: (200, json.dumps({"error": "nope"})),
            free=free_ok,
        )
        translator = make_translator(transport)
        self.assertEqual(
            translator.translate_batch(["a"], None, "en"), ["译:a"]
        )

    def test_api_invalid_json_triggers_fallback(self) -> None:
        transport = router(
            api=lambda call: (200, "not-json"),
            free=free_ok,
        )
        translator = make_translator(transport)
        self.assertEqual(translator.translate_batch(["a"], None, "en"), ["译:a"])

    def test_transport_exception_is_retried_then_falls_back(self) -> None:
        state = {"count": 0}

        def api(call: Call) -> tuple[int, str]:
            state["count"] += 1
            raise OSError("connection reset")

        transport = router(api=api, free=free_ok)
        translator = make_translator(transport)
        result = translator.translate_batch(["a"], None, "en")

        self.assertEqual(result, ["译:a"])
        # 3 次尝试（初次批量）+ 3 次尝试（刷新 TKK 后重试）
        self.assertEqual(state["count"], translator.MAX_ATTEMPTS * 2)

    def test_retryable_status_is_retried(self) -> None:
        state = {"count": 0}

        def api(call: Call) -> tuple[int, str]:
            state["count"] += 1
            if state["count"] < 3:
                return 429, "rate limited"
            return api_ok(call)

        transport = router(api=api)
        translator = make_translator(transport)
        translator.RATE_LIMIT_PENALTY = 0.0

        result = translator.translate_batch(["a"], None, "en")

        self.assertEqual(result, ["译:a"])
        self.assertEqual(state["count"], 3)

    def test_prefer_free_uses_free_first(self) -> None:
        transport = router(api=api_ok, free=free_ok)
        translator = make_translator(transport, prefer="free")

        result = translator.translate_batch(["a"], None, "en")

        self.assertEqual(result, ["译:a"])
        self.assertEqual(len(transport.free_calls()), 1)
        self.assertEqual(len(transport.api_calls()), 0)

    def test_prefer_free_falls_back_to_api(self) -> None:
        transport = router(api=api_ok, free=lambda call: (500, "boom"))
        translator = make_translator(transport, prefer="free")
        translator.MAX_ATTEMPTS = 1

        self.assertEqual(translator.translate_batch(["a"], None, "en"), ["译:a"])
        self.assertEqual(len(transport.api_calls()), 1)


class SkipAndEdgeCaseTest(unittest.TestCase):
    """不产生请求的场景与入参校验。"""

    def test_empty_input(self) -> None:
        transport = router()
        translator = make_translator(transport)
        self.assertEqual(translator.translate_batch([], None, "en"), [])
        self.assertEqual(transport.calls, [])

    def test_blank_texts_skipped(self) -> None:
        transport = router(api=api_ok)
        translator = make_translator(transport)

        result = translator.translate_batch(["", "   ", "\n", "Hello"], None, "en")

        self.assertEqual(result, ["", "   ", "\n", "译:Hello"])
        api_calls = transport.api_calls()
        self.assertEqual(len(api_calls), 1)
        self.assertEqual(api_calls[0].qs, ["Hello"])

    def test_all_blank_makes_no_request(self) -> None:
        transport = router()
        translator = make_translator(transport)
        self.assertEqual(translator.translate_batch(["", " "], None, "en"), ["", " "])
        self.assertEqual(transport.calls, [])

    def test_none_target_raises(self) -> None:
        translator = make_translator(router())
        with self.assertRaises(ValueError):
            translator.translate_batch(["a"], None, "")

    def test_invalid_constructor_arguments(self) -> None:
        with self.assertRaises(ValueError):
            GoogleWebTranslator(timeout=0, transport=FakeTransport())
        with self.assertRaises(ValueError):
            GoogleWebTranslator(max_chars_per_request=0, transport=FakeTransport())
        with self.assertRaises(ValueError):
            GoogleWebTranslator(max_items_per_request=0, transport=FakeTransport())
        with self.assertRaises(ValueError):
            GoogleWebTranslator(prefer="bogus", transport=FakeTransport())

    def test_he_maps_to_iw(self) -> None:
        transport = router(api=api_ok)
        translator = make_translator(transport)
        translator.translate_batch(["a"], "he", "en")
        self.assertEqual(transport.api_calls()[0].query["sl"], ["iw"])

    def test_translate_single_helper(self) -> None:
        transport = router(api=api_ok)
        translator = make_translator(transport)
        self.assertEqual(translator.translate("Hello", None, "ja"), "译:Hello")

    def test_context_manager_closes_transport(self) -> None:
        closed: list[bool] = []

        class Closeable(FakeTransport):
            def close(self) -> None:
                closed.append(True)

        transport = Closeable(lambda call: (200, "[]"))
        with make_translator(transport):
            pass
        self.assertEqual(closed, [True])

    def test_repr(self) -> None:
        self.assertIn("prefer='api'", repr(make_translator(router())))

    def test_unexpected_error_never_breaks_contract(self) -> None:
        """内部异常也不能破坏"等长同序 + 原文占位"的契约。"""
        translator = make_translator(router(api=api_ok))
        texts = ["a", "b"]

        with mock.patch.object(
            GoogleWebTranslator, "_translate_chunk", side_effect=RuntimeError("boom")
        ):
            result = translator.translate_batch(texts, None, "en")

        self.assertEqual(result, texts)


class RateLimitTest(unittest.TestCase):
    """令牌桶限流。"""

    def test_tokens_throttle_requests(self) -> None:
        transport = router(api=api_ok)
        # qps=2 → 容量 2，第 3 个请求起每个至少等 0.5s
        translator = make_translator(
            transport, qps=2.0, max_items_per_request=1
        )

        started = time.monotonic()
        result = translator.translate_batch(["a", "b", "c", "d"], None, "en")
        elapsed = time.monotonic() - started

        self.assertEqual(result, ["译:a", "译:b", "译:c", "译:d"])
        self.assertEqual(len(transport.api_calls()), 4)
        # 宽松下界，避免机器抖动导致 flaky
        self.assertGreaterEqual(elapsed, 0.4, "rate limiter did not throttle")

    def test_unlimited_when_qps_zero(self) -> None:
        transport = router(api=api_ok)
        translator = make_translator(transport, qps=0.0, max_items_per_request=1)

        started = time.monotonic()
        translator.translate_batch(["a", "b", "c", "d"], None, "en")
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 0.4)


class DefaultTransportWiringTest(unittest.TestCase):
    """默认 transport 与 tk 抓取路径的装配。"""

    def test_tkk_fetched_once_from_homepage(self) -> None:
        transport = router(api=api_ok)
        translator = make_translator(transport)
        translator.translate_batch(["a"], None, "en")
        translator.translate_batch(["b"], None, "en")

        home_calls = transport.home_calls()
        self.assertEqual(len(home_calls), 1)
        self.assertEqual(home_calls[0].url, FREE_DOMAIN)

    def test_tkk_fetch_failure_falls_back_to_constant(self) -> None:
        transport = router(api=api_ok, home=lambda call: (500, "nope"))
        translator = make_translator(transport)
        translator.translate_batch(["hello"], None, "en")
        self.assertEqual(
            transport.api_calls()[0].query["tk"], [build_tk("hello", DEFAULT_TKK)]
        )

    def test_httpx_transport_is_default(self) -> None:
        """不注入 transport 时装配的是 httpx 实现（不联网，仅检查类型）。"""
        translator = GoogleWebTranslator()
        self.assertEqual(
            type(translator._transport).__name__, "_HttpXTransport"
        )
        translator.close()

    def test_httpx_timeout_splits_connect_and_read(self) -> None:
        """跨境超时：connect 固定 3s，read 用调用方 timeout。"""
        try:
            import httpx  # noqa: F401 - 仅在有 httpx 时校验
        except ImportError:  # pragma: no cover
            self.skipTest("httpx not installed")

        built = _HttpXTransport._build_timeout(8.0)
        self.assertEqual(built.connect, 3.0)
        self.assertEqual(built.read, 8.0)
        self.assertEqual(built.write, 8.0)
        # 调用方给的超时小于 3s 时，connect 不能超过整体超时
        self.assertEqual(_HttpXTransport._build_timeout(2.0).connect, 2.0)


class ProxyAndLocalAddressTest(unittest.TestCase):
    """P0 回归：``proxy`` 绝不能被静默丢弃；``local_address`` 必须显式不支持。"""

    PROXY = "http://127.0.0.1:7890"

    def setUp(self) -> None:
        try:
            import httpx
        except ImportError:  # pragma: no cover
            self.skipTest("httpx not installed")
        self.httpx = httpx

    def _pool_proxy_url(self, client: Any, url: str) -> str:
        """取某个 URL 实际使用到的代理地址（httpx 内部结构，仅测试使用）。"""
        transport = client._transport_for_url(self.httpx.URL(url))
        pool = getattr(transport, "_pool", None)
        proxy_url = getattr(pool, "_proxy_url", None)
        return str(proxy_url) if proxy_url is not None else ""

    def test_proxy_is_actually_applied(self) -> None:
        """配置的代理必须真的挂到连接池上（不是"静默变直连"）。"""
        transport = _HttpXTransport(proxy=self.PROXY)
        client = transport._get_client()
        try:
            used = self._pool_proxy_url(client, "https://translate.googleapis.com/translate_a/t")
            self.assertIn("7890", used, f"configured proxy not applied, got {used!r}")
            self.assertIn("127.0.0.1", used)
        finally:
            transport.close()

    def test_proxy_survives_local_address(self) -> None:
        """P0 复现用例：proxy + local_address 同时传时，proxy 仍必须生效。

        修复前：``local_address`` 触发 TypeError → except 分支把 **proxy 一起
        pop 掉** → 静默失效。更阴险的是：本机若有 ``HTTP_PROXY`` 环境变量，
        失效表现**不是**直连超时，而是"悄悄走了另一个代理"（实测配 7890
        却走到了环境变量里的 53476），排查极难。

        断言三件事：① ``local_address`` 显式抛 NotImplementedError（不静默）；
        ② 不传 local_address 时 proxy 真的挂到了连接池上（``_mounts`` 有代理
        挂载 + 实际 ``_proxy_url`` 等于配置值）；③ 公共入口也同样拒绝。
        """
        with self.assertRaises(NotImplementedError) as ctx:
            _HttpXTransport(proxy=self.PROXY, local_address="192.168.1.10")
        self.assertIn("local_address", str(ctx.exception))
        self.assertIn("httpx", str(ctx.exception))

        # proxy 单独工作时必须真的生效
        transport = _HttpXTransport(proxy=self.PROXY)
        client = transport._get_client()
        try:
            mouts = getattr(client, "_mounts", {})
            self.assertTrue(
                any(mount is not None for mount in mouts.values()),
                "no proxy mount in client._mounts",
            )
            self.assertIn(
                "7890",
                self._pool_proxy_url(client, "https://translate.google.com/"),
            )
        finally:
            transport.close()

        # 公共入口：不允许"收下但不生效"
        with self.assertRaises(NotImplementedError):
            GoogleWebTranslator(proxy=self.PROXY, local_address="192.168.1.10")

    def test_proxy_kwarg_is_resolved_from_signature(self) -> None:
        """代理关键字名按 httpx 版本探测，不硬编码。"""
        keyword = _HttpXTransport._resolve_proxy_kwarg()
        self.assertIn(keyword, ("proxy", "proxies"))
        parameters = inspect.signature(self.httpx.Client.__init__).parameters
        self.assertIn(keyword, parameters)

    def test_translator_rejects_local_address(self) -> None:
        """公共入口也要显式拒绝，不能"收下但不生效"。"""
        with self.assertRaises(NotImplementedError):
            GoogleWebTranslator(local_address="192.168.1.10")
        # 即使注入了自定义 transport 也不允许静默忽略
        with self.assertRaises(NotImplementedError):
            GoogleWebTranslator(local_address="192.168.1.10", transport=FakeTransport())

    def test_translator_accepts_proxy(self) -> None:
        translator = GoogleWebTranslator(proxy=self.PROXY)
        try:
            self.assertEqual(translator._transport._proxy, self.PROXY)
            self.assertEqual(translator._transport._proxy_kwarg, "proxy")
        finally:
            translator.close()


class WarmupAndTkkBackoffTest(unittest.TestCase):
    """P1：TKK 抓取不能每请求重试；提供 warmup 挪走首图延迟。"""

    def test_warmup_prefetches_tkk(self) -> None:
        transport = router(api=api_ok)
        translator = make_translator(transport)

        tkk = translator.warmup()

        self.assertEqual(tkk, DEFAULT_TKK)
        self.assertEqual(len(transport.home_calls()), 1)
        # 预热后第一次翻译不再抓首页
        translator.translate_batch(["a"], None, "en")
        self.assertEqual(len(transport.home_calls()), 1)

    def test_warmup_survives_unreachable_network(self) -> None:
        transport = router(api=api_ok, home=lambda call: (0, ""))
        translator = make_translator(transport)
        self.assertEqual(translator.warmup(), DEFAULT_TKK)

    def test_warmup_survives_transport_exception(self) -> None:
        def home(call: Call) -> tuple[int, str]:
            raise OSError("connect timeout")

        transport = router(api=api_ok, home=home)
        translator = make_translator(transport)
        self.assertEqual(translator.warmup(), DEFAULT_TKK)

    def test_tkk_failure_backs_off_across_requests(self) -> None:
        """抓取失败后不能在后续每个请求上重试，必须走递增退避。"""
        transport = router(api=api_ok, home=lambda call: (500, "nope"))
        translator = make_translator(transport)

        for _ in range(5):
            translator.translate_batch(["a"], None, "en")

        # 5 次翻译只应产生 1 次首页抓取（其余都被退避窗口挡住）
        self.assertEqual(len(transport.home_calls()), 1)
        self.assertGreaterEqual(translator._tkk._failure_delay(), 60.0)


class CircuitBreakerTest(unittest.TestCase):
    """熔断器：连续 403/429 打开；打开期间直接回原文；半开只探测一次。"""

    def test_thresholds(self) -> None:
        """连续 20 次 403 或 15 次 429 触发熔断（第 19/14 次不触发）。"""
        breaker = _CircuitBreaker(threshold_403=20, threshold_429=15, open_seconds=60.0)
        for _ in range(19):
            breaker.record_status(403)
        self.assertFalse(breaker.is_open)
        breaker.record_status(403)
        self.assertTrue(breaker.is_open)

        other = _CircuitBreaker(threshold_403=20, threshold_429=15, open_seconds=60.0)
        for _ in range(14):
            other.record_status(429)
        self.assertFalse(other.is_open)
        other.record_status(429)
        self.assertTrue(other.is_open)

    def test_success_resets_counters(self) -> None:
        """非 403/429 的响应清零连续计数（"连续"语义）。"""
        breaker = _CircuitBreaker(threshold_403=3, threshold_429=3, open_seconds=60.0)
        breaker.record_status(403)
        breaker.record_status(403)
        breaker.record_status(200)
        breaker.record_status(403)
        breaker.record_status(403)
        self.assertFalse(breaker.is_open)

    def test_blocked_while_open_then_half_open_single_probe(self) -> None:
        """打开期间全部拒绝；到期后只放行 1 个探测请求。"""
        breaker = _CircuitBreaker(threshold_403=1, threshold_429=1, open_seconds=60.0)
        breaker.record_status(429)
        self.assertTrue(breaker.is_open)
        self.assertFalse(breaker.allow())

        breaker._open_until = 0.0  # 模拟 60s 到期
        self.assertTrue(breaker.allow())  # 探测放行
        self.assertFalse(breaker.allow())  # 并发第二个请求被挡

        breaker.record_status(200)  # 探测成功 → 闭合
        self.assertFalse(breaker.is_open)
        self.assertTrue(breaker.allow())

    def test_probe_failure_reopens(self) -> None:
        """探测失败要重新打开并重新计时。"""
        breaker = _CircuitBreaker(threshold_403=1, threshold_429=1, open_seconds=60.0)
        breaker.record_status(429)
        breaker._open_until = 0.0
        self.assertTrue(breaker.allow())
        breaker.record_status(429)
        self.assertTrue(breaker.is_open)
        self.assertEqual(breaker.trip_count, 2)

    def test_probe_transport_exception_reopens(self) -> None:
        breaker = _CircuitBreaker(threshold_403=1, threshold_429=1, open_seconds=60.0)
        breaker.record_status(403)
        breaker._open_until = 0.0
        self.assertTrue(breaker.allow())
        breaker.record_exception()
        self.assertTrue(breaker.is_open)

    def test_open_circuit_returns_originals_without_network(self) -> None:
        """打开期间：不发请求、不抛异常、原样返回（绝不阻塞出图）。"""
        transport = router(
            api=lambda call: (429, "slow down"),
            free=lambda call: (429, "slow down"),
        )
        translator = make_translator(transport, max_items_per_request=1)
        translator.MAX_ATTEMPTS = 1
        translator._breaker._threshold_429 = 2
        texts = ["a", "b", "c"]

        result = translator.translate_batch(texts, None, "en")

        self.assertEqual(result, texts)
        self.assertTrue(translator.circuit_open)
        # 只有第 1 批真的打了网络（两次 429 触发熔断），后续批次零请求
        self.assertEqual(len(transport.api_calls()), 2)

    def test_open_circuit_logs_error_with_marker(self) -> None:
        """熔断必须打 error 级日志且带 ``circuit_open`` 标记，便于告警规则匹配。"""
        transport = router(
            api=lambda call: (429, "slow down"),
            free=lambda call: (429, "slow down"),
        )
        translator = make_translator(transport, max_items_per_request=1)
        translator.MAX_ATTEMPTS = 1
        translator._breaker._threshold_429 = 1

        with self.assertLogs("imgtrans.translator.google_web", level="ERROR") as ctx:
            translator.translate_batch(["a", "b"], None, "en")

        self.assertTrue(any("circuit_open" in line for line in ctx.output))
        self.assertTrue(any("returning originals" in line for line in ctx.output))

    def test_circuit_properties_for_monitoring(self) -> None:
        translator = make_translator(router())
        self.assertFalse(translator.circuit_open)
        self.assertEqual(translator.circuit_trip_count, 0)

    # ---- 以下 3 项按主理人指定的验收场景实现 ------------------------- #

    def test_twenty_consecutive_403_opens_circuit_and_blocks_next_batch(self) -> None:
        """验收场景 1：连续 20×403 → 熔断 → 下一批不再发请求、直接回原文。"""
        transport = router(
            api=lambda call: (403, "forbidden"),
            free=lambda call: (403, "forbidden"),
        )
        translator = make_translator(transport, max_items_per_request=1)
        translator.MAX_ATTEMPTS = 1

        # 每次 translate_batch(["t"]) 产生 1 批：api 初次 + 刷新 TKK 后重试，
        # 再加 free 降级，共 3 个 403 响应。跑到熔断为止（有上限防空转）。
        batches = 0
        while not translator.circuit_open and batches < 50:
            translator.translate_batch(["t"], None, "en")
            batches += 1

        self.assertTrue(translator.circuit_open, "circuit should open after 20 consecutive 403")
        self.assertEqual(translator.circuit_trip_count, 1)
        self.assertGreaterEqual(batches, 7)

        calls_at_open = len(transport.calls)
        result = translator.translate_batch(["hello"], None, "en")

        self.assertEqual(result, ["hello"], "must return originals while open")
        self.assertEqual(
            len(transport.calls), calls_at_open, "no request may be sent while circuit is open"
        )

    def test_half_open_after_open_seconds_with_injected_clock(self) -> None:
        """验收场景 2：60s 后（注入时钟，不真 sleep）放 1 个探测，成功则闭合并清零。"""
        now = [1000.0]
        breaker = _CircuitBreaker(
            threshold_403=20,
            threshold_429=15,
            open_seconds=60.0,
            clock=lambda: now[0],
        )

        for _ in range(20):
            breaker.record_status(403)
        self.assertTrue(breaker.is_open)
        self.assertFalse(breaker.allow())

        now[0] += 59.0
        self.assertFalse(breaker.allow(), "still open before 60s elapsed")
        now[0] += 1.0
        self.assertTrue(breaker.allow(), "first request after 60s must be the probe")
        self.assertFalse(breaker.allow(), "only 1 probe allowed while half-open")

        breaker.record_status(200)  # 探测成功
        self.assertFalse(breaker.is_open)
        self.assertEqual(breaker._consecutive_403, 0, "counters must be cleared on close")
        self.assertTrue(breaker.allow(), "traffic resumes after close")

    def test_success_clears_consecutive_counters_no_false_trip(self) -> None:
        """验收场景 3：中间夹一次成功 → 计数清零，不因历史累计误触发。

        历史累计 19 + 19 = 38 次 403，但连续最多只有 19 次，因此**不能**熔断。
        """
        transport = router(api=lambda call: (403, "forbidden"), free=free_ok)
        translator = make_translator(transport, max_items_per_request=1)
        translator.MAX_ATTEMPTS = 1
        breaker = translator._breaker

        for _ in range(19):
            breaker.record_status(403)
        breaker.record_status(200)  # 一次成功
        for _ in range(19):
            breaker.record_status(403)

        self.assertFalse(
            translator.circuit_open,
            "cumulative failures must not trip the breaker; only consecutive ones count",
        )
        # 再补 1 次才真正连成 20
        breaker.record_status(403)
        self.assertTrue(translator.circuit_open)


class CircuitRecoveryTest(unittest.TestCase):
    """熔断打开 → 恢复的端到端路径（注入假时钟，不真 sleep）。"""

    def test_recovers_and_translates_after_probe_succeeds(self) -> None:
        state = {"fail": True}
        now = [1000.0]

        def api(call: Call) -> tuple[int, str]:
            return (403, "forbidden") if state["fail"] else api_ok(call)

        transport = router(api=api, free=lambda call: (403, "forbidden"))
        translator = make_translator(transport, max_items_per_request=1)
        translator.MAX_ATTEMPTS = 1
        translator._breaker = _CircuitBreaker(
            threshold_403=2, threshold_429=2, open_seconds=60.0, clock=lambda: now[0]
        )

        translator.translate_batch(["x"], None, "en")  # 2 次 403 → 熔断
        self.assertTrue(translator.circuit_open)
        self.assertEqual(translator.translate_batch(["y"], None, "en"), ["y"])

        state["fail"] = False
        now[0] += 61.0  # 60s 到期 → 半开放行探测

        self.assertEqual(translator.translate_batch(["Hello"], None, "en"), ["译:Hello"])
        self.assertFalse(translator.circuit_open)
        self.assertEqual(translator.translate_batch(["World"], None, "en"), ["译:World"])

    def test_open_window_is_not_extended_by_further_failures(self) -> None:
        """已打开时继续收到 403 不应重复触发/延长窗口（防日志刷爆与 trip_count 虚高）。"""
        now = [1000.0]
        breaker = _CircuitBreaker(
            threshold_403=2, threshold_429=2, open_seconds=60.0, clock=lambda: now[0]
        )
        breaker.record_status(403)
        breaker.record_status(403)
        self.assertEqual(breaker.trip_count, 1)

        for _ in range(50):
            breaker.record_status(403)
        self.assertEqual(breaker.trip_count, 1, "open circuit must not re-trip")

        now[0] += 61.0
        self.assertTrue(breaker.allow(), "half-open probe still allowed after 60s")



class RetryPolicyTest(unittest.TestCase):
    """退避序列与 429 惩罚。"""

    def test_backoff_sequence(self) -> None:
        """退避序列对齐设计文档：0.5 / 1.0 / 2.0（抖动置 0 时）。"""
        translator = make_translator(router())
        translator.BACKOFF_BASE = 0.5
        translator.BACKOFF_JITTER = 0.0
        with mock.patch("time.sleep"):
            delays = [translator._sleep_backoff(attempt) for attempt in range(4)]
        self.assertEqual(delays, [0.5, 1.0, 2.0, 4.0])

    def test_429_retry_delays_follow_sequence(self) -> None:
        """T5：429 的实际睡眠序列必须是 0.5 → 1.0（且每次只睡一次）。"""
        transport = router(api=lambda call: (429, "slow down"), free=free_ok)
        translator = make_translator(transport)
        translator.BACKOFF_BASE = 0.5
        translator.BACKOFF_JITTER = 0.0
        translator.RATE_LIMIT_PENALTY = 0.0

        with mock.patch("time.sleep") as sleeper:
            translator.translate_batch(["a"], None, "en")

        slept = [call.args[0] for call in sleeper.call_args_list if call.args]
        slept = [value for value in slept if isinstance(value, (int, float)) and value > 0]
        # 初次 3 次尝试 → 0.5 / 1.0；刷新 TKK 后重试 3 次 → 0.5 / 1.0
        self.assertEqual(slept, [0.5, 1.0, 0.5, 1.0])

    def test_429_penalty_is_applied_to_bucket(self) -> None:
        """429/503 要在令牌桶上留下冷却（额外降速）。"""
        transport = router(api=lambda call: (429, "slow down"), free=free_ok)
        translator = make_translator(transport)
        translator.RATE_LIMIT_PENALTY = 5.0

        with mock.patch("time.sleep"):
            translator.translate_batch(["a"], None, "en")

        self.assertGreater(translator._bucket._not_before, 0.0)


class TkkFetchDisabledTest(unittest.TestCase):
    """生产默认 ``fetch_tkk=False``：一次首页请求都不发。

    背景：2026-11 实测 Google 翻译首页（3.1MB）已经完全不含 ``tkk`` 变量，
    每次抓取只会白下几 MB 并白等一次 connect 超时（3s）。
    而内置常量 ``406398.2087938574`` 仍被服务端接受（已实测 200）。
    所以默认关掉抓取；开关保留，万一 Google 恢复首页变量可以立刻打开。
    """

    def test_no_homepage_request_by_default(self) -> None:
        transport = router(api=api_ok)
        translator = GoogleWebTranslator(qps=0.0, timeout=2.0, transport=transport)

        translator.translate_batch(["hello"], None, "zh-CN")

        self.assertEqual(len(transport.home_calls()), 0, "默认不应抓取首页")

    def test_uses_builtin_tkk_when_fetch_disabled(self) -> None:
        transport = router(api=api_ok)
        translator = GoogleWebTranslator(qps=0.0, timeout=2.0, transport=transport)

        self.assertEqual(translator.warmup(), DEFAULT_TKK)
        self.assertEqual(len(transport.home_calls()), 0)

    def test_403_with_unchanged_tkk_skips_retry(self) -> None:
        """签名刷不出来时，重试必然是同样的 403，直接降级更划算。"""
        transport = router(api=lambda call: (403, "forbidden"), free=free_ok)
        translator = GoogleWebTranslator(qps=0.0, timeout=2.0, transport=transport)

        result = translator.translate_batch(["Hello"], "en", "zh-CN")

        self.assertEqual(result, ["译:Hello"], "应降级到 free 通道")
        self.assertEqual(len(transport.api_calls()), 1, "不应盲目重试第二次 api")
        self.assertEqual(len(transport.home_calls()), 0)


if __name__ == "__main__":
    unittest.main()
