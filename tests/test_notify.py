"""v4.10 PushPlus 微信通知的纯逻辑测试。

设计取舍（为什么不用「在每处状态切换点插一句」）：
任务进入终态的地方有六七处（rclone job、v2 目录 worker、转封装、跨账号转移、解压、
卡住看门狗、低速重连用尽），挨个插一遍，新增一条路径忘了插就静默失效。
改成每 5 秒巡检 jobs.json、看状态跃迁，跃迁一次通知一次，去重键落在 notify-state.json。
"""
import importlib.util
import json
import os
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(os.path.dirname(HERE), "download-console.py")

_spec = importlib.util.spec_from_file_location("console_under_test_notify", APP)
m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)


class NotifyEventTest(unittest.TestCase):
    def test_done_is_success(self):
        self.assertEqual(m.notify_event({"state": "done"}), "success")

    def test_error_is_fail(self):
        self.assertEqual(m.notify_event({"state": "error", "error": "captcha"}), "fail")

    def test_fault_stop_is_fail(self):
        """额度用尽 / 看门狗 / 重连用尽都落在 stopped，但带错误信息 → 要通知。"""
        self.assertEqual(m.notify_event({"state": "stopped", "error": "当日下载额度已用尽"}), "fail")

    def test_user_stop_is_silent(self):
        self.assertIsNone(m.notify_event({"state": "stopped", "error": ""}))

    def test_restart_interruption_is_silent(self):
        """v4.11：重启容器把在传任务标成 stopped + 「控制台重启过…」，那不是任务故障，别推「任务失败」。"""
        for err in ("控制台重启过，下载已中断（点「继续」会跳过已完成文件接着传）",
                    "控制台重启过，解压已中断（重新提交即可，已解出的文件会保留）",
                    "守护进程重启过，任务已中断（点继续即可续传）"):
            self.assertIsNone(m.notify_event({"state": "stopped", "error": err}))

    def test_pause_cleanup_is_silent(self):
        """v4.15：暂停收尾会写「已暂停：还有 N 个文件未完成…」带 error 的 stopped，这不是失败。"""
        err = "已暂停：还有 4 个文件未完成，点「继续」接着下（已完成/已跳过的不会重下）"
        self.assertIsNone(m.notify_event({"state": "stopped", "error": err}))

    def test_running_and_queued_are_silent(self):
        for st in ("running", "queued", "paused", "stopping"):
            self.assertIsNone(m.notify_event({"state": st}))

    def test_empty_job_is_silent(self):
        self.assertIsNone(m.notify_event(None))
        self.assertIsNone(m.notify_event({}))


class NotifySigTest(unittest.TestCase):
    def test_sig_ignores_bytes(self):
        """v4.15：任务 error 后残留传输还在推字节，绝不能把字节算进「有没有变化」。"""
        a = {"id": 1, "state": "error", "bytes": 100, "error": "captcha"}
        b = dict(a, bytes=260)
        self.assertEqual(m.notify_sig(a), m.notify_sig(b))

    def test_sig_is_error_only(self):
        self.assertEqual(m.notify_sig({"state": "done", "error": "  "}), "")
        self.assertEqual(m.notify_sig({"state": "done", "error": "x" * 100}), "x" * 60)
        self.assertEqual(m.notify_sig(None), "")


class TokenTest(unittest.TestCase):
    def test_token_shape(self):
        self.assertTrue(m.pushplus_token_ok("721edd86af47452181dc72198291881b"))
        self.assertFalse(m.pushplus_token_ok(""))
        self.assertFalse(m.pushplus_token_ok("https://www.pushplus.plus/send"))
        self.assertFalse(m.pushplus_token_ok("short"))
        self.assertFalse(m.pushplus_token_ok("x" * 80))

    def test_norm_settings_clears_bad_token(self):
        s = m._norm_settings({"pushplusOn": "abc", "pushplusToken": " https://bad "})
        self.assertFalse(s["pushplusOn"])
        self.assertEqual(s["pushplusToken"], "")
        ok = m._norm_settings({"pushplusOn": True, "pushplusToken": "721edd86af47452181dc72198291881b"})
        self.assertTrue(ok["pushplusOn"])
        self.assertEqual(ok["pushplusToken"], "721edd86af47452181dc72198291881b")


