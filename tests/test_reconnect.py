"""v4.03 低速自动重连的纯逻辑测试。

背景（2026-11-23 现场实测）：PikPak 的下载连接会进入「慢速蠕动」状态 ——
不报错、不超时、字节还在涨，但只有 58~74 KB/s。此时 stuckWatchdog 不触发
（它要求速度为 0），任务就这样挂着跑几十小时。手动「停→继续」后立刻
8.9~23.4 MB/s，说明废掉的是那条连接本身，不是账号额度也不是带宽。

因此这里要守住三件事：
  1) 判据是**窗口内平均速度**，不是瞬时速度（实测同文件 10 秒内 204→16 KB/s）；
  2) 阈值以上的任务绝不能被踢（否则正常任务会被反复打断）；
  3) 真慢的线路上不能无休止地踢 —— 次数封顶 + 窗口翻倍。
"""
import importlib.util
import os
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(os.path.dirname(HERE), "download-console.py")

_spec = importlib.util.spec_from_file_location("console_under_test_reconnect", APP)
m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)

SET = {"autoReconnect": True, "reconnectSlowKBps": 384,
       "reconnectWindowSec": 180, "reconnectMax": 5}


def job(**kw):
    j = {"id": 1, "state": "running", "kind": "download", "bytes": 0}
    j.update(kw)
    return j


