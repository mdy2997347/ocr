"""``tk`` 令牌算法的验证。

三层验证：

1. **交叉验证（最关键）**：用 node 跑 ``tools/tk_reference.js``（Google 官方
   前端 JS 算法的参考实现），与 Python 的 :func:`build_tk` 逐条严格比对。
2. **回归向量**：把交叉验证得到的值硬编码，防止两边同时被改坏。
3. **边界与单元测试**：空串、超长混合文本、非法 TKK、``RL``/``shr32`` 语义、
   ``TKKSnapshot`` 的 TTL 与失败回落。
"""

from __future__ import annotations

import json
import logging
import random
import shutil
import subprocess
import unittest
from pathlib import Path

from imgtrans.translator.google_tk import (
    DEFAULT_TKK,
    TKKSnapshot,
    _rl,
    _shr32,
    _utf8_bytes,
    build_tk,
    dec_tkk,
    fetch_tkk,
)

ROOT = Path(__file__).resolve().parents[1]
NODE_SCRIPT = ROOT / "tools" / "tk_reference.js"


def setUpModule() -> None:
    """静音库内日志，保持 ``-v`` 输出可读（``assertLogs`` 会自行接管级别）。"""
    logging.getLogger("imgtrans").setLevel(logging.CRITICAL)

#: 500 字混合文本（CJK + 拉丁 + 日文 + emoji），确定性构造。
_LONG_BASE = "多语言图片翻译 image translation テスト OK 😀🎉 "
LONG_TEXT = (_LONG_BASE * 40)[:500]

#: 交叉验证用例。
CASES: list[str] = [
    "",
    "a",
    "hello",
    "Hello, World!",
    "你好",
    "中文测试 image",
    "😀emoji🎉",
    LONG_TEXT,
]


def build_golden_inputs() -> list[str]:
    """构造 200 条确定性 golden 样本（去重后仍为 200 条）。

    覆盖：空串、单字符、emoji/astral、组合 emoji、各语言、URL 元字符
    （``&``/``+``/``%``）、空格/换行/制表/NBSP、500/1000/5000 字符长文本。
    """
    fixed: list[str] = [
        "",
        "a",
        "A",
        "0",
        " ",
        "\n",
        "\t",
        "\r\n",
        "\u00a0",  # NBSP
        "a\u00a0b",
        "&",
        "+",
        "%",
        "&amp;",
        "&#039;",
        "100%",
        "C++",
        "a+b",
        "a&b=c",
        "?q=1&r=2",
        "a b",
        "a\nb",
        "a\tb",
        "你好",
        "你好 世界",
        "😀",
        "😀🎉",
        "🇨🇳",
        "👨‍👩‍👧‍👦",  # ZWJ 序列
        "™",
        "Ünïcödé",
        "日本語テスト",
        "한국어",
        "العربية",
        "עברית",
        "Ελληνικά",
        "Русский",
        "Hello, World!",
        "iPhone 15 Pro",
        "Hello WORLD",
        "Google 翻译",
        "x" * 500,
        "y" * 1000,
        "z" * 5000,
        ("多语言 image 😀 " * 60)[:1000],
        "😀" * 100,
    ]
    alphabet = "abzAZ09 .,!?&+%_-/\\\"'<>=@#()[]{}你日本語😀🎉éÜ\t\n\u00a0"
    rng = random.Random(20240101)
    cases = list(fixed)
    seen = set(cases)
    while len(cases) < 200:
        length = rng.choice([1, 2, 3, 5, 8, 13, 21, 34, 55, 100, 300])
        candidate = "".join(rng.choice(alphabet) for _ in range(length))
        if candidate in seen:
            continue
        seen.add(candidate)
        cases.append(candidate)
    return cases


