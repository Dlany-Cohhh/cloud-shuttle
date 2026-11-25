"""v4.13：失败任务自动重试。只测「什么时候该重试」，不碰真实的上传/下载。"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import importlib.util
spec = importlib.util.spec_from_file_location("dc", os.path.join(ROOT, "download-console.py"))
m = importlib.util.module_from_spec(spec)
sys.modules["dc"] = m
spec.loader.exec_module(m)

T0 = 1_795_000_000


class AutoRetryTest(unittest.TestCase):
    def setUp(self):
        self.jobs = []
        self.orig = (m.load_settings, m.load_jobs, m.save_jobs)
        m.load_jobs = lambda: list(self.jobs)
        m.save_jobs = lambda jobs: None       # 内存里改，不落盘
        self.calls = []

    def tearDown(self):
        m.load_settings, m.load_jobs, m.save_jobs = self.orig

    def _settings(self, on=True, minutes=10, maxn=3):
        m.load_settings = lambda: {"autoRetryOn": on, "autoRetryMin": minutes, "autoRetryMax": maxn}

    def _resume_ok(self, jid):
        self.calls.append(jid)
        return (True, "")

    def test_disabled_does_nothing(self):
        self._settings(on=False)
        self.jobs = [{"id": 1, "state": "error", "error": "connection reset"}]
        r = m.auto_retry_scan(now=T0, resume=self._resume_ok)
        self.assertFalse(r["on"])
        self.assertEqual(r["retried"], 0)
        self.assertEqual(self.calls, [])

    def test_first_sighting_only_stamps_time(self):
        """刚看到失败不该立刻重跑，先记下时间点，等满 N 分钟。"""
        self._settings()
        self.jobs = [{"id": 1, "state": "error", "error": "connection reset"}]
        r = m.auto_retry_scan(now=T0, resume=self._resume_ok)
        self.assertEqual(r["retried"], 0)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.jobs[0]["_arErrAt"], T0)

    def test_retries_after_wait(self):
        self._settings(minutes=10)
        self.jobs = [{"id": 1, "state": "error", "error": "connection reset", "_arErrAt": T0}]
        r = m.auto_retry_scan(now=T0 + 10 * 60, resume=self._resume_ok)
        self.assertEqual(r["retried"], 1)
        self.assertEqual(self.calls, [1])
        self.assertEqual(self.jobs[0]["_arTries"], 1)
        self.assertIsNone(self.jobs[0]["_arErrAt"])     # 下次再失败要重新计时

    def test_not_yet_due(self):
        self._settings(minutes=10)
        self.jobs = [{"id": 1, "state": "error", "error": "boom", "_arErrAt": T0}]
        r = m.auto_retry_scan(now=T0 + 9 * 60 + 30, resume=self._resume_ok)
        self.assertEqual(r["retried"], 0)
        self.assertEqual(r["waiting"], [1])

    def test_stops_after_max(self):
        self._settings(maxn=3)
        self.jobs = [{"id": 1, "state": "error", "error": "boom", "_arErrAt": T0, "_arTries": 3}]
        r = m.auto_retry_scan(now=T0 + 3600, resume=self._resume_ok)
        self.assertEqual(r["retried"], 0)
        self.assertEqual(r["skipped"], 1)

    def test_zero_max_never_retries(self):
        self._settings(maxn=0)
        self.jobs = [{"id": 1, "state": "error", "error": "boom", "_arErrAt": T0}]
        r = m.auto_retry_scan(now=T0 + 3600, resume=self._resume_ok)
        self.assertEqual(r["retried"], 0)

    def test_skips_errors_that_retry_cannot_fix(self):
        """账号失效 / 人机验证 / 额度用尽 / 磁盘保护：重试只会白等，还可能把风控刷得更狠。"""
        self._settings()
        errs = ["refresh token 已失效，请重新登录", "需要人机验证（captcha）",
                "当日下载额度已用尽", "目标存储空间保护中（剩余 3.1 GB）"]
        self.jobs = [{"id": i, "state": "error", "error": e, "_arErrAt": T0}
                     for i, e in enumerate(errs)]
        r = m.auto_retry_scan(now=T0 + 3600, resume=self._resume_ok)
        self.assertEqual(r["retried"], 0)
        self.assertEqual(r["skipped"], 4)
        self.assertEqual(self.calls, [])

    def test_done_clears_tries(self):
        self._settings()
        self.jobs = [{"id": 1, "state": "done", "_arTries": 2, "_arErrAt": T0}]
        r = m.auto_retry_scan(now=T0 + 3600, resume=self._resume_ok)
        self.assertEqual(r["cleared"], 1)
        self.assertEqual(self.jobs[0]["_arTries"], 0)

    def test_worker_busy_does_not_burn_attempt(self):
        self._settings()
        self.jobs = [{"id": 1, "state": "error", "error": "boom", "_arErrAt": T0}]

        def busy(jid):
            return (False, "该任务的旧工作线程还没退出（正在中断在飞文件），等几秒再点「继续」")

        r = m.auto_retry_scan(now=T0 + 3600, resume=busy)
        self.assertEqual(r["retried"], 0)
        self.assertEqual(self.jobs[0].get("_arTries", 0), 0)   # 不算一次尝试
        self.assertEqual(self.jobs[0]["_arErrAt"], T0 + 3600)  # 下轮再看

    def test_resume_error_consumes_attempt(self):
        """点不动（磁盘保护 / 源目录已空）：算一次尝试，别每 30 秒撞一次。"""
        self._settings()
        self.jobs = [{"id": 1, "state": "error", "error": "boom", "_arErrAt": T0}]
        r = m.auto_retry_scan(now=T0 + 3600, resume=lambda jid: {"error": "预检源目录失败"})
        self.assertEqual(r["retried"], 0)
        self.assertEqual(self.jobs[0]["_arTries"], 1)
        self.assertTrue(r["errors"])

    def test_api_shape_is_understood(self):
        """默认实现拿到的是 /api 的 JSON，{ok:true} 不含 error 就要认成功。"""
        self._settings()
        self.jobs = [{"id": 1, "state": "error", "error": "boom", "_arErrAt": T0}]
        r = m.auto_retry_scan(now=T0 + 3600, resume=lambda jid: {"ok": True})
        self.assertEqual(r["retried"], 1)


if __name__ == "__main__":
    unittest.main()
