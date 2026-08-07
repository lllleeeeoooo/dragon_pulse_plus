# -*- coding: utf-8 -*-
"""Bark 推送正文处理测试：md 表格转纯文本 + 单换行转 <br>（Bark 渲染不了表格/会并段落）"""
import unittest
from unittest.mock import patch, Mock

from notifier.bark import BarkNotifier, _detable, _preserve_line_breaks


class TestDetable(unittest.TestCase):
    """Markdown 表格 → 纯文本行：丢分隔行、去框线"""

    def test_表格转纯文本并丢分隔行(self):
        text = "标题\n| 股票 | 涨幅 |\n| --- | --- |\n| 600002 | +4% |\n正文"
        out = _detable(text)
        self.assertIn("股票 | 涨幅", out)
        self.assertIn("600002 | +4%", out)
        self.assertNotIn("---", out)
        self.assertIn("标题", out)
        self.assertIn("正文", out)

    def test_对齐分隔行也丢弃(self):
        text = "| A |\n| :---: |\n| B |"
        out = _detable(text)
        self.assertNotIn("---", out)
        self.assertIn("A", out)
        self.assertIn("B", out)

    def test_非表格行保留(self):
        self.assertEqual(_detable("普通文本 a | b"), "普通文本 a | b")


class TestPreserveLineBreaks(unittest.TestCase):
    """单换行 → 原生硬换行(行尾两空格+\n，紧凑换行)；\n\n 段落分隔保留（大段间空行）"""

    def test_单换行转硬换行(self):
        self.assertEqual(_preserve_line_breaks("A\nB"), "A  \nB")

    def test_段落分隔保留(self):
        # A→B 硬换行（紧凑），B→C 原段落分隔 \n\n 保留（空一行）
        self.assertEqual(_preserve_line_breaks("A\nB\n\nC"), "A  \nB\n\nC")

    def test_crlf归一化(self):
        self.assertEqual(_preserve_line_breaks("A\r\nB"), "A  \nB")

    def test_连续空行折叠为段落分隔(self):
        self.assertEqual(_preserve_line_breaks("A\n\n\n\nB"), "A\n\nB")


class TestBarkSendTransform(unittest.TestCase):
    """_do_send 实际推送前对 markdown 字段做表格+换行处理"""

    def setUp(self):
        self.b = BarkNotifier(token="test", server_url="https://api.day.app", enabled=True)
        self._log = patch.object(BarkNotifier, "_persist_log")
        self._log.start()
        self.addCleanup(self._log.stop)

    def _resp(self):
        r = Mock()
        r.status_code = 200
        r.text = "ok"
        r.json.return_value = {"code": 200}
        return r

    def test_推送正文换行转硬换行且表格转纯文本(self):
        body = "结论\n| 标的 | 涨幅 |\n| --- | --- |\n| A | +3% |\n建议"
        with patch("notifier.bark.requests.post", return_value=self._resp()) as post:
            self.b._do_send("标题", body, "group", "active", "default")
        md = post.call_args.kwargs["json"]["markdown"]
        self.assertEqual(md, "结论  \n标的 | 涨幅  \nA | +3%  \n建议")
        self.assertNotIn("\n| ", md)  # 无表格框线行

    def test_普通正文换行可见(self):
        body = "第一行\n第二行"
        with patch("notifier.bark.requests.post", return_value=self._resp()) as post:
            self.b._do_send("标题", body, "group", "active", "default")
        md = post.call_args.kwargs["json"]["markdown"]
        self.assertEqual(md, "第一行  \n第二行")

    def test_段落分隔保留(self):
        # 原 \n\n 大段分隔仍保留（大段之间空一行），段内单换行变硬换行
        body = "标题\n第一节内容\n第二节内容\n\n第三节内容"
        with patch("notifier.bark.requests.post", return_value=self._resp()) as post:
            self.b._do_send("标题", body, "group", "active", "default")
        md = post.call_args.kwargs["json"]["markdown"]
        self.assertEqual(md, "标题  \n第一节内容  \n第二节内容\n\n第三节内容")


if __name__ == "__main__":
    unittest.main()
