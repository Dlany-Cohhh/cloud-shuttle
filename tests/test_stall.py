"""v3.95 龟速让位 / 慢速尾池的纯逻辑测试。

背景（2026-11-23 实测）：4 个并发槽里 2 个被 0.01~0.02 MB/s 的文件长期占住，
总吞吐从 4.4 MB/s 掉到 2.3 MB/s。这里覆盖三条规则：
  1) stallSeconds 必须在 30~600 秒之间，非法值回退默认 90；
  2) 第二轮必须把「让位过的文件」和「没试过的文件」分开，前者只占 1 个槽；
  3) 尾池不能无限期挂着：30 分钟推不动 1MB 就认输。
"""
import importlib.util
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(os.path.dirname(HERE), "download-console.py")

_spec = importlib.util.spec_from_file_location("console_under_test_stall", APP)
m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)


class StallLimitsTest(unittest.TestCase):
    def test_default_seconds(self):
        self.assertEqual(m.stall_limits({}), (m.STALL_MIN_SPEED, 90))

    def test_seconds_in_range(self):
        self.assertEqual(m.stall_limits({"stallSeconds": 120})[1], 120)

    def test_too_small_clamped(self):
        self.assertEqual(m.stall_limits({"stallSeconds": 3})[1], 30)

    def test_too_big_clamped(self):
        self.assertEqual(m.stall_limits({"stallSeconds": 99999})[1], 600)

    def test_garbage_falls_back(self):
        self.assertEqual(m.stall_limits({"stallSeconds": "abc"})[1], 90)
        self.assertEqual(m.stall_limits({"stallSeconds": None})[1], 90)

    def test_settings_clamp(self):
        s = m._norm_settings({"stallSeconds": 1})
        self.assertEqual(s["stallSeconds"], 30)
        s = m._norm_settings({"stallSeconds": 5000})
        self.assertEqual(s["stallSeconds"], 600)
        s = m._norm_settings({})
        self.assertEqual(s["stallSeconds"], 90)

    def test_min_speed_unchanged(self):
        self.assertEqual(m.STALL_MIN_SPEED, 50 * 1024)

    def test_stall_default_lowered(self):
        # v3.95 把 300 秒抬到 90 秒：300 秒 × 2 个槽 = 白等 10 分钟
        self.assertEqual(m.STALL_SECONDS, 90)


class SplitRound2Test(unittest.TestCase):
    def test_split_by_slow_flag(self):
        todo = [{"src": "a"}, {"src": "b", "slow": True}, {"src": "c", "slow": False}]
        fast, slow = m.split_round2(todo)
        self.assertEqual(len(fast), 2)
        self.assertEqual(len(slow), 1)
        self.assertEqual(slow[0]["src"], "b")

    def test_no_slow(self):
        todo = [{"src": "a"}, {"src": "b"}]
        fast, slow = m.split_round2(todo)
        self.assertEqual(len(fast), 2)
        self.assertEqual(slow, [])

    def test_all_slow(self):
        todo = [{"src": "a", "slow": True}, {"src": "b", "slow": True}]
        fast, slow = m.split_round2(todo)
        self.assertEqual(fast, [])
        self.assertEqual(len(slow), 2)

    def test_slow_flag_false_is_fast(self):
        # 只认 True，避免 attempts=0 之类的老数据被误判成慢文件
        todo = [{"src": "a", "slow": 0}, {"src": "b", "slow": ""}]
        fast, slow = m.split_round2(todo)
        self.assertEqual(len(fast), 2)

    def test_tail_workers_respect_conn_budget(self):
        # v3.96：尾池按连接预算开槽，4x4 时可以用满 4 个
        self.assertEqual(m.tail_workers({"multiThreadStreams": 4}, 4), 4)
        self.assertEqual(m.tail_workers({"multiThreadStreams": 8}, 4), 2)
        self.assertEqual(m.tail_workers({"multiThreadStreams": 16}, 4), 1)
        # 不能超过任务本身的总并发
        self.assertEqual(m.tail_workers({"multiThreadStreams": 4}, 1), 1)
        # 坏数据兜底
        self.assertEqual(m.tail_workers({}, 4), 4)
        self.assertEqual(m.tail_workers({"multiThreadStreams": 0}, 4), 4)

    def test_does_not_mutate(self):
        todo = [{"src": "a", "slow": True}]
        before = [dict(x) for x in todo]
        m.split_round2(todo)
        self.assertEqual(todo, before)


class SlowTailGiveUpTest(unittest.TestCase):
    def test_hopeless_after_30min_with_almost_nothing(self):
        self.assertTrue(m.slow_tail_hopeless(1800, 1024))

    def test_not_hopeless_when_bytes_flowed(self):
        self.assertFalse(m.slow_tail_hopeless(1800, 5 * 1048576))

    def test_not_hopeless_before_window(self):
        # v4.06 语义变更：零字节有单独的早退窗口（ZERO_TAIL_SECONDS=600），
        # 所以「600 秒 + 零字节」现在就该认输；但只要有字节进来，仍按 30 分钟算。
        self.assertTrue(m.slow_tail_hopeless(600, 0))
        self.assertFalse(m.slow_tail_hopeless(600, 1024))
        self.assertFalse(m.slow_tail_hopeless(599, 0))

    def test_zero_bytes_gives_up_early(self):
        """源对象根本没打开成功（PikPak 无直链）时不必等满 30 分钟。"""
        self.assertTrue(m.slow_tail_hopeless(m.ZERO_TAIL_SECONDS, 0))
        self.assertFalse(m.slow_tail_hopeless(m.ZERO_TAIL_SECONDS - 1, 0))
        # 有字节但极慢：仍走 30 分钟那条线
        self.assertFalse(m.slow_tail_hopeless(1200, 4096))
        self.assertTrue(m.slow_tail_hopeless(1800, 4096))

    def test_boundary(self):
        self.assertTrue(m.slow_tail_hopeless(1800, 1048575))
        self.assertFalse(m.slow_tail_hopeless(1800, 1048576))

    def test_constants(self):
        self.assertEqual(m.SLOW_TAIL_MAX_SECONDS, 1800)
        self.assertEqual(m.SLOW_TAIL_MAX_BYTES, 1048576)
        self.assertEqual(m.ZERO_TAIL_SECONDS, 600)


class Round2FlowTest(unittest.TestCase):
    """用假 run_one 跑一遍二轮调度，确认慢速尾池只有一个并发。"""

    def test_slow_files_serialized(self):
        entries = [{"src": "s%d" % i, "slow": True, "status": "pending"} for i in range(3)]
        entries.append({"src": "f1", "status": "pending"})
        calls = {"peak": 0, "now": 0}

        class Lock:
            pass

        import threading
        lk = threading.Lock()

        def run_one(e):
            with lk:
                calls["now"] += 1
                calls["peak"] = max(calls["peak"], calls["now"])
            if e.get("slow"):
                import time as _t
                _t.sleep(0.02)
            with lk:
                calls["now"] -= 1
            e["status"] = "done"

        fast, slow = m.split_round2(entries)
        self.assertEqual(len(fast), 1)
        self.assertEqual(len(slow), 3)

        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(run_one, fast))
        peak_before = calls["peak"]
        with ThreadPoolExecutor(max_workers=m.tail_workers({"multiThreadStreams": 16}, 4)) as pool:
            list(pool.map(run_one, slow))
        self.assertEqual(calls["peak"], peak_before, "慢速尾池不应该超过前面已有的并发峰值")


if __name__ == "__main__":
    unittest.main()
