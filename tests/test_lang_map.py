"""语言代码映射的单元测试（``lang_map``）。"""

from __future__ import annotations

import unittest

from imgtrans.translator.lang_map import (
    AUTO_LIST,
    CONVERT_CODES,
    SUPPORTED_LANGS,
    convert_lang,
    convert_lang_code,
    is_supported_lang,
    is_supported_source,
    lookup_auto,
    normalize_lang,
)


class AutoListTest(unittest.TestCase):
    """``AUTO_LIST`` 表内容。"""

    def test_entry_count(self) -> None:
        # 35 个受支持语言码 + 8 条检测器别名（el-*/ms-*/nb/nn/pt-*）
        # + 2 条仅源侧的中文变体
        self.assertEqual(len(AUTO_LIST), 45)

    def test_covers_every_supported_lang(self) -> None:
        """受支持的目标语言必须都能从识别结果映射过来，反之亦然。"""
        for code in SUPPORTED_LANGS:
            self.assertIn(code, AUTO_LIST)
            self.assertTrue(lookup_auto(code))

    def test_vietnamese_is_fixed(self) -> None:
        """原 PHP 表里 ``'vi' => 'iv'`` 是笔误，已修正。"""
        self.assertEqual(AUTO_LIST["vi"], "vi")
        self.assertNotIn("iv", AUTO_LIST.values())

    def test_hebrew_uses_legacy_code(self) -> None:
        self.assertEqual(AUTO_LIST["he"], "iw")

    def test_chinese_variants(self) -> None:
        self.assertEqual(AUTO_LIST["zh-Hans"], "zh-cn")
        self.assertEqual(AUTO_LIST["zh-Hant"], "zh-tw")

    def test_expected_mappings(self) -> None:
        expected = {
            "en": "en",
            "de": "de",
            "el-monoton": "el",
            "el-polyton": "el",
            "ms-arab": "ms",
            "ms-latn": "ms",
            "nb": "no",
            "nn": "no",
            "pt-br": "pt",
            "pt-pt": "pt",
        }
        for key, value in expected.items():
            with self.subTest(key=key):
                self.assertEqual(AUTO_LIST[key], value)

    def test_keys_are_lowercase_or_bcp47(self) -> None:
        for key in AUTO_LIST:
            with self.subTest(key=key):
                self.assertEqual(key, key.strip())
                self.assertTrue(key)


class LookupAutoTest(unittest.TestCase):
    """``lookup_auto`` 大小写无关匹配。"""

    def test_exact_keys(self) -> None:
        self.assertEqual(lookup_auto("en"), "en")
        self.assertEqual(lookup_auto("ja"), "ja")

    def test_case_insensitive(self) -> None:
        # 原 PHP auto() 先 strtolower 再查表，'zh-Hans' 这类混合大小写键永远查不到
        self.assertEqual(lookup_auto("zh-Hans"), "zh-cn")
        self.assertEqual(lookup_auto("ZH-HANS"), "zh-cn")
        self.assertEqual(lookup_auto("zh-hant"), "zh-tw")
        self.assertEqual(lookup_auto("PT-BR"), "pt")

    def test_region_subtag_fallback(self) -> None:
        self.assertEqual(lookup_auto("en-US"), "en")
        self.assertEqual(lookup_auto("de-AT"), "de")

    def test_unknown_or_empty(self) -> None:
        self.assertIsNone(lookup_auto("xx"))
        self.assertIsNone(lookup_auto(""))
        self.assertIsNone(lookup_auto(None))
        self.assertIsNone(lookup_auto("   "))

    def test_is_supported_source(self) -> None:
        self.assertTrue(is_supported_source("en"))
        self.assertTrue(is_supported_source("zh-Hant"))
        self.assertFalse(is_supported_source("tlh"))


