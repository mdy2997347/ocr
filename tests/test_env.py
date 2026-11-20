"""``.env`` 加载器的单元测试。

重点不是"能读文件"，而是两条容易被踩的语义：

1. 默认**不覆盖**已存在的环境变量（命令行/systemd 注入的值优先）；
2. 找不到文件时安静跳过，而不是抛异常把服务启动流程打断。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from imgtrans.env import apply_model_dir, default_env_path, load_env, parse_env


class ApplyModelDirTest(unittest.TestCase):
    """``IMGTRANS_MODEL_DIR`` -> ``PADDLE_PDX_CACHE_HOME`` 的映射。

    这几条语义决定了"独立部署"是否成立：模型必须落到项目目录里，
    但已显式设置底层变量的部署（容器/编排）不能被覆盖。
    """

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        patcher = mock.patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("IMGTRANS_MODEL_DIR", None)
        os.environ.pop("PADDLE_PDX_CACHE_HOME", None)

    def test_maps_and_creates_dir(self) -> None:
        target = self.tmp / "models"
        os.environ["IMGTRANS_MODEL_DIR"] = str(target)

        result = apply_model_dir()

        self.assertEqual(result, target.resolve())
        self.assertEqual(os.environ["PADDLE_PDX_CACHE_HOME"], str(target.resolve()))
        self.assertTrue(target.is_dir())

    def test_noop_when_unset(self) -> None:
        self.assertIsNone(apply_model_dir())
        self.assertNotIn("PADDLE_PDX_CACHE_HOME", os.environ)

    def test_existing_pdx_var_wins(self) -> None:
        # 容器编排注入的 PADDLE_PDX_CACHE_HOME 优先级更高，不能被 .env 顶掉
        os.environ["PADDLE_PDX_CACHE_HOME"] = str(self.tmp / "from-orchestrator")
        os.environ["IMGTRANS_MODEL_DIR"] = str(self.tmp / "from-dotenv")

        result = apply_model_dir()

        self.assertEqual(result, self.tmp / "from-orchestrator")
        self.assertEqual(
            os.environ["PADDLE_PDX_CACHE_HOME"], str(self.tmp / "from-orchestrator")
        )

    def test_relative_path_anchored_at_project_root(self) -> None:
        # 相对路径必须锚在项目根，而不是 cwd：否则从别的目录启动会把模型
        # 建到意外的位置（systemd 的 WorkingDirectory 通常不是项目根）
        os.environ["IMGTRANS_MODEL_DIR"] = "rel-models"
        result = apply_model_dir()
        assert result is not None
        self.assertTrue(result.is_absolute())
        self.assertEqual(result.parent, default_env_path.parent)
        self.assertEqual(result.name, "rel-models")
        self.assertTrue(result.is_dir())
        result.rmdir()

    def test_explicit_arg_overrides_env(self) -> None:
        os.environ["IMGTRANS_MODEL_DIR"] = str(self.tmp / "from-env")
        result = apply_model_dir(self.tmp / "from-arg")
        self.assertEqual(result, (self.tmp / "from-arg").resolve())


class ParseEnvTest(unittest.TestCase):
    def test_comments_blank_and_export(self) -> None:
        text = """
        # 注释行
        IMGTRANS_API_KEY=abc123

        export IMGTRANS_PORT=8000
        """
        self.assertEqual(
            parse_env(text), {"IMGTRANS_API_KEY": "abc123", "IMGTRANS_PORT": "8000"}
        )

    def test_quotes_are_stripped(self) -> None:
        text = 'A="http://127.0.0.1:10809"\nB=\'x y\'\n'
        self.assertEqual(parse_env(text), {"A": "http://127.0.0.1:10809", "B": "x y"})

    def test_empty_value_kept(self) -> None:
        # IMGTRANS_PROXY= 表示"明确不要代理"，不能当成没配
        self.assertEqual(parse_env("IMGTRANS_PROXY=\n"), {"IMGTRANS_PROXY": ""})

    def test_value_with_equals_sign(self) -> None:
        self.assertEqual(parse_env("TOKEN=abc=def\n"), {"TOKEN": "abc=def"})

    def test_line_without_equals_ignored(self) -> None:
        self.assertEqual(parse_env("JUST_A_WORD\nA=1\n"), {"A": "1"})


class LoadEnvTest(unittest.TestCase):
    def _write(self, content: str) -> Path:
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".env", delete=False, encoding="utf-8"
        )
        handle.write(content)
        handle.close()
        self.addCleanup(lambda: os.unlink(handle.name))
        return Path(handle.name)

    def test_does_not_override_existing(self) -> None:
        """已存在的环境变量优先级更高：线上临时改值不该被文件盖回去。"""
        path = self._write("IMGTRANS_API_KEY=from-file\n")
        with mock.patch.dict(os.environ, {"IMGTRANS_API_KEY": "from-shell"}, clear=False):
            load_env(path)
            self.assertEqual(os.environ["IMGTRANS_API_KEY"], "from-shell")

    def test_override_when_asked(self) -> None:
        path = self._write("IMGTRANS_API_KEY=from-file\n")
        with mock.patch.dict(os.environ, {"IMGTRANS_API_KEY": "from-shell"}, clear=False):
            load_env(path, override=True)
            self.assertEqual(os.environ["IMGTRANS_API_KEY"], "from-file")

    def test_missing_file_is_silent(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            self.assertEqual(load_env("does-not-exist.env"), {})

    def test_bom_is_tolerated(self) -> None:
        """Windows 记事本存 UTF-8 会带 BOM，带着 BOM 的键名永远匹配不上。"""
        handle = tempfile.NamedTemporaryFile("wb", suffix=".env", delete=False)
        handle.write("\ufeffIMGTRANS_API_KEY=bom-key\n".encode("utf-8"))
        handle.close()
        self.addCleanup(lambda: os.unlink(handle.name))

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("IMGTRANS_API_KEY", None)
            load_env(Path(handle.name))
            self.assertEqual(os.environ.get("IMGTRANS_API_KEY"), "bom-key")


if __name__ == "__main__":
    unittest.main()
