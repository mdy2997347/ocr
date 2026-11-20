"""``BaiduTranslator`` 的单测（全部注入假 transport，不联网）。

覆盖点
------

* ``sign`` 用百度官方文档示例的已知向量校验（``f89f9594…``）；
* 请求形态：``POST`` + ``form-urlencoded`` + 六个必填字段齐全；
* **多段拼成一次请求**（不是逐段发）——这是本通道的立身之本；
* 按 ``\\n`` 分行返回后的 1:1 对齐；
* 段内嵌换行时按行展开再拼回；
* 行数对不上 → 整批保留原文（绝不把 A 段译文画到 B 段上）；
* 错误码分流：``52001`` 重试、``54003`` 降速重试、``54001`` 不重试且致命、
  连续 3 次致命 → 熔断（之后不发请求、``last_error`` 有值）；
* 按**字节**切批（中文 3 字节/字，按字符切会超限）；
* 缺凭证在构造期就抛 ``ValueError``；
* ``repr`` / 错误信息里不出现密钥。
"""

from __future__ import annotations

import hashlib
import json
import logging
import unittest
from dataclasses import dataclass, field
from typing import Any
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from imgtrans.translator.baidu import (
    TRANSLATE_URL,
    BaiduTranslator,
    _FatalCircuit,
    make_sign,
)
from imgtrans.translator.lang_map import SUPPORTED_LANGS, to_baidu_lang

APPID = "2015063000000001"
SECRET = "12345678"


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
    def form(self) -> dict[str, str]:
        """请求体表单（百度只发单值，取第一个即可）。"""
        parsed = parse_qs((self.data or b"").decode("utf-8"), keep_blank_values=True)
        return {key: values[0] for key, values in parsed.items()}

    @property
    def q(self) -> str:
        """请求体里的 ``q``。"""
        return self.form.get("q", "")

    @property
    def path(self) -> str:
        """URL 路径。"""
        return urlsplit(self.url).path


@dataclass
class FakeTransport:
    """可编排的假传输层。

    ``responses`` 按顺序弹出；弹空后重复最后一项。元素可以是
    ``(status, text)`` 二元组，也可以是异常实例（表示传输层失败）。
    """

    responses: list[Any] = field(default_factory=list)
    calls: list[Call] = field(default_factory=list)

    def __call__(
        self,
        method: str,
        url: str,
        params: dict[str, Any] | None,
        headers: dict[str, str] | None,
        data: bytes | None,
        timeout: float,
    ) -> tuple[int, str]:
        self.calls.append(Call(method, url, params, headers, data, timeout))
        item = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(item, BaseException):
            raise item
        return item


def ok(*pairs: tuple[str, str]) -> tuple[int, str]:
    """构造一个成功的百度响应。"""
    return 200, json.dumps(
        {
            "from": "zh",
            "to": "en",
            "trans_result": [{"src": src, "dst": dst} for src, dst in pairs],
        },
        ensure_ascii=False,
    )


def err(code: str, msg: str = "boom") -> tuple[int, str]:
    """构造一个业务错误响应。"""
    return 200, json.dumps({"error_code": code, "error_msg": msg})


def make(transport: FakeTransport, **kwargs: Any) -> BaiduTranslator:
    """建一个不限流、固定 salt 的翻译器（测试默认配置）。"""
    params: dict[str, Any] = {
        "appid": APPID,
        "secret": SECRET,
        "transport": transport,
        "salt_factory": lambda: "1435660288",
        "qps": 0,
    }
    params.update(kwargs)
    return BaiduTranslator(**params)