class ConvertLangTest(unittest.TestCase):
    """web 通道（``convertLang``）。"""

    def test_uppercases(self) -> None:
        self.assertEqual(convert_lang("en"), "EN")
        self.assertEqual(convert_lang("zh-cn"), "ZH-CN")

    def test_he_to_iw(self) -> None:
        self.assertEqual(convert_lang("he"), "IW")
        self.assertEqual(convert_lang("HE"), "IW")

    def test_empty(self) -> None:
        self.assertEqual(convert_lang(None), "")
        self.assertEqual(convert_lang(""), "")
        self.assertEqual(convert_lang("  "), "")

    def test_strips_whitespace(self) -> None:
        self.assertEqual(convert_lang(" ja "), "JA")

    def test_convert_codes_table(self) -> None:
        self.assertEqual(CONVERT_CODES, {"IW": ("HE",)})


class ConvertLangCodeTest(unittest.TestCase):
    """Cloud 通道（``convertLangCode``）。"""

    def test_simplified_chinese(self) -> None:
        self.assertEqual(convert_lang_code("zh-cn"), "zh-CN")
        self.assertEqual(convert_lang_code("ZH-CN"), "zh-CN")

    def test_traditional_chinese(self) -> None:
        self.assertEqual(convert_lang_code("zh-hk"), "zh-TW")
        self.assertEqual(convert_lang_code("zh-tw"), "zh-TW")
        self.assertEqual(convert_lang_code("zh-TW"), "zh-TW")

    def test_other_codes_take_primary_subtag(self) -> None:
        self.assertEqual(convert_lang_code("en-US"), "en")
        self.assertEqual(convert_lang_code("de"), "de")
        self.assertEqual(convert_lang_code("JA"), "ja")

    def test_hebrew_not_legacied_for_cloud(self) -> None:
        self.assertEqual(convert_lang_code("he"), "he")

    def test_empty(self) -> None:
        self.assertEqual(convert_lang_code(None), "")
        self.assertEqual(convert_lang_code(""), "")


class SupportedLangTest(unittest.TestCase):
    """目标语言白名单（业务口径）。"""

    #: 业务方给定的清单，逐个写死。顺序也要一致——它直接用于文档与
    #: ``/v1/languages`` 的返回顺序。
    EXPECTED = (
        "en", "ar", "bg", "bn", "cs", "da", "de", "el", "es", "et", "fi", "fr",
        "he", "hi", "hr", "hu", "id", "it", "ja", "ka", "ko", "ms", "nl", "no",
        "pl", "pt", "ro", "ru", "sk", "sl", "sv", "th", "tr", "uk", "vi",
    )

    def test_matches_business_list(self) -> None:
        self.assertEqual(SUPPORTED_LANGS, self.EXPECTED)

    def test_no_duplicates(self) -> None:
        self.assertEqual(len(set(SUPPORTED_LANGS)), len(SUPPORTED_LANGS))

    def test_chinese_is_not_a_target(self) -> None:
        """中文只作为源语言：服务定位是把中文图译成外语。"""
        self.assertNotIn("zh", SUPPORTED_LANGS)
        self.assertFalse(is_supported_lang("zh-CN"))
        # 但源语言识别仍要认中文，否则中文原图一个块都拿不到
        self.assertTrue(is_supported_source("zh-Hans"))

    def test_normalize_accepts_case_space_and_region(self) -> None:
        cases = {
            "EN": "en",
            "  ja  ": "ja",
            "pt-BR": "pt",
            "pt_pt": "pt",
            "el-monoton": "el",
            "hr": "hr",
        }
        for raw, expected in cases.items():
            self.assertEqual(normalize_lang(raw), expected, raw)

    def test_normalize_maps_legacy_codes(self) -> None:
        """调用方照旧文档传 iw / in，不该被拒。"""
        self.assertEqual(normalize_lang("iw"), "he")
        self.assertEqual(normalize_lang("in"), "id")

    def test_normalize_rejects_unknown(self) -> None:
        for raw in ("xx", "", None, "  ", "klingon", 123):
            self.assertEqual(normalize_lang(raw), "", repr(raw))
            self.assertFalse(is_supported_lang(raw), repr(raw))


if __name__ == "__main__":
    unittest.main()