def run_node_reference(inputs: list[str]) -> dict[str, str]:
    """用 node 跑官方 JS 参考实现，返回 ``{input: tk}``。

    Raises:
        unittest.SkipTest: 环境缺 node 或参考脚本。
        AssertionError: 参考脚本执行失败。
    """
    node = shutil.which("node")
    if not node:
        raise unittest.SkipTest("node not available, skip cross validation")
    if not NODE_SCRIPT.is_file():
        raise unittest.SkipTest(f"reference script missing: {NODE_SCRIPT}")

    payload = json.dumps(inputs, ensure_ascii=False).encode("utf-8")
    completed = subprocess.run(  # noqa: S603 - 固定二进制与本地脚本
        [node, str(NODE_SCRIPT)],
        input=payload,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=120,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(
            "node reference failed: "
            f"rc={completed.returncode} stderr={completed.stderr.decode('utf-8', 'replace')}"
        )
    return json.loads(completed.stdout.decode("utf-8"))


class NodeCrossValidationTest(unittest.TestCase):
    """Python 移植版与官方 JS 参考实现的逐条严格比对。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.expected = run_node_reference(CASES)

    def test_node_returns_all_cases(self) -> None:
        """参考实现覆盖了所有用例。"""
        self.assertEqual(len(self.expected), len(CASES))

    def test_build_tk_matches_node(self) -> None:
        """逐条严格比对：任何一个字符不同都算移植错误。"""
        mismatches: list[str] = []
        for text in CASES:
            actual = build_tk(text)
            expected = self.expected[text]
            if actual != expected:
                mismatches.append(f"{text!r}: python={actual} node={expected}")
        self.assertEqual(
            mismatches,
            [],
            "tk mismatch between python port and node reference:\n" + "\n".join(mismatches),
        )

    def test_astral_characters_encoded_as_four_bytes(self) -> None:
        """emoji 走四字节 UTF-8 分支（与 JS 代理对分支一致）。"""
        self.assertEqual(_utf8_bytes("😀"), [0xF0, 0x9F, 0x98, 0x80])
        self.assertEqual(_utf8_bytes("é"), [0xC3, 0xA9])
        self.assertEqual(_utf8_bytes("中"), [0xE4, 0xB8, 0xAD])

    def test_prefix_stability(self) -> None:
        """同一前缀的 tk 计算不依赖长度（防止循环变量复用出错）。"""
        self.assertEqual(build_tk("abc"), build_tk("abc"))
        self.assertNotEqual(build_tk("abc"), build_tk("abd"))


class GoldenSampleTest(unittest.TestCase):
    """T1：200 条 golden 样本与 node 参考实现逐条比对（设计文档 §9.4.3）。"""

    def test_two_hundred_golden_samples(self) -> None:
        inputs = build_golden_inputs()
        self.assertEqual(len(inputs), 200)
        self.assertEqual(len(set(inputs)), 200, "golden 样本必须互不相重")

        expected = run_node_reference(inputs)
        self.assertEqual(len(expected), 200)

        mismatches: list[str] = []
        for text in inputs:
            actual = build_tk(text)
            if actual != expected[text]:
                preview = text if len(text) <= 32 else f"{text[:32]}...(len={len(text)})"
                mismatches.append(f"{preview!r}: python={actual} node={expected[text]}")
        self.assertEqual(
            mismatches,
            [],
            f"{len(mismatches)}/200 tk mismatch:\n" + "\n".join(mismatches[:10]),
        )

    def test_golden_lengths_are_covered(self) -> None:
        """确认长文本用例真的进来了（500/1000/5000）。"""
        lengths = {len(text) for text in build_golden_inputs()}
        for expected_length in (0, 1, 500, 1000, 5000):
            self.assertIn(expected_length, lengths)


class KnownVectorTest(unittest.TestCase):
    """回归向量：由 node 参考实现生成后硬编码，防止"两边同时改坏"。"""

    VECTORS: dict[str, str] = {
        "": "263193.145255",
        "a": "347446.228936",
        "hello": "338590.203232",
        "Hello, World!": "563463.961145",
        "你好": "916876.773874",
        "中文测试 image": "369030.234232",
        "😀emoji🎉": "571082.951732",
    }

    def test_known_vectors(self) -> None:
        for text, expected in self.VECTORS.items():
            with self.subTest(text=text):
                self.assertEqual(build_tk(text), expected)

    def test_vector_count(self) -> None:
        self.assertEqual(len(self.VECTORS), 7)


class TkFormatTest(unittest.TestCase):
    """令牌格式与入参处理。"""

    def test_format(self) -> None:
        tk = build_tk("hello")
        head, _, tail = tk.partition(".")
        self.assertEqual(tk, f"{head}.{tail}")
        self.assertTrue(head.isdigit())
        self.assertTrue(tail.isdigit())
        self.assertLess(int(head), 1_000_000)
        self.assertEqual(int(tail), int(head) ^ 406398)

    def test_default_tkk_constant(self) -> None:
        self.assertEqual(DEFAULT_TKK, "406398.2087938574")
        # PHP: 406398 . "." . (561666268 + 1526272306)
        self.assertEqual(561666268 + 1526272306, 2087938574)
        self.assertEqual(dec_tkk(DEFAULT_TKK), (406398, 2087938574))

    def test_custom_tkk_changes_result(self) -> None:
        self.assertNotEqual(build_tk("hello"), build_tk("hello", "406398.123"))

    def test_invalid_tkk_raises(self) -> None:
        for bad in ("", "abc", "1.2.3", "1.", ".2", "a.b"):
            with self.subTest(tkk=bad):
                with self.assertRaises(ValueError):
                    build_tk("hello", bad)

    def test_empty_and_whitespace(self) -> None:
        self.assertEqual(build_tk(""), "263193.145255")
        self.assertNotEqual(build_tk(" "), build_tk(""))
        self.assertNotEqual(build_tk("\n"), build_tk(""))

    def test_long_text_is_deterministic(self) -> None:
        self.assertEqual(len(LONG_TEXT), 500)
        self.assertEqual(build_tk(LONG_TEXT), build_tk(LONG_TEXT))


class PrimitiveTest(unittest.TestCase):
    """``shr32`` / ``_rl`` / ``_utf8_bytes`` 的语义测试。"""

    def test_shr32_boundaries(self) -> None:
        self.assertEqual(_shr32(0xFFFFFFFF, 0), 0xFFFFFFFF)
        self.assertEqual(_shr32(0xFFFFFFFF, 32), 0)
        self.assertEqual(_shr32(0xFFFFFFFF, 40), 0)
        self.assertEqual(_shr32(0xFFFFFFFF, 1), 0x7FFFFFFF)
        self.assertEqual(_shr32(-1, 8), 0x00FFFFFF)

    def test_rl_matches_js_semantics(self) -> None:
        # "+-a^+6" 含两个三元组：
        #   1) a = a + (a << 10)
        #   2) a = a ^ (a >>> 6)
        first = (1 + (1 << 10)) & 0xFFFFFFFF
        self.assertEqual(_rl(1, "+-a^+6"), first ^ (first >> 6))
        # "+-0"：a = a + (a << 0)
        self.assertEqual(_rl(10, "+-0"), 20)
        # "^+0"：a = a ^ (a >>> 0) → 0
        self.assertEqual(_rl(5, "^+0"), 0)
        # 指令串可连续执行（等价于按 3 字符一组重复调用）
        self.assertEqual(_rl(1, "+-a^+6+-a^+6"), _rl(_rl(1, "+-a^+6"), "+-a^+6"))

    def test_utf8_bytes_ascii(self) -> None:
        self.assertEqual(_utf8_bytes("a"), [97])
        self.assertEqual(_utf8_bytes(""), [])
        self.assertEqual(_utf8_bytes("Hello, World!")[:5], [72, 101, 108, 108, 111])


class FetchTkkTest(unittest.TestCase):
    """``fetch_tkk`` / ``TKKSnapshot``。"""

    @staticmethod
    def _transport(status: int, body: str, *, raises: Exception | None = None):
        calls: list[tuple] = []

        def transport(method, url, params, headers, data, timeout):
            calls.append((method, url, params, headers, data, timeout))
            if raises is not None:
                raise raises
            return status, body

        transport.calls = calls  # type: ignore[attr-defined]
        return transport

    def test_fetch_success(self) -> None:
        transport = self._transport(200, "<html>window.TKK='406398.2087938574';</html>")
        self.assertEqual(fetch_tkk(transport), "406398.2087938574")
        self.assertEqual(transport.calls[0][0], "GET")  # type: ignore[attr-defined]

    def test_fetch_patterns(self) -> None:
        for body in (
            "tkk:'123456.789012'",
            'tkk:"123456.789012"',
            "tkk=123456.789012",
            "x={tkk: '123456.789012', other:1}",
        ):
            with self.subTest(body=body):
                transport = self._transport(200, body)
                self.assertEqual(fetch_tkk(transport), "123456.789012")

    def test_fetch_failure_returns_none(self) -> None:
        self.assertIsNone(fetch_tkk(self._transport(500, "err")))
        self.assertIsNone(fetch_tkk(self._transport(200, "<html>no tk here</html>")))
        self.assertIsNone(fetch_tkk(self._transport(200, "")))
        self.assertIsNone(fetch_tkk(self._transport(200, "", raises=OSError("boom"))))

    def test_snapshot_uses_fetched_value(self) -> None:
        transport = self._transport(200, "tkk:'111111.222222'")
        snapshot = TKKSnapshot(transport, ttl=3600)
        self.assertEqual(snapshot.get(), "111111.222222")
        # 命缓存：不再发起请求
        self.assertEqual(len(transport.calls), 1)  # type: ignore[attr-defined]
        self.assertEqual(snapshot.get(), "111111.222222")
        self.assertEqual(len(transport.calls), 1)  # type: ignore[attr-defined]

    def test_snapshot_falls_back_to_constant(self) -> None:
        transport = self._transport(200, "nothing")
        snapshot = TKKSnapshot(transport, ttl=3600)
        self.assertEqual(snapshot.get(), DEFAULT_TKK)

    def test_snapshot_logs_after_repeated_failures(self) -> None:
        """连续抓取失败要告警（第 1 次 error，第 10 次再 error）。"""
        transport = self._transport(500, "err")
        snapshot = TKKSnapshot(transport, ttl=0)
        with self.assertLogs("imgtrans.translator.google_tk", level="ERROR") as ctx:
            for _ in range(10):
                snapshot._retry_at = 0.0  # 跳过退避窗口，强制每次都尝试抓取
                snapshot._fetched_at = None
                snapshot.get()
        self.assertIn("TKK fetch failed 1 time(s) in a row", "\n".join(ctx.output))
        self.assertIn("TKK fetch failed 10 time(s) in a row", "\n".join(ctx.output))
        self.assertEqual(snapshot.value, DEFAULT_TKK)

    def test_snapshot_invalidate_forces_refetch(self) -> None:
        transport = self._transport(200, "tkk:'111111.222222'")
        snapshot = TKKSnapshot(transport, ttl=3600)
        snapshot.get()
        snapshot.invalidate()
        self.assertEqual(snapshot.get(), "111111.222222")
        self.assertEqual(len(transport.calls), 2)  # type: ignore[attr-defined]

    def test_snapshot_ttl_expiry(self) -> None:
        transport = self._transport(200, "tkk:'111111.222222'")
        snapshot = TKKSnapshot(transport, ttl=0)
        snapshot.get()
        snapshot.get()
        self.assertEqual(len(transport.calls), 2)  # type: ignore[attr-defined]


if __name__ == "__main__":
    unittest.main()