class TestSign(unittest.TestCase):
    """签名算法。"""

    def test_official_vector(self) -> None:
        """百度官方文档示例：appid+apple+1435660288+12345678 → f89f9594…"""
        self.assertEqual(
            make_sign("2015063000000001", "apple", "1435660288", "12345678"),
            "f89f9594663708c1605f3d736d01d2d4",
        )

    def test_matches_plain_md5(self) -> None:
        """实现必须是"四段直接拼接后 MD5"，不能偷偷加分隔符。"""
        raw = f"{APPID}你好世界999{SECRET}"
        self.assertEqual(
            make_sign(APPID, "你好世界", "999", SECRET),
            hashlib.md5(raw.encode("utf-8")).hexdigest(),
        )

    def test_newline_is_signed_as_is(self) -> None:
        """多段拼接后的 ``q`` 里含 ``\\n``，签名要对原文算，不能先编码。"""
        q = "第一行\n第二行"
        raw = f"{APPID}{q}77{SECRET}"
        self.assertEqual(
            make_sign(APPID, q, "77", SECRET),
            hashlib.md5(raw.encode("utf-8")).hexdigest(),
        )


class TestRequestShape(unittest.TestCase):
    """请求形态与单次批量。"""

    def test_single_request_for_whole_batch(self) -> None:
        """40 段只发 1 次请求，q 用 ``\\n`` 连接。"""
        texts = [f"第{i}行" for i in range(40)]
        transport = FakeTransport([ok(*[(t, f"L{i}") for i, t in enumerate(texts)])])
        translator = make(transport)

        out = translator.translate_batch(texts, None, "en")

        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(transport.calls[0].q, "\n".join(texts))
        self.assertEqual(out, [f"L{i}" for i in range(40)])

    def test_form_fields(self) -> None:
        """六个必填字段齐全，且 sign 自洽。"""
        transport = FakeTransport([ok(("你好", "Hello"))])
        translator = make(transport)

        translator.translate_batch(["你好"], "zh", "en")

        call = transport.calls[0]
        self.assertEqual(call.method, "POST")
        self.assertEqual(call.path, "/api/trans/vip/translate")
        self.assertEqual(
            call.headers,
            {"Content-Type": "application/x-www-form-urlencoded"},
        )
        form = call.form
        self.assertEqual(form["from"], "zh")
        self.assertEqual(form["to"], "en")
        self.assertEqual(form["appid"], APPID)
        self.assertEqual(form["salt"], "1435660288")
        self.assertEqual(form["sign"], make_sign(APPID, "你好", "1435660288", SECRET))

    def test_auto_source(self) -> None:
        """``source=None`` → ``from=auto``。"""
        transport = FakeTransport([ok(("你好", "Hello"))])
        make(transport).translate_batch(["你好"], None, "en")
        self.assertEqual(transport.calls[0].form["from"], "auto")

    def test_target_is_mapped_to_baidu_code(self) -> None:
        """``ja`` 必须变成百度的 ``jp``，否则 58001。"""
        transport = FakeTransport([ok(("你好", "こんにちは"))])
        make(transport).translate_batch(["你好"], "zh", "ja")
        self.assertEqual(transport.calls[0].form["to"], "jp")

    def test_empty_input(self) -> None:
        """空列表不发请求。"""
        transport = FakeTransport([ok()])
        self.assertEqual(make(transport).translate_batch([], None, "en"), [])
        self.assertEqual(transport.calls, [])

    def test_blank_items_skipped(self) -> None:
        """纯空白段不参与翻译，但结果里保留原位。"""
        transport = FakeTransport([ok(("你好", "Hello"))])
        out = make(transport).translate_batch(["你好", "   ", ""], None, "en")
        self.assertEqual(out[0], "Hello")
        self.assertEqual(out[1], "   ")
        self.assertEqual(out[2], "")
        self.assertNotIn("   ", transport.calls[0].q)

    def test_unsupported_target_raises(self) -> None:
        """目标语言不在百度表里 → ValueError（不是静默返回原文）。"""
        transport = FakeTransport([ok()])
        with self.assertRaises(ValueError):
            make(transport).translate_batch(["你好"], None, "klingon")