class NotifyContentTest(unittest.TestCase):
    def test_title_and_content_escape_html(self):
        job = {"id": 7, "remote": "会员号", "subpath": "pack/<evil>&", "dest": "/data/x",
               "filesDone": 3, "filesTotal": 3, "bytes": 1024 ** 3, "total": 1024 ** 3, "state": "done"}
        title = m.notify_title(job, "success")
        self.assertIn("下载完成", title)
        content = m.notify_content(job, "success", "2026-11-25 10:00")
        self.assertNotIn("<evil>", content)      # 任务名里的尖括号必须转义
        self.assertIn("&lt;evil&gt;", content)

    def test_fail_content_contains_error(self):
        job = {"id": 8, "state": "error", "error": "captcha token 缺失", "remote": "会员号", "subpath": "a"}
        content = m.notify_content(job, "fail", "2026-11-25 10:00")
        self.assertIn("captcha token", content)
        self.assertIn("任务失败", m.notify_title(job, "fail"))

    def test_dir_job_shows_file_counts(self):
        job = {"id": 9, "state": "done", "filesDone": 3, "filesTotal": 3, "filesFailed": 0}
        self.assertIn("3/3 个", m.notify_content(job, "success", "2026-11-25 10:00"))

    def test_dir_job_shows_failed_count(self):
        job = {"id": 10, "state": "error", "filesDone": 5, "filesTotal": 7,
               "filesFailed": 2, "error": "2 个文件失败"}
        content = m.notify_content(job, "fail", "2026-11-25 10:00")
        self.assertIn("5/7 个（失败 2）", content)

    def test_legacy_job_omits_file_row(self):
        # v4.11：rclone 老任务没有文件级计数，不能显示「文件：0/0 个」
        job = {"id": 11, "state": "done", "remote": "会员号", "subpath": "a/b"}
        content = m.notify_content(job, "success", "2026-11-25 10:00")
        self.assertNotIn("<b>文件</b>", content)
        self.assertNotIn("0/0", content)

    def test_single_file_job_says_one_file(self):
        job = {"id": 12, "state": "done", "single_file": True, "bytes": 123456}
        self.assertIn("<b>文件</b>：1 个", m.notify_content(job, "success", "2026-11-25 10:00"))


class NotifyScanTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._orig = (m.load_settings, m.load_jobs, m.NOTIFY_STATE_FILE, m.NOTIFY_SEND_GAP,
                      m.NOTIFY_BACKOFF)
        m.NOTIFY_STATE_FILE = os.path.join(self.tmp, "notify-state.json")
        m.NOTIFY_SEND_GAP = 0        # 测试里不必真的间隔 1 秒
        m.NOTIFY_BACKOFF.clear()
        m.NOTIFY_BACKOFF.update({"until": 0.0, "fails": 0})
        self.sent = []

    def tearDown(self):
        (m.load_settings, m.load_jobs, m.NOTIFY_STATE_FILE,
         m.NOTIFY_SEND_GAP, m.NOTIFY_BACKOFF) = self._orig

    def _settings(self, on=True, token="721edd86af47452181dc72198291881b"):
        m.load_settings = lambda: {"pushplusOn": on, "pushplusToken": token}

    def _jobs(self, jobs):
        m.load_jobs = lambda: list(jobs)

    def test_first_scan_seeds_without_sending(self):
        self._settings()
        self._jobs([{"id": 1, "state": "done", "bytes": 10}])
        r = m.notify_scan(send=lambda *a: self.sent.append(a) or {"code": 200})
        self.assertTrue(r["seeded"])
        self.assertEqual(r["sent"], 0)
        self.assertEqual(self.sent, [])
        self.assertTrue(os.path.exists(m.NOTIFY_STATE_FILE))

    def test_new_terminal_state_sends_once(self):
        self._settings()
        self._jobs([{"id": 1, "state": "done", "bytes": 10}])
        m.notify_scan(send=lambda *a: {"code": 200})            # 播种
        self._jobs([{"id": 1, "state": "done", "bytes": 10},
                    {"id": 2, "state": "done", "bytes": 20}])
        r = m.notify_scan(send=lambda tok, t, c: self.sent.append(t) or {"code": 200})
        self.assertEqual(r["sent"], 1)
        self.assertEqual(len(self.sent), 1)
        r2 = m.notify_scan(send=lambda tok, t, c: self.sent.append(t) or {"code": 200})
        self.assertEqual(r2["sent"], 0)                          # 同一状态不重复发

    def test_disabled_does_nothing(self):
        self._settings(on=False)
        self._jobs([{"id": 1, "state": "error", "error": "boom"}])
        r = m.notify_scan(send=lambda *a: self.sent.append(a) or {"code": 200})
        self.assertFalse(r["enabled"])
        self.assertEqual(r["sent"], 0)

    def test_bad_token_reports_error_without_sending(self):
        self._settings(token="")
        self._jobs([{"id": 1, "state": "done"}])
        r = m.notify_scan(send=lambda *a: self.sent.append(a) or {"code": 200})
        self.assertEqual(r["sent"], 0)
        self.assertTrue(r["errors"])

    def test_pushplus_failure_is_reported_but_state_advances(self):
        self._settings()
        self._jobs([{"id": 1, "state": "done"}])
        m.notify_scan(send=lambda *a: {"code": 200})
        self._jobs([{"id": 3, "state": "error", "error": "x"}])
        r = m.notify_scan(send=lambda *a: {"code": 903, "msg": "token 无效"})
        self.assertEqual(r["sent"], 0)
        self.assertTrue(any("token 无效" in e for e in r["errors"]))
        # 已经登记过，不会因为发送失败每分钟重试刷屏
        r2 = m.notify_scan(send=lambda *a: {"code": 200})
        self.assertEqual(r2["sent"], 0)

    def test_bytes_growth_does_not_resend(self):
        """v4.15 回归：error 后 bytes 一直在涨（残留传输推进），不能每 5 秒发一条。"""
        self._settings()
        self._jobs([{"id": 5, "state": "done", "bytes": 1}])
        m.notify_scan(send=lambda *a: {"code": 200})          # 播种
        n = 0
        for i in range(10):
            self._jobs([{"id": 9, "state": "error", "error": "captcha", "bytes": 322319502548 + i * 2100000}])
            r = m.notify_scan(send=lambda *a: self.sent.append(a) or {"code": 200})
            n += r["sent"]
        self.assertEqual(n, 1)                                 # 只发一次
        self.assertEqual(len(self.sent), 1)

    def test_done_never_repeats_after_cooldown(self):
        """v4.19 回归：任务停在 done，冷却期一过不许再推一遍（v4.15 是每 30 分钟重播）。"""
        self._settings()
        self._jobs([{"id": 7, "state": "running"}])
        m.notify_scan(send=lambda *a: {"code": 200})
        t0 = 1000000
        self._jobs([{"id": 7, "state": "done"}])
        r1 = m.notify_scan(send=lambda *a: self.sent.append(a) or {"code": 200}, now=t0)
        r2 = m.notify_scan(send=lambda *a: self.sent.append(a) or {"code": 200},
                           now=t0 + m.NOTIFY_REPEAT_SEC + 1)
        r3 = m.notify_scan(send=lambda *a: self.sent.append(a) or {"code": 200},
                           now=t0 + 10 * m.NOTIFY_REPEAT_SEC)
        self.assertEqual((r1["sent"], r2["sent"], r3["sent"]), (1, 0, 0))
        self.assertEqual(len(self.sent), 1)

    def test_done_error_clear_does_not_repeat(self):
        """完成文案里的「N 个文件失败」被清空只是文案变化，不是新的完成事件。"""
        self._settings()
        self._jobs([{"id": 8, "state": "running"}])
        m.notify_scan(send=lambda *a: {"code": 200})
        self._jobs([{"id": 8, "state": "done",
                     "error": "3 个文件失败，其余 209 个已完成"}])
        r1 = m.notify_scan(send=lambda *a: self.sent.append(a) or {"code": 200})
        self._jobs([{"id": 8, "state": "done", "error": ""}])
        r2 = m.notify_scan(send=lambda *a: self.sent.append(a) or {"code": 200})
        self.assertEqual(r1["sent"], 1)
        self.assertEqual(r2["sent"], 0)
        self.assertEqual(len(self.sent), 1)

    def test_resume_then_done_again_notifies_once_more(self):
        """真的又下完一轮（done → running → done）应该再通知一次。"""
        self._settings()
        self._jobs([{"id": 9, "state": "running"}])
        m.notify_scan(send=lambda *a: {"code": 200})
        self._jobs([{"id": 9, "state": "done"}])
        self.assertEqual(m.notify_scan(send=lambda *a: self.sent.append(a) or {"code": 200})["sent"], 1)
        self._jobs([{"id": 9, "state": "running"}])
        m.notify_scan(send=lambda *a: {"code": 200})
        self._jobs([{"id": 9, "state": "done"}])
        self.assertEqual(m.notify_scan(send=lambda *a: self.sent.append(a) or {"code": 200})["sent"], 1)
        self.assertEqual(len(self.sent), 2)

    def test_legacy_state_file_is_migrated_without_resending(self):
        """从 v4.15 的老平表升级：只登记现状，不再把历史终态补发一遍。"""
        self._settings()
        old = {"1|done|": 1000000, "2|error|boom": 1000000}
        with open(m.NOTIFY_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(old, f)
        self._jobs([{"id": 1, "state": "done"}, {"id": 2, "state": "error", "error": "boom"}])
        r = m.notify_scan(send=lambda *a: self.sent.append(a) or {"code": 200})
        self.assertTrue(r["seeded"])
        self.assertEqual(r["sent"], 0)
        self.assertEqual(self.sent, [])
        with open(m.NOTIFY_STATE_FILE, encoding="utf-8") as f:
            d = json.load(f)
        self.assertEqual(d["v"], 2)
        self.assertIn("1", d["jobs"])
        # 迁移之后状态没变也不许再发
        r2 = m.notify_scan(send=lambda *a: self.sent.append(a) or {"code": 200})
        self.assertEqual(r2["sent"], 0)

    def test_same_failure_cause_after_cooldown_notifies_again(self):
        """同一错因隔了很久又失败（中间没离开过 error）应该再通知一条，但不许更密。"""
        self._settings()
        self._jobs([{"id": 7, "state": "running"}])
        m.notify_scan(send=lambda *a: {"code": 200})
        t0 = int(time.time())          # 播种那一轮用的是真实时间，这里必须接在它后面
        self._jobs([{"id": 7, "state": "error", "error": "captcha"}])
        r1 = m.notify_scan(send=lambda *a: self.sent.append(a) or {"code": 200}, now=t0)
        r2 = m.notify_scan(send=lambda *a: self.sent.append(a) or {"code": 200},
                           now=t0 + 5)
        r3 = m.notify_scan(send=lambda *a: self.sent.append(a) or {"code": 200},
                           now=t0 + m.NOTIFY_REPEAT_SEC + 1)
        self.assertEqual(r1["sent"], 1)
        self.assertEqual(r2["sent"], 0)
        self.assertLessEqual(r3["sent"], 1)

    def test_failure_backoff_blocks_next_scan(self):
        """PushPlus 限流（服务端验证错误）后退避，别继续 5 秒一条撞上去。"""
        self._settings()
        self._jobs([{"id": 1, "state": "done"}])
        m.notify_scan(send=lambda *a: {"code": 200})
        self._jobs([{"id": 11, "state": "error", "error": "boom"}])
        r = m.notify_scan(send=lambda *a: {"code": 999, "msg": "服务端验证错误"}, now=5000)
        self.assertEqual(r["sent"], 0)
        self.assertGreater(r["backoff"], 0)
        r2 = m.notify_scan(send=lambda *a: {"code": 200}, now=5001)
        self.assertEqual(r2["sent"], 0)
        self.assertGreater(r2["backoff"], 0)
        r3 = m.notify_scan(send=lambda *a: self.sent.append(a) or {"code": 200},
                           now=5000 + m.NOTIFY_BACKOFF_BASE + 5)
        self.assertEqual(r3["sent"], 1)                        # 退避结束后补发，不丢通知

    def test_max_per_scan(self):
        """一轮最多发 NOTIFY_MAX_PER_SCAN 条，避免一次事故刷屏。"""
        self._settings()
        self._jobs([{"id": 0, "state": "done"}])
        m.notify_scan(send=lambda *a: {"code": 200})
        jobs = [{"id": 100 + i, "state": "error", "error": "e%d" % i} for i in range(6)]
        self._jobs(jobs)
        r = m.notify_scan(send=lambda *a: self.sent.append(a) or {"code": 200})
        self.assertEqual(r["sent"], m.NOTIFY_MAX_PER_SCAN)


if __name__ == "__main__":
    unittest.main()
