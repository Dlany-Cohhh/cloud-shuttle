#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v3.92：详情页「正在传输」的文件级字节/速度要取自 worker 的实时采样。

覆盖：
- 有采样时按原路径回填 bytes / speed；
- 无采样（刚起、刚换文件）退化为 0，不抛异常；
- 只列 status=running，done/pending 不进列表；
- 最多 20 条，避免详情页过大。

运行：python -m unittest discover -s tests -v
"""
import importlib.util
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("console", os.path.join(ROOT, "download-console.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class LiveTransferringTest(unittest.TestCase):
    def test_fills_bytes_and_speed(self):
        entries = [{"src": "a.mp4", "size": 1000, "status": "running"}]
        lives = {"a.mp4": (400, 10240.0, "a.mp4")}
        out = m.build_live_transferring(entries, lives)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["bytes"], 400)
        self.assertEqual(out[0]["total"], 1000)
        self.assertEqual(out[0]["speed"], 10240.0)
        self.assertEqual(out[0]["name"], "a.mp4")

    def test_missing_sample_is_zero(self):
        entries = [{"src": "b.mp4", "size": 900, "status": "running"}]
        out = m.build_live_transferring(entries, {})
        self.assertEqual(out[0]["bytes"], 0)
        self.assertEqual(out[0]["speed"], 0)

    def test_only_running_listed(self):
        entries = [
            {"src": "done.mp4", "size": 1, "status": "done"},
            {"src": "wait.mp4", "size": 1, "status": "pending"},
            {"src": "def.mp4", "size": 1, "status": "deferred"},
            {"src": "run.mp4", "size": 1, "status": "running"},
        ]
        out = m.build_live_transferring(entries, {"run.mp4": (1, 2.0, "run.mp4")})
        self.assertEqual([x["name"] for x in out], ["run.mp4"])

    def test_cap_twenty(self):
        entries = [{"src": "f%d" % i, "size": 1, "status": "running"} for i in range(30)]
        self.assertEqual(len(m.build_live_transferring(entries, {})), 20)

    def test_bad_values_do_not_raise(self):
        entries = [{"src": "c.mp4", "size": None, "status": "running"}]
        out = m.build_live_transferring(entries, {"c.mp4": (None, None, "c.mp4")})
        self.assertEqual(out[0]["bytes"], 0)
        self.assertEqual(out[0]["total"], 0)


if __name__ == "__main__":
    unittest.main()