class TestAlign(unittest.TestCase):
    """回包对齐。"""

    def test_one_to_one(self) -> None:
        """段数与行数相等 → 直接 1:1。"""
        transport = FakeTransport([ok(("A", "a"), ("B", "b"), ("C", "c"))])
        out = make(transport).translate_batch(["A", "B", "C"], "zh", "en")
        self.assertEqual(out, ["a", "b", "c"])

    def test_empty_dst_keeps_original(self) -> None:
        """某行译文为空 → 该位置保留原文。"""
        transport = FakeTransport([ok(("A", ""), ("B", "b"))])
        out = make(transport).translate_batch(["A", "B"], "zh", "en")
        self.assertEqual(out, ["A", "b"])

    def test_embedded_newline_expands(self) -> None:
        """一段内含 ``\\n`` → 百度拆成两行返回，按行拼回同一段。"""
        texts = ["第一行\n第二行", "第三行"]
        transport = FakeTransport(
            [ok(("第一行", "First"), ("第二行", "Second"), ("第三行", "Third"))]
        )
        out = make(transport).translate_batch(texts, "zh", "en")
        self.assertEqual(out, ["First\nSecond", "Third"])

    def test_row_count_mismatch_keeps_originals(self) -> None:
        """行数怎么都对不上 → 整批保留原文，不做任何猜测性映射。"""
        transport = FakeTransport([ok(("A", "a"))])
        out = make(transport).translate_batch(["A", "B", "C"], "zh", "en")
        self.assertEqual(out, ["A", "B", "C"])

    def test_missing_trans_result(self) -> None:
        """没有 ``trans_result`` → 保留原文并记 last_error。"""
        transport = FakeTransport([(200, json.dumps({"from": "zh", "to": "en"}))])
        translator = make(transport)
        out = translator.translate_batch(["A"], "zh", "en")
        self.assertEqual(out, ["A"])
        self.assertIn("trans_result", translator.last_error or "")


class TestErrors(unittest.TestCase):
    """错误码分流。"""

    def setUp(self) -> None:
        """把退避睡眠变 no-op：这里验的是"重试了几次"，不是"退避了多久"。"""
        patcher = mock.patch("imgtrans.translator.baidu.time.sleep")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_retryable_timeout_then_success(self) -> None:
        """``52001`` 请求超时 → 重试一次后成功。"""
        transport = FakeTransport([err("52001"), ok(("A", "a"))])
        out = make(transport).translate_batch(["A"], "zh", "en")
        self.assertEqual(out, ["a"])
        self.assertEqual(len(transport.calls), 2)

    def test_rate_limit_retries_then_success(self) -> None:
        """``54003`` 频率受限 → 重试（并给令牌桶降温）。"""
        transport = FakeTransport([err("54003"), ok(("A", "a"))])
        translator = make(transport)
        out = translator.translate_batch(["A"], "zh", "en")
        self.assertEqual(out, ["a"])
        self.assertEqual(len(transport.calls), 2)

    def test_sign_error_is_fatal_and_not_retried(self) -> None:
        """``54001`` 签名错误重试无意义 → 只发 1 次，last_error 带中文解释。"""
        transport = FakeTransport([err("54001")])
        translator = make(transport)
        out = translator.translate_batch(["A"], "zh", "en")
        self.assertEqual(out, ["A"])
        self.assertEqual(len(transport.calls), 1)
        self.assertIn("54001", translator.last_error or "")
        self.assertIn("签名错误", translator.last_error or "")

    def test_three_fatal_errors_trip_circuit(self) -> None:
        """连续 3 次致命错误 → 第 3 次即熔断，第 4 次不再发请求。"""
        transport = FakeTransport([err("52003")])
        translator = make(transport)

        for _ in range(3):
            translator.translate_batch(["A"], "zh", "en")
        self.assertEqual(len(transport.calls), 3)
        self.assertTrue(translator.circuit_open)
        self.assertEqual(translator.circuit_trip_count, 1)

        out = translator.translate_batch(["A"], "zh", "en")
        self.assertEqual(out, ["A"])
        # 熔断后一次请求都不发
        self.assertEqual(len(transport.calls), 3)
        self.assertIn("熔断", translator.last_error or "")

    def test_last_error_cleared_on_success(self) -> None:
        """上一轮的失败原因不能污染下一轮。"""
        transport = FakeTransport([err("54001"), ok(("A", "a"))])
        translator = make(transport)
        translator.translate_batch(["A"], "zh", "en")
        self.assertIsNotNone(translator.last_error)
        translator.translate_batch(["A"], "zh", "en")
        self.assertIsNone(translator.last_error)

    def test_http_500_retries(self) -> None:
        """HTTP 500 属于链路问题 → 重试。"""
        transport = FakeTransport([(500, "oops"), ok(("A", "a"))])
        out = make(transport).translate_batch(["A"], "zh", "en")
        self.assertEqual(out, ["a"])
        self.assertEqual(len(transport.calls), 2)

    def test_transport_exception_keeps_originals(self) -> None:
        """传输层一直抛异常 → 返回原文 + last_error，绝不向上抛。"""
        transport = FakeTransport([OSError("connection reset")])
        translator = make(transport)
        out = translator.translate_batch(["A", "B"], "zh", "en")
        self.assertEqual(out, ["A", "B"])
        self.assertIn("传输层异常", translator.last_error or "")
        self.assertEqual(len(transport.calls), translator.MAX_ATTEMPTS)

    def test_non_json_payload(self) -> None:
        """返回 HTML（网关错误页）→ 保留原文。"""
        transport = FakeTransport([(200, "<html>502</html>")])
        translator = make(transport)
        out = translator.translate_batch(["A"], "zh", "en")
        self.assertEqual(out, ["A"])
        self.assertIn("非 JSON", translator.last_error or "")