class ReconnectWatchdogTest(unittest.TestCase):
    def setUp(self):
        # 不真的起线程、不真的写日志
        self.spawned = []
        self._p = mock.patch.object(
            m.threading, "Thread",
            side_effect=lambda **kw: self.spawned.append(kw) or mock.MagicMock())
        self._p.start()
        self._le = mock.patch.object(m, "log_event")
        self._le.start()

    def tearDown(self):
        self._p.stop()
        self._le.stop()

    # ── 基线 ───────────────────────────────────────────────────────────
    def test_first_call_only_sets_baseline(self):
        # 返回值是「job 被改过」，调度器据此写盘。基线记在内存里不落盘的话，
        # 下一轮从 jobs.json 读回 0，看门狗就永远停在记基线这一步（真机踩过）。
        j = job(bytes=1000)
        with mock.patch.object(m.time, "time", return_value=1000):
            self.assertTrue(m._reconnect_watchdog(j, SET))
        self.assertEqual(j["_kickBytes"], 1000)
        self.assertEqual(j["_kickAt"], 1000)
        self.assertEqual(self.spawned, [])

    def test_waits_for_full_window(self):
        j = job(bytes=0)
        with mock.patch.object(m.time, "time", return_value=1000):
            m._reconnect_watchdog(j, SET)
        # 179 秒、字节暴涨，仍然不能触发（窗口没到）
        with mock.patch.object(m.time, "time", return_value=1179):
            self.assertFalse(m._reconnect_watchdog(j, dict(SET, bytes=0)))
        self.assertEqual(self.spawned, [])

    # ── 必须踢的情形 ───────────────────────────────────────────────────
    def test_kicks_when_window_average_below_threshold(self):
        # 180 秒只涨了 58 KB/s * 180 ≈ 10.4 MB
        j = job(bytes=0)
        with mock.patch.object(m.time, "time", return_value=1000):
            m._reconnect_watchdog(j, SET)
        j["bytes"] = int(58 * 1024 * 180)
        with mock.patch.object(m.time, "time", return_value=1180):
            self.assertTrue(m._reconnect_watchdog(j, SET))
        self.assertEqual(len(self.spawned), 1)
        self.assertEqual(j["_kickN"], 1)
        self.assertEqual(j["_kickWin"], 360)      # 窗口翻倍

    def test_zero_progress_still_kicks(self):
        j = job(bytes=0)
        with mock.patch.object(m.time, "time", return_value=1000):
            m._reconnect_watchdog(j, SET)
        with mock.patch.object(m.time, "time", return_value=1180):
            self.assertTrue(m._reconnect_watchdog(j, SET))

    # ── v4.06：同一个文件连续零字节要停止踢 ─────────────────────────────
    def test_same_file_zero_bytes_stops_kicking(self):
        """现场：PikPak 一个无直链的占位文件，看门狗每 180 秒重启任务，
        单文件自己的重试永远走不到失败，任务在 97.4% 无限循环。
        规则：同一文件连续两轮零字节 → 不再踢，交给文件级尾池判失败。"""
        j = job(bytes=0, curfile="32.mp4")
        with mock.patch.object(m.time, "time", return_value=1000):
            m._reconnect_watchdog(j, SET)                      # 基线
        with mock.patch.object(m.time, "time", return_value=1180):
            self.assertTrue(m._reconnect_watchdog(j, SET))     # same=0 → 第 1 次踢
        self.assertEqual(len(self.spawned), 1)
        with mock.patch.object(m.time, "time", return_value=1540):
            self.assertTrue(m._reconnect_watchdog(j, SET))     # 窗口 360，same=1 → 第 2 次踢
        self.assertEqual(len(self.spawned), 2)
        with mock.patch.object(m.time, "time", return_value=2260):
            self.assertTrue(m._reconnect_watchdog(j, SET))     # 窗口 720，same=2 → 放弃
        self.assertEqual(len(self.spawned), 2)                 # 不再重启
        self.assertEqual(j["_kickSame"], 2)

    def test_new_file_resets_the_strike_counter(self):
        j = job(bytes=0, curfile="32.mp4", _kickFile="32.mp4",
                _kickFileBytes=0, _kickSame=2, _kickAt=1000, _kickBytes=0)
        j["bytes"] = 0
        j["curfile"] = "33.mp4"          # 换文件了，重新给机会
        with mock.patch.object(m.time, "time", return_value=1180):
            self.assertTrue(m._reconnect_watchdog(j, SET))
        self.assertEqual(len(self.spawned), 1)
        self.assertEqual(j["_kickSame"], 0)

    def test_bytes_advancing_resets_strikes(self):
        # 同一个文件但在涨字节（只是慢）→ 不该被这条规则拦住
        j = job(bytes=0, curfile="32.mp4", _kickFile="32.mp4",
                _kickFileBytes=0, _kickSame=2, _kickAt=1000, _kickBytes=0)
        j["bytes"] = 50 * 1024 * 180
        with mock.patch.object(m.time, "time", return_value=1180):
            self.assertTrue(m._reconnect_watchdog(j, SET))
        self.assertEqual(len(self.spawned), 1)
        self.assertEqual(j["_kickSame"], 0)

    # ── 不能踢的情形 ───────────────────────────────────────────────────
    def test_healthy_rate_is_not_kicked(self):
        j = job(bytes=0)
        with mock.patch.object(m.time, "time", return_value=1000):
            m._reconnect_watchdog(j, SET)
        j["bytes"] = int(9 * 1024 * 1024 * 180)   # 9 MB/s
        with mock.patch.object(m.time, "time", return_value=1180):
            self.assertTrue(m._reconnect_watchdog(j, SET))   # 只推进基准，不踢
        self.assertEqual(self.spawned, [])
        self.assertEqual(j["_kickAt"], 1180)       # 基准推进到当前

    def test_just_under_threshold_counts_as_slow(self):
        j = job(bytes=0)
        with mock.patch.object(m.time, "time", return_value=1000):
            m._reconnect_watchdog(j, SET)
        j["bytes"] = int(383 * 1024 * 180)
        with mock.patch.object(m.time, "time", return_value=1180):
            self.assertTrue(m._reconnect_watchdog(j, SET))

    def test_respects_max_kicks(self):
        j = job(bytes=0, _kickN=5)
        with mock.patch.object(m.time, "time", return_value=1000):
            j["_kickAt"], j["_kickBytes"] = 1000, 0
        with mock.patch.object(m.time, "time", return_value=2000):
            self.assertFalse(m._reconnect_watchdog(j, SET))
        self.assertEqual(self.spawned, [])

    def test_disabled_switch_and_zero_max(self):
        j = job(bytes=0)
        with mock.patch.object(m.time, "time", return_value=1000):
            self.assertFalse(m._reconnect_watchdog(j, dict(SET, autoReconnect=False)))
            self.assertFalse(m._reconnect_watchdog(j, dict(SET, reconnectMax=0)))
        self.assertEqual(self.spawned, [])

    def test_skips_non_download_kinds(self):
        for kind in ("xfer", "unzip"):
            j = job(bytes=0, kind=kind)
            with mock.patch.object(m.time, "time", return_value=1000):
                self.assertFalse(m._reconnect_watchdog(j, SET))
        self.assertEqual(self.spawned, [])

    def test_skips_non_running(self):
        j = job(bytes=0, state="stopped")
        with mock.patch.object(m.time, "time", return_value=1000):
            self.assertFalse(m._reconnect_watchdog(j, SET))
        self.assertEqual(self.spawned, [])

    # ── 窗口退避 ───────────────────────────────────────────────────────
    def test_window_doubles_and_caps_at_one_hour(self):
        j = job(bytes=0, _kickN=4, _kickWin=1800)
        # 注意 0 是「未设置基准」的哨兵值（`not base_t` 会命中基线分支），所以用 1
        j["_kickAt"], j["_kickBytes"] = 1, 0
        with mock.patch.object(m.time, "time", return_value=4000):
            self.assertTrue(m._reconnect_watchdog(j, SET))
        self.assertEqual(j["_kickWin"], 3600)
        self.assertEqual(j["_kickN"], 5)

    def test_counter_regression_rebaselines_instead_of_kicking(self):
        # 字节计数器回退 = 任务自己重启过，不是变慢。真机第一次实测就是被
        # 「736MB → 161MB」这种回退误踢了一脚，所以这里必须只重记基准。
        j = job(bytes=0)
        with mock.patch.object(m.time, "time", return_value=1000):
            m._reconnect_watchdog(j, SET)
        j["bytes"] = 161 * 1024 * 1024
        j["_kickBytes"] = 736 * 1024 * 1024
        with mock.patch.object(m.time, "time", return_value=1180):
            self.assertTrue(m._reconnect_watchdog(j, SET))   # 只重记基准
        self.assertEqual(self.spawned, [])
        self.assertFalse(j.get("_kickN"))          # 没踢，计数不动
        self.assertEqual(j["_kickBytes"], 161 * 1024 * 1024)


