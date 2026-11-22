#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v3.90：单文件复制异步化 + 暂停可中断。

覆盖：
- copyfile 以 _async 提交并轮询 job/status 直到成功；
- 暂停事件触发 job/stop，传输真正被取消并返回 pending（不再残留在后台）；
- 异步 job 的错误文本仍走原有 classify_error 分类；
- 对端忽略 _async（无 jobid）时按同步完成处理，保持兼容。

运行：python -m unittest discover -s tests -v
"""
import importlib.util
import os
import threading
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("console", os.path.join(ROOT, "download-console.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def make_job(jid=1):
    return {"id": jid, "jobid": None, "remote": "pk", "subpath": "dir", "src": "pk:dir",
            "dest": "/data/dir", "mode": "copy", "naming": "v2", "proxy": False,
            "state": "running", "bytes": 0, "total": 0, "speed": 0, "eta": None, "error": ""}


def make_entry(name="a.mp4", size=100):
    return {"src": name, "srcFull": "dir/" + name, "dst": name, "size": size,
            "status": "pending", "error": "", "attempts": 0}


class FakeAsyncRclone:
    """异步版假 rclone：copyfile 只发 jobid，结果由 job/status 决定。"""

    def __init__(self, statuses=(), on_status=None, no_jobid=False):
        self.calls = []
        self.statuses = list(statuses)
        self.on_status = on_status
        self.no_jobid = no_jobid
        self.stop_calls = 0

    def __call__(self, method, **kv):
        for k in ("_timeout", "_strict", "_proxy"):
            kv.pop(k, None)
        self.calls.append((method, dict(kv)))
        if method == "operations/copyfile":
            return {} if self.no_jobid else {"jobid": 42}
        if method == "job/status":
            if self.on_status:
                self.on_status()
            if self.statuses:
                return self.statuses.pop(0)
            return {"finished": True, "success": True}
        if method == "job/stop":
            self.stop_calls += 1
            return {}
        raise RuntimeError("unexpected rc method " + method)


class AsyncCopyTest(unittest.TestCase):
    def setUp(self):
        self.old_rc = m.rc
        self.old_poll = m.COPY_POLL_INTERVAL
        m.COPY_POLL_INTERVAL = 0.01
        self.settings = dict(m.DEFAULTS, fileRetryMax=0)
        with m.SAFE_JOBS_LOCK:
            m.SAFE_JOBS[1] = {"stop": threading.Event()}

    def tearDown(self):
        m.rc = self.old_rc
        m.COPY_POLL_INTERVAL = self.old_poll
        with m.SAFE_JOBS_LOCK:
            m.SAFE_JOBS.pop(1, None)

    def run_one(self, fake, entry=None):
        m.rc = fake
        return m.transfer_one(make_job(), entry or make_entry(), self.settings, {}, set())

    def test_async_success_uses_job_polling(self):
        fake = FakeAsyncRclone(statuses=[{"finished": False}, {"finished": False}])
        st, err = self.run_one(fake)
        self.assertEqual((st, err), ("done", ""))
        copies = [c for c in fake.calls if c[0] == "operations/copyfile"]
        self.assertEqual(len(copies), 1)
        self.assertEqual(copies[0][1].get("_async"), "true")
        self.assertIn("MultiThreadStreams", copies[0][1].get("_config", ""))
        self.assertTrue(any(c[0] == "job/status" for c in fake.calls))
        self.assertEqual(fake.stop_calls, 0)

    def test_stop_really_cancels_inflight_copy(self):
        # 第一次查状态时尚未完成，同时用户点了暂停：必须调 job/stop 并返回 pending
        with m.SAFE_JOBS_LOCK:
            m.SAFE_JOBS[1]["stop"].clear()

        def user_hits_pause():
            with m.SAFE_JOBS_LOCK:
                m.SAFE_JOBS[1]["stop"].set()

        fake = FakeAsyncRclone(statuses=[{"finished": False}] * 50, on_status=user_hits_pause)
        st, _ = self.run_one(fake)
        self.assertEqual(st, "pending")
        self.assertEqual(fake.stop_calls, 1)

    def test_async_error_still_classified(self):
        fake = FakeAsyncRclone(statuses=[{"finished": True, "success": False,
                                          "error": "file name too long"}])
        st, err = self.run_one(fake)
        self.assertEqual(st, "failed")
        self.assertTrue(err)

    def test_no_jobid_falls_back_to_sync_done(self):
        fake = FakeAsyncRclone(no_jobid=True)
        st, _ = self.run_one(fake)
        self.assertEqual(st, "done")


if __name__ == "__main__":
    unittest.main()
