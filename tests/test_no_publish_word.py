"""T15：禁词扫描——对 examples/ 里的真实运行记录做回归。

只检查 `[stdout]` 段之后的内容。

原因：`run_injection.txt` 与 `run_conflict.txt` 的**说明行**是人工写的用例描述，
它本身就要提到"已发布"（在描述注入样本或验收要求）。扫全文件必然误报，
而真正的风险是 **Agent 输出**里出现发布语义，所以扫描范围必须精确。
"""

import unittest
from pathlib import Path

from src.honesty import find_forbidden

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
OUTPUT_MARKER = "[stdout]"


def stdout_section(text: str) -> str | None:
    """取出 `[stdout]` 之后的内容；没有该标记返回 None。"""
    lines = text.splitlines()
    try:
        index = lines.index(OUTPUT_MARKER)
    except ValueError:
        return None
    return "\n".join(lines[index + 1 :])


class TestNoPublishWord(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.texts = sorted(EXAMPLES.glob("run_*.txt"))

    def test_evidence_exists(self):
        self.assertGreaterEqual(
            len(self.texts), 8, "examples/ 下应有 8 组运行记录，先跑 scripts/run_examples.py"
        )

    def test_every_file_has_stdout_marker(self):
        for path in self.texts:
            with self.subTest(file=path.name):
                self.assertIsNotNone(
                    stdout_section(path.read_text(encoding="utf-8")),
                    f"{path.name} 缺少 [stdout] 标记",
                )

    def test_stdout_has_no_forbidden_words(self):
        for path in self.texts:
            text = path.read_text(encoding="utf-8")
            section = stdout_section(text)
            with self.subTest(file=path.name):
                self.assertEqual(
                    find_forbidden(section or ""), [], f"{path.name} 输出含禁词"
                )

    def test_input_description_may_contain_the_word(self):
        """反证扫描范围正确：输入侧确实含有"已发布"，输出侧没有。"""
        text = (EXAMPLES / "run_injection.txt").read_text(encoding="utf-8")
        self.assertIn("已发布", text, "注入用例的说明应描述该越权指令")
        self.assertNotIn("已发布", stdout_section(text) or "")

    def test_traces_have_no_forbidden_words(self):
        for path in sorted(EXAMPLES.glob("run_*.jsonl")):
            with self.subTest(file=path.name):
                self.assertEqual(
                    find_forbidden(path.read_text(encoding="utf-8")), []
                )

    def test_audit_log_has_no_forbidden_words(self):
        audit = EXAMPLES / "audit.jsonl"
        if not audit.is_file():
            self.skipTest("尚未生成审计日志")
        self.assertEqual(find_forbidden(audit.read_text(encoding="utf-8")), [])


if __name__ == "__main__":
    unittest.main()
