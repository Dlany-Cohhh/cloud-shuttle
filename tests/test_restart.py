"""v4.17 重复启动保护的测试。

背景：同一个目录任务被「继续 / 开始」两次时，旧线程还卡在传输池里没退，
新线程已经带着一套全新的 entries 从另一个基线开始算，两代交替写同一个计划文件
和 bytes，界面就出现「进度条一会长一会短、完成文件数从 5 掉回 2」。

修法：_start_safe_dir_job 先置旧停止信号 + 按组取消在飞 job，等旧线程真正退出，
等不到就拒绝启动（返回 False 并写 error）。
"""
import importlib.util
import os
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(os.path.dirname(HERE), "download-console.py")

_spec = importlib.util.spec_from_file_location("console_under_test_restart", APP)
m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)


def _job(jid=1, proxy=False):
    return {"id": jid, "proxy": proxy, "naming": "v2", "single_file": False,
            "kind": "download", "state": "stopped", "error": "", "jobid": "old"}


class RestartGuardTest(unittest.TestCase):
    def setUp(self):
        self._orig_worker = m._safe_dir_worker
        self._orig_stop = m.stop_job_group
        self._orig_wait = m.SAFE_RESTART_WAIT
        self._orig_threads = dict(m.SAFE_THREADS)
        self._orig_jobs = dict(m.SAFE_JOBS)
        m.stop_job_group = lambda *a, **k: True
        m.SAFE_RESTART_WAIT = 2
        m.SAFE_JOBS.clear()
        m.SAFE_THREADS.clear()
        self.exited = []

    def tearDown(self):
        m._safe_dir_worker = self._orig_worker
        m.stop_job_group = self._orig_stop
        m.SAFE_RESTART_WAIT = self._orig_wait
        m.SAFE_JOBS.clear()
        m.SAFE_JOBS.update(self._orig_jobs)
        m.SAFE_THREADS.clear()
        m.SAFE_THREADS.update(self._orig_threads)

    def _worker_that_obeys_stop(self, job_id, snapshot=None):
        while not m._safe_requested_stop(job_id):
            time.sleep(0.02)
        self.exited.append(job_id)

    def test_second_start_waits_until_old_exits(self):
        """旧线程会看停信号：第二次启动必须等它退出，且只留一个世代。"""
        m._safe_dir_worker = self._worker_that_obeys_stop
        job = _job(1)
        self.assertTrue(m._start_safe_dir_job(job, {}))
        th1 = m.SAFE_THREADS[1]
        time.sleep(0.2)
        self.assertTrue(th1.is_alive())
        # 第二次启动：应该先把旧线程停掉、等它退出，再起新的
        job2 = _job(1)
        self.assertTrue(m._start_safe_dir_job(job2, {}))
        self.assertFalse(th1.is_alive())
        self.assertEqual(self.exited, [1])
        self.assertTrue(m.SAFE_THREADS[1].is_alive())
        self.assertFalse(m.SAFE_JOBS[1]["stop"].is_set())   # 新世代的信号是干净的
        m.SAFE_JOBS[1]["stop"].set()
        m.SAFE_THREADS[1].join(timeout=2)

    def test_second_start_refuses_when_old_hangs(self):
        """旧线程不理会停信号（卡在收尾）：不硬起第二世代，改为排队等 scheduler 重试。"""
        m._safe_dir_worker = lambda job_id, snapshot=None: time.sleep(3)
        m.SAFE_RESTART_WAIT = 0.3
        job = _job(2)
        self.assertTrue(m._start_safe_dir_job(job, {}))
        time.sleep(0.1)
        job2 = _job(2)
        self.assertFalse(m._start_safe_dir_job(job2, {}))
        self.assertIn("还在收尾", job2["error"])
        self.assertEqual(job2["state"], "queued")     # v4.18：排回队列自动重试
        # 没有第二个线程抢进来
        self.assertEqual(len(m.SAFE_THREADS), 1)


if __name__ == "__main__":
    unittest.main()
