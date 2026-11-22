"""v3.97/v3.98 一致性校验「云端列目录」的测试。

背景（2026-11-23 实测，会员号 159 个文件）：
  · PikPak 的列目录耗时是双峰的 —— 平时 1~2 秒，抖动时同一条命令能挂满 150 秒；
    04:32 超时失败，04:34 用完全相同参数 1.2 秒返回 159 条 → 一次性抖动不该直接
    变成「云端连接失败」，所以第一次 60 秒、失败立刻重试一次；
  · `lsjson --stat` 指向「目录」时在 PikPak 上永远不返回（25 秒超时、无输出），
    所以不再先探一次，而是递归列一次、用返回值自己判断是文件还是目录
    （单文件只列出一条且 Path 恰好是自己的文件名）。
"""
import importlib.util
import json
import os
import subprocess
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(os.path.dirname(HERE), "download-console.py")

_spec = importlib.util.spec_from_file_location("console_under_test_verify_retry", APP)
m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)


class _P:
    def __init__(self, rc=0, out=b"", err=b""):
        self.returncode = rc
        self.stdout = out
        self.stderr = err


def _lsjson(entries):
    return json.dumps(entries, ensure_ascii=False).encode()


FILES = [{"Path": "a.mp4", "Size": 10}, {"Path": "b.mp4", "Size": 20}]


class VerifyListRetryTest(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self._orig = m._rclone_run

    def tearDown(self):
        m._rclone_run = self._orig

    def _install(self, seq):
        """seq: 每次调用依次返回的 _P 或 Exception 实例。"""
        self.calls.clear()

        def fake(args, timeout=90):
            self.calls.append((list(args), timeout))
            item = seq[len(self.calls) - 1]
            if isinstance(item, Exception):
                raise item
            return item
        m._rclone_run = fake

    def test_timeout_then_success(self):
        self._install([subprocess.TimeoutExpired(cmd="rclone", timeout=60),
                       _P(out=_lsjson(FILES))])
        r = m.verify_cloud("会员号", "Pack From Shared/demo")
        self.assertEqual(r["error"], "")
        self.assertEqual(sorted(r["files"]), ["a.mp4", "b.mp4"])
        self.assertEqual(r["scope"], "dir")
        self.assertEqual(r["attempts"], 2)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.calls[0][1], m.VERIFY_LIST_TMO[0])
        self.assertEqual(self.calls[1][1], m.VERIFY_LIST_TMO[1])
        self.assertIn("--recursive", self.calls[0][0])

    def test_first_call_is_not_stat_probe(self):
        """目录场景不该再做 --stat 首探（PikPak 上对目录永不返回）。"""
        self._install([_P(out=_lsjson(FILES))])
        m.verify_cloud("会员号", "Pack From Shared/demo")
        self.assertEqual(len(self.calls), 1)
        self.assertNotIn("--stat", self.calls[0][0])
        self.assertEqual(self.calls[0][1], m.VERIFY_LIST_TMO[0])

    def test_both_attempts_timeout_reports_clearly(self):
        self._install([subprocess.TimeoutExpired(cmd="rclone", timeout=60),
                       subprocess.TimeoutExpired(cmd="rclone", timeout=150)])
        r = m.verify_cloud("会员号", "Pack From Shared/demo")
        self.assertEqual(r["error"], "云端列目录超时（第 2 次，150 秒）")
        self.assertEqual(r["attempts"], 2)
        self.assertEqual(r["files"], {})

    def test_nonzero_exit_then_success(self):
        self._install([_P(rc=1, err=b"couldn't get captcha token"),
                       _P(out=_lsjson(FILES))])
        r = m.verify_cloud("会员号", "Pack From Shared/demo")
        self.assertEqual(r["error"], "")
        self.assertEqual(len(r["files"]), 2)
        self.assertEqual(r["attempts"], 2)

    def test_nonzero_exit_both_keeps_reason(self):
        self._install([_P(rc=1, err=b"list boom"),
                       _P(rc=1, err=b"list boom2")])
        r = m.verify_cloud("会员号", "Pack From Shared/demo")
        self.assertIn("list boom2", r["error"])
        self.assertEqual(r["attempts"], 2)

    def test_single_file_detected_from_listing(self):
        """单文件：递归列表只返回一条，且 Path 等于请求的文件名。"""
        ent = [{"Path": "x.mp4", "Name": "x.mp4", "Size": 7}]
        self._install([_P(out=_lsjson(ent))])
        r = m.verify_cloud("会员号", "pack/x.mp4")
        self.assertEqual(r["scope"], "file")
        self.assertEqual(r["files"], {"x.mp4": 7})
        self.assertEqual(r["targets"], {"x.mp4": "会员号:pack/x.mp4"})
        self.assertEqual(r["attempts"], 1)
        self.assertEqual(len(self.calls), 1)

    def test_dir_with_single_differently_named_file_stays_dir(self):
        ent = [{"Path": "other.mp4", "Size": 7}]
        self._install([_P(out=_lsjson(ent))])
        r = m.verify_cloud("会员号", "pack/dir1")
        self.assertEqual(r["scope"], "dir")
        self.assertEqual(r["targets"]["other.mp4"], "会员号:pack/dir1/other.mp4")

    def test_junk_and_dir_entries_skipped(self):
        ent = [{"Path": "sub", "IsDir": True},
               {"Path": ".DS_Store", "Size": 1},
               {"Path": "ok1.mp4", "Size": 3},
               {"Path": "ok2.partial", "Size": 4}]
        self._install([_P(out=_lsjson(ent))])
        r = m.verify_cloud("会员号", "d")
        self.assertEqual(list(r["files"]), ["ok1.mp4"])
        self.assertEqual(r["targets"]["ok1.mp4"], "会员号:d/ok1.mp4")


if __name__ == "__main__":
    unittest.main()