class ReconnectDefaultsTest(unittest.TestCase):
    def test_defaults_present(self):
        for k in ("autoReconnect", "reconnectSlowKBps", "reconnectWindowSec", "reconnectMax"):
            self.assertIn(k, m.DEFAULTS)
        self.assertTrue(m.DEFAULTS["autoReconnect"])
        self.assertEqual(m.DEFAULTS["reconnectMax"], 5)

    def test_norm_clamps_absurd_values(self):
        d = m._norm_settings({"autoReconnect": "false", "reconnectSlowKBps": 0,
                              "reconnectWindowSec": 5, "reconnectMax": 999})
        self.assertFalse(d["autoReconnect"])
        self.assertEqual(d["reconnectSlowKBps"], 32)
        self.assertEqual(d["reconnectWindowSec"], 60)
        self.assertEqual(d["reconnectMax"], 20)

    def test_settings_are_writable(self):
        # 回归：第一版把字段加进了 DEFAULTS 却忘了加进 savesettings 白名单，
        # 结果是「设置页能改、保存后无效」，只有真机点一次才发现。
        for k in ("autoReconnect", "reconnectSlowKBps", "reconnectWindowSec", "reconnectMax"):
            self.assertIn(k, m.SETTINGS_WRITABLE)


if __name__ == "__main__":
    unittest.main()