class TestBatching(unittest.TestCase):
    """按字节切批。"""

    def test_split_by_bytes_not_chars(self) -> None:
        """4 个 3 字节段 + 连接符，上限 10 字节 → 每批 2 段。"""
        transport = FakeTransport(
            [ok(("aaa", "1"), ("bbb", "2")), ok(("ccc", "3"), ("ddd", "4"))]
        )
        translator = make(transport, max_bytes_per_request=10)
        out = translator.translate_batch(["aaa", "bbb", "ccc", "ddd"], "zh", "en")
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(transport.calls[0].q, "aaa\nbbb")
        self.assertEqual(transport.calls[1].q, "ccc\nddd")
        self.assertEqual(out, ["1", "2", "3", "4"])

    def test_chinese_counts_three_bytes(self) -> None:
        """两个汉字段（各 6 字节）在 10 字节上限下必须分两批。"""
        transport = FakeTransport([ok(("中文", "cn")), ok(("英文", "en"))])
        translator = make(transport, max_bytes_per_request=10)
        out = translator.translate_batch(["中文", "英文"], "zh", "en")
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(out, ["cn", "en"])

    def test_max_items(self) -> None:
        """条数上限生效。"""
        transport = FakeTransport([ok(("a", "1"), ("b", "2")), ok(("c", "3"))])
        translator = make(transport, max_items_per_request=2)
        translator.translate_batch(["a", "b", "c"], "zh", "en")
        self.assertEqual(len(transport.calls), 2)


class TestConstruction(unittest.TestCase):
    """构造期校验。"""

    def test_missing_appid(self) -> None:
        """缺 APP ID → 启动期就抛，别等第一张图。"""
        with self.assertRaises(ValueError) as ctx:
            BaiduTranslator(appid="", secret=SECRET)
        self.assertIn("IMGTRANS_BAIDU_APPID", str(ctx.exception))

    def test_missing_secret(self) -> None:
        """缺密钥 → 抛，并提示该设哪个变量。"""
        with self.assertRaises(ValueError) as ctx:
            BaiduTranslator(appid=APPID, secret="")
        self.assertIn("IMGTRANS_BAIDU_SECRET", str(ctx.exception))

    def test_repr_hides_secret(self) -> None:
        """``repr`` 只露 APP ID 尾号，密钥一个字都不能出现。"""
        text = repr(BaiduTranslator(appid="2023111100009999", secret=SECRET))
        self.assertNotIn(SECRET, text)
        self.assertIn("9999", text)

    def test_kind_for_healthz(self) -> None:
        """``kind`` 供 ``/healthz`` 展示。"""
        translator = BaiduTranslator(appid=APPID, secret=SECRET)
        self.assertEqual(translator.kind, "baidu")
        self.assertFalse(translator.circuit_open)
        self.assertEqual(translator.circuit_trip_count, 0)

    def test_close_is_safe_twice(self) -> None:
        """重复 close 不抛（进程退出路径）。"""
        translator = BaiduTranslator(appid=APPID, secret=SECRET)
        translator.close()
        translator.close()


