"""v3.98 jobs 轮询不再被 LOCK 卡住的纯逻辑测试。

背景（2026-11-23）：用户经常看到「后端服务连接失败：请求超时（8 秒）—— 每 2 秒自动重试」。
根因：`jobs` 处理整段拿全局 LOCK，而 job_live 里每个运行中任务要串行问两次 rclone
（job/status + core/stats，各 8 秒上限）。任务多或 rc 变慢时列表接口轻松超过前端 8 秒，
同时 LOCK 被占着，连「停止」也要排队。

修法：LOCK 只在读盘/写盘瞬间持有；job_live 在锁外跑；写回时只合并进度字段。
"""
import importlib.util
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(os.path.dirname(HERE), "download-console.py")

_spec = importlib.util.spec_from_file_location("console_under_test_jobs", APP)
m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)


class MergeLiveJobTest(unittest.TestCase):
    def test_progress_fields_copied(self):
        tgt = {"id": 1, "state": "running", "dest": "a:b", "bytes": 10}
        src = {"id": 1, "state": "running", "dest": "WRONG", "bytes": 99,
               "speed": 5, "curfile": "x.mp4", "eta": "1m"}
        m._merge_live_job(tgt, src)
        self.assertEqual(tgt["bytes"], 99)
        self.assertEqual(tgt["speed"], 5)
        self.assertEqual(tgt["curfile"], "x.mp4")
        self.assertEqual(tgt["dest"], "a:b")      # 非进度字段不许被覆盖

    def test_state_change_applies_while_running(self):
        tgt = {"id": 1, "state": "running"}
        src = {"id": 1, "state": "done", "error": ""}
        m._merge_live_job(tgt, src)
        self.assertEqual(tgt["state"], "done")

    def test_state_change_ignored_after_user_stop(self):
        """用户刚点了停止，迟到的 job_live 不能把它改回 running。"""
        tgt = {"id": 1, "state": "stopped", "speed": 0}
        src = {"id": 1, "state": "running", "speed": 123}
        m._merge_live_job(tgt, src)
        self.assertEqual(tgt["state"], "stopped")
        self.assertEqual(tgt["speed"], 123)       # 速度这类字段仍可更新

    def test_error_copied(self):
        tgt = {"id": 1, "state": "running", "error": ""}
        src = {"id": 1, "state": "error", "error": "captcha"}
        m._merge_live_job(tgt, src)
        self.assertEqual(tgt["error"], "captcha")


class ConstantsTest(unittest.TestCase):
    def test_rc_timeout_shorter_than_budget(self):
        # 单个 rc 调用必须显著小于整轮预算，否则两三个任务就撑爆 8 秒看门狗
        self.assertLessEqual(m.JOB_LIVE_TMO, 4.0)
        self.assertGreaterEqual(m.JOBS_LIVE_TTL, 1.0)


if __name__ == "__main__":
    unittest.main()
