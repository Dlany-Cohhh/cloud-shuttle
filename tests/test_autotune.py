#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v3.94：自动测速调优的单元测试。

覆盖：
- 真实吞吐口径 = 目标目录落盘增量（不看 rclone 自报速度）；
- autotune.json 读写 / 最优档解析，坏数据不能炸；
- 候选档一律受 AUTOTUNE_MAX_CONNS 约束（实测 24 连接就会塌）；
- autoTune 开启时任务套用最优档，关闭时原样返回；
- 新设置字段的钳制区间。
"""
import importlib.util
import os
import tempfile
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("console", os.path.join(ROOT, "download-console.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cs-autotune-")
        self._old_at = m.AUTOTUNE_FILE
        self._old_local = m.local_dest
        m.AUTOTUNE_FILE = os.path.join(self.tmp, "autotune.json")
        m.local_dest = lambda d: d           # 测试里 dest 直接就是本地路径

    def tearDown(self):
        m.AUTOTUNE_FILE = self._old_at
        m.local_dest = self._old_local


class ThroughputTest(_Base):
    def test_dir_bytes_counts_recursive_files(self):
        d = os.path.join(self.tmp, "dest")
        os.makedirs(os.path.join(d, "a", "b"))
        with open(os.path.join(d, "x.bin"), "wb") as f:
            f.write(b"x" * 1000)
        with open(os.path.join(d, "a", "b", "y.bin"), "wb") as f:
            f.write(b"y" * 2000)
        self.assertEqual(m.dir_bytes(d), 3000)

    def test_dir_bytes_tolerates_missing_dir(self):
        self.assertEqual(m.dir_bytes(os.path.join(self.tmp, "nope")), 0)
        self.assertEqual(m.dir_bytes(""), 0)

    def test_measure_rate_uses_disk_growth(self):
        """1 秒窗口内写入 1 MB，成绩应约为 1 MB/s；settle 期间写的字节不算。"""
        d = os.path.join(self.tmp, "rate")
        os.makedirs(d)
        stop = threading.Event()

        def writer():
            # settle 2s：先写 2MB，不该计入
            with open(os.path.join(d, "warm.bin"), "wb") as f:
                f.write(b"0" * (2 * 1048576))
            while not stop.is_set():
                with open(os.path.join(d, "hot.bin"), "ab") as f:   # 追加，文件体积才会增长
                    f.write(b"1" * (256 * 1024))
                time.sleep(0.26)          # 约 1 MB/s

        th = threading.Thread(target=writer, daemon=True)
        th.start()
        try:
            rate = m.measure_rate(d, 2, settle=2)
        finally:
            stop.set()
            th.join(1)
        self.assertGreater(rate, 0.4)
        self.assertLess(rate, 2.2)        # 2MB 预写没被算进来

    def test_measure_rate_zero_when_nothing_written(self):
        d = os.path.join(self.tmp, "idle")
        os.makedirs(d)
        self.assertEqual(round(m.measure_rate(d, 1), 2), 0)


class StateTest(_Base):
    def test_write_then_best(self):
        m._at_write({"best": {"transfers": 2, "mts": 8, "rate": 2.2,
                              "label": "2x8", "at": 123}, "history": []})
        b = m.at_best()
        self.assertEqual((b["transfers"], b["mts"]), (2, 8))
        self.assertAlmostEqual(b["rate"], 2.2)
        self.assertEqual(b["label"], "2x8")

    def test_best_ignores_garbage(self):
        m._at_write({"best": {"transfers": "x", "mts": None}})
        self.assertIsNone(m.at_best())
        m._at_write({"best": {}})
        self.assertIsNone(m.at_best())
        m.AUTOTUNE_FILE = os.path.join(self.tmp, "not-exist.json")
        self.assertIsNone(m.at_best())

    def test_label_format(self):
        self.assertEqual(m.at_label(2, 8), "2x8")


class CandidateTest(_Base):
    def test_all_candidates_fit_connection_cap(self):
        """实测 24 连接掉到 0.11 MB/s，候选档必须都 ≤ 16 连接。"""
        for t, mts in m.AUTOTUNE_CANDIDATES:
            self.assertLessEqual(t * mts, m.AUTOTUNE_MAX_CONNS)
        self.assertLessEqual(m.AUTOTUNE_MAX_CONNS, 16)

    def test_candidates_have_two_kinds(self):
        """既要有「多文件」档，也要有「单文件多流」档，才能覆盖不同文件数场景。"""
        self.assertTrue(any(t >= 2 for t, _ in m.AUTOTUNE_CANDIDATES))
        self.assertTrue(any(mts >= 12 for _, mts in m.AUTOTUNE_CANDIDATES))


class ApplyProfileTest(_Base):
    def test_applies_stored_best(self):
        m._at_write({"best": {"transfers": 2, "mts": 8, "rate": 2.2, "label": "2x8"}})
        s = {"autoTune": True, "transfers": 4, "multiThreadStreams": 16}
        out = m.autotune_profile_for(s)
        self.assertEqual((out["transfers"], out["multiThreadStreams"]), (2, 8))
        self.assertEqual((s["transfers"], s["multiThreadStreams"]), (4, 16))  # 不改原 dict

    def test_noop_when_disabled(self):
        m._at_write({"best": {"transfers": 2, "mts": 8, "rate": 2.2, "label": "2x8"}})
        s = {"autoTune": False, "transfers": 4, "multiThreadStreams": 16}
        self.assertEqual(m.autotune_profile_for(s), s)

    def test_noop_when_already_best(self):
        m._at_write({"best": {"transfers": 2, "mts": 8, "rate": 2.2, "label": "2x8"}})
        s = {"autoTune": True, "transfers": 2, "multiThreadStreams": 8}
        self.assertEqual(m.autotune_profile_for(s), s)

    def test_probe_running_bypasses_best(self):
        """测速进行中不能被「已存最优档」覆盖，否则四档其实都在跑同一档。"""
        m._at_write({"best": {"transfers": 2, "mts": 8, "rate": 2.2, "label": "2x8"}})
        s = {"autoTune": True, "transfers": 4, "multiThreadStreams": 4}
        old = dict(m.AUTOTUNE_RUN)
        try:
            m.AUTOTUNE_RUN.update({"running": True})
            self.assertEqual(m.autotune_profile_for(s), s)
        finally:
            m.AUTOTUNE_RUN.clear()
            m.AUTOTUNE_RUN.update(old)

    def test_no_best_yet(self):
        m.AUTOTUNE_FILE = os.path.join(self.tmp, "empty.json")
        s = {"autoTune": True, "transfers": 4, "multiThreadStreams": 16}
        self.assertEqual(m.autotune_profile_for(s), s)


class GrowthGateTest(_Base):
    """换档后有列目录/建索引的空窗，必须等真增长才计分（v3.94 实测踩过的坑）。"""

    def test_returns_none_when_idle(self):
        d = os.path.join(self.tmp, "idle2")
        os.makedirs(d)
        t0 = time.time()
        self.assertIsNone(m.wait_growth(d, timeout=4, step=1))
        self.assertLess(time.time() - t0, 6)

    def test_returns_after_growth(self):
        d = os.path.join(self.tmp, "grow")
        os.makedirs(d)

        def late_writer():
            time.sleep(2)
            with open(os.path.join(d, "f.bin"), "wb") as f:
                f.write(b"z" * (4 * 1048576))

        threading.Thread(target=late_writer, daemon=True).start()
        waited = m.wait_growth(d, timeout=20, step=1)
        self.assertIsNotNone(waited)
        self.assertGreaterEqual(waited, 1.0)


class SettingsTest(unittest.TestCase):
    def test_autotune_fields_default_and_clamped(self):
        s = m._norm_settings({})
        self.assertTrue(s["autoTune"])
        self.assertEqual(s["autoTuneSeconds"], 45)
        self.assertEqual(s["autoTuneSettle"], 30)
        s = m._norm_settings({"autoTune": "false", "autoTuneSeconds": 0, "autoTuneSettle": 9999})
        self.assertFalse(s["autoTune"])
        self.assertEqual(s["autoTuneSeconds"], 15)
        self.assertEqual(s["autoTuneSettle"], 120)
        s = m._norm_settings({"autoTune": "on", "autoTuneSeconds": "90"})
        self.assertTrue(s["autoTune"])
        self.assertEqual(s["autoTuneSeconds"], 90)


if __name__ == "__main__":
    unittest.main()