class TestCircuitClock(unittest.TestCase):
    """熔断器的时间语义（注入假时钟，不 sleep）。"""

    def test_half_open_after_timeout(self) -> None:
        """到期后放行 1 个探测请求，探测成功即闭合。"""
        now = [0.0]
        circuit = _FatalCircuit(threshold=1, open_seconds=60.0, clock=lambda: now[0])
        self.assertTrue(circuit.allow())
        circuit.record_failure(fatal=True)
        self.assertTrue(circuit.is_open)
        self.assertFalse(circuit.allow())

        now[0] = 61.0
        self.assertTrue(circuit.allow())  # 半开，放行探测
        self.assertFalse(circuit.allow())  # 探测在飞，其余拦截
        circuit.record_success()
        self.assertFalse(circuit.is_open)
        self.assertTrue(circuit.allow())

    def test_non_fatal_failure_does_not_trip(self) -> None:
        """网络抖动不触发熔断。"""
        circuit = _FatalCircuit(threshold=2, open_seconds=60.0)
        circuit.record_failure(fatal=False)
        circuit.record_failure(fatal=False)
        self.assertFalse(circuit.is_open)


class TestLangMap(unittest.TestCase):
    """语言映射表。"""

    def test_every_supported_lang_maps(self) -> None:
        """对外承诺的 35 个目标语言，百度那边必须全都能映射。"""
        missing = [code for code in SUPPORTED_LANGS if not to_baidu_lang(code)]
        self.assertEqual(missing, [])

    def test_project_codes_differ_from_iso(self) -> None:
        """几个最容易写错的：ja→jp、ko→kor、fr→fra、es→spa、he→heb。"""
        self.assertEqual(to_baidu_lang("ja"), "jp")
        self.assertEqual(to_baidu_lang("ko"), "kor")
        self.assertEqual(to_baidu_lang("fr"), "fra")
        self.assertEqual(to_baidu_lang("es"), "spa")
        self.assertEqual(to_baidu_lang("he"), "heb")

    def test_chinese_and_auto(self) -> None:
        """中文只作源语言；繁体是 cht 不是 zh-TW。"""
        self.assertEqual(to_baidu_lang("zh"), "zh")
        self.assertEqual(to_baidu_lang("zh-Hans"), "zh")
        self.assertEqual(to_baidu_lang("zh-TW"), "cht")
        self.assertEqual(to_baidu_lang("auto"), "auto")
        self.assertEqual(to_baidu_lang(None), "")

    def test_region_subtag_falls_back(self) -> None:
        """``ja-JP`` / ``EN_us`` 退到主语言子标签。"""
        self.assertEqual(to_baidu_lang("ja-JP"), "jp")
        self.assertEqual(to_baidu_lang("EN_us"), "en")


class TestBuildTranslator(unittest.TestCase):
    """工厂接线。"""

    def test_build_baidu(self) -> None:
        """``build_translator("baidu", …)`` 能建出通道。"""
        from imgtrans.translator import build_translator

        translator = build_translator(
            "baidu", baidu_appid=APPID, baidu_secret=SECRET, baidu_qps=2.0
        )
        self.assertIsInstance(translator, BaiduTranslator)
        translator.close()

    def test_build_baidu_without_credentials(self) -> None:
        """缺凭证时工厂抛 ValueError（serve.py 据此拒绝启动）。"""
        from imgtrans.translator import build_translator

        with self.assertRaises(ValueError):
            build_translator("baidu")

    def test_endpoint(self) -> None:
        """端点必须是通用文本翻译，不是词典/图片翻译。"""
        self.assertEqual(
            TRANSLATE_URL, "https://fanyi-api.baidu.com/api/trans/vip/translate"
        )


if __name__ == "__main__":
    unittest.main()
