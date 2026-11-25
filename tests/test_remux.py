"""v4.04 下载后自动转封装（MPEG-TS → MP4）的纯逻辑测试。

背景（2026-11-23 现场）：用户反馈「下载的文件不能转化为 mp4」。查下来不是我们
改名丢了扩展名，而是 PikPak 上那批资源**本身就是无扩展名的 MPEG-TS 流**（HLS
切片拼出来的），落盘后播放器认不出。实测这类 TS 是 H.264 + AAC，正好是 MP4 的
原生组合，所以 `-c copy` 无损转封装即可：1.90 GB / 34 秒，体积还小 8%。

这里要守住的是「不损坏用户数据」和「别在没条件时瞎跑」：
  1) 只认 TS，MP4/MKV 一律不碰（误转会把能播的文件弄坏）；
  2) 输出走 .part → 原子改名，中途失败不能留下半个 mp4 顶替原文件；
  3) 默认保留源文件 —— 用户确认能播之前不替人做删除决定；
  4) 容器内没有 ffmpeg 时安静降级，不能抛异常把下载流程带崩。
"""
import importlib.util
import os
import subprocess
import tempfile
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(os.path.dirname(HERE), "download-console.py")

_spec = importlib.util.spec_from_file_location("console_under_test_remux", APP)
m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)


def ts_head(n=400):
    """造一段看起来像 TS 的字节：每 188 字节一个 0x47 同步头。"""
    b = bytearray(n)
    for i in range(0, n, 188):
        b[i] = 0x47
    return bytes(b)


def mp4_head():
    return b"\x00\x00\x00\x20ftypisom" + b"\x00" * 400


class SniffTest(unittest.TestCase):
    def test_ts_detected(self):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(ts_head())
            p = f.name
        try:
            self.assertEqual(m.sniff_media_container(p), "ts")
        finally:
            os.unlink(p)

    def test_mp4_detected(self):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(mp4_head())
            p = f.name
        try:
            self.assertEqual(m.sniff_media_container(p), "mp4")
        finally:
            os.unlink(p)

    def test_unknown_returns_none(self):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"hello world" * 60)
            p = f.name
        try:
            self.assertIsNone(m.sniff_media_container(p))
        finally:
            os.unlink(p)

    def test_missing_file_is_none_not_crash(self):
        self.assertIsNone(m.sniff_media_container("/nonexistent/xyz"))


class FfmpegProbeTest(unittest.TestCase):
    def setUp(self):
        m._FFMPEG.update({"probed": False, "bin": None, "why": "", "at": 0.0})

    def test_missing_returns_none_with_reason(self):
        with mock.patch.object(m.shutil, "which", return_value=None), \
             mock.patch.object(m.os.path, "isfile", return_value=False):
            self.assertIsNone(m.ffmpeg_bin())
        self.assertIn("ffmpeg", m._FFMPEG["why"])

    def test_negative_cache_expires(self):
        """先起服务、后装 ffmpeg 的场景：负结果不能缓存一辈子。"""
        with mock.patch.object(m.shutil, "which", return_value=None), \
             mock.patch.object(m.os.path, "isfile", return_value=False):
            self.assertIsNone(m.ffmpeg_bin())
        with mock.patch.object(m.shutil, "which", return_value="/usr/bin/ffmpeg"), \
             mock.patch.object(m.os.path, "isfile", return_value=False):
            # 60 秒内仍是旧结论（缓存生效）
            self.assertIsNone(m.ffmpeg_bin())
            m._FFMPEG["at"] = time.time() - 61
            self.assertEqual(m.ffmpeg_bin(), "/usr/bin/ffmpeg")

    def test_no_ffmpeg_gives_clean_failure(self):
        with mock.patch.object(m, "ffmpeg_bin", return_value=None):
            ok, out, msg = m.remux_to_mp4("/tmp/whatever.ts")
        self.assertFalse(ok)
        self.assertEqual(out, "")
        self.assertTrue(msg)


class RemuxCommandTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.src = os.path.join(self.dir, "noext")
        with open(self.src, "wb") as f:
            f.write(ts_head(188 * 8))

    def tearDown(self):
        for n in os.listdir(self.dir):
            try:
                os.unlink(os.path.join(self.dir, n))
            except Exception:
                pass
        os.rmdir(self.dir)

    def _run(self, fake, dur=(0.0, 0.0)):
        with mock.patch.object(m, "ffmpeg_bin", return_value="/usr/bin/ffmpeg"), \
             mock.patch.object(m, "media_duration", side_effect=list(dur)), \
             mock.patch.object(m.subprocess, "run", side_effect=fake) as run:
            res = m.remux_to_mp4(self.src, {"remuxTimeoutSec": 60})
        return res, run

    def test_maps_only_first_video_and_audio(self):
        def fake(cmd, **kw):
            open(cmd[-1], "wb").write(b"x" * 16)
            return mock.Mock(returncode=0, stdout=b"")
        res, run = self._run(fake)
        self.assertTrue(res[0], res[2])
        cmd = run.call_args[0][0]
        self.assertIn("-c", cmd)
        self.assertEqual(cmd[cmd.index("-c") + 1], "copy")
        self.assertEqual(cmd[cmd.index("-map") + 1], "0:v:0")
        self.assertEqual(cmd[cmd.index("-map", cmd.index("-map") + 1) + 1], "0:a:0")

    def test_output_name_appends_mp4(self):
        def fake(cmd, **kw):
            open(cmd[-1], "wb").write(b"x" * 16)
            return mock.Mock(returncode=0, stdout=b"")
        ok, out, _ = self._run(fake)[0]
        self.assertTrue(ok)
        self.assertEqual(out, self.src + ".mp4")
        self.assertTrue(os.path.isfile(out))

    def test_dot_ts_is_replaced_not_appended(self):
        src2 = os.path.join(self.dir, "a.ts")
        os.rename(self.src, src2)
        self.src = src2

        def fake(cmd, **kw):
            open(cmd[-1], "wb").write(b"x" * 16)
            return mock.Mock(returncode=0, stdout=b"")
        ok, out, _ = self._run(fake)[0]
        self.assertTrue(ok)
        self.assertEqual(out, src2[:-3] + ".mp4")

    def test_existing_mp4_is_not_overwritten(self):
        out = self.src + ".mp4"
        open(out, "wb").write(b"keepme")
        ok, path, msg = self._run(lambda *a, **k: (_ for _ in ()).throw(AssertionError("不该调用 ffmpeg")))[0]
        self.assertTrue(ok)
        self.assertEqual(path, out)
        self.assertEqual(open(out, "rb").read(), b"keepme")

    def test_failure_leaves_no_part_file(self):
        def fake(cmd, **kw):
            open(cmd[-1], "wb").write(b"half")
            return mock.Mock(returncode=1, stdout=b"boom")
        ok, _, msg = self._run(fake)[0]
        self.assertFalse(ok)
        self.assertIn("退出码 1", msg)
        self.assertEqual([n for n in os.listdir(self.dir) if n.endswith(".part")], [])

    def test_timeout_cleans_part(self):
        def fake(cmd, **kw):
            open(cmd[-1], "wb").write(b"half")
            raise subprocess.TimeoutExpired(cmd, 60)
        ok, _, msg = self._run(fake)[0]
        self.assertFalse(ok)
        self.assertIn("超时", msg)
        self.assertEqual([n for n in os.listdir(self.dir) if n.endswith(".part")], [])

    def test_success_but_empty_output_is_failure(self):
        def fake(cmd, **kw):
            open(cmd[-1], "wb").write(b"")
            return mock.Mock(returncode=0, stdout=b"")
        ok, _, msg = self._run(fake)[0]
        self.assertFalse(ok)
        self.assertIn("没有产出", msg)

    def test_short_output_is_refused(self):
        """源 2792 秒、只转出 600 秒 —— 这种半成品绝不能留下顶替原文件。"""
        def fake(cmd, **kw):
            open(cmd[-1], "wb").write(b"x" * 32)
            return mock.Mock(returncode=0, stdout=b"")
        ok, out, msg = self._run(fake, dur=(2792.0, 600.0))[0]
        self.assertFalse(ok)
        self.assertIn("不完整", msg)
        self.assertFalse(os.path.exists(self.src + ".mp4"))

    def test_zero_hole_source_is_refused(self):
        """2026-11-23 现场那个文件：尺寸完整、中间 2.4 GB 全是 0。
        转它只会得到「看起来能播、其实只有前 10 分钟」的 mp4，所以必须直接拒。"""
        big = ts_head(188 * 40) + b"\x00" * (188 * 2000) + ts_head(188 * 40)
        with open(self.src, "wb") as f:
            f.write(big)
        ok, out, msg = self._run(
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("坏文件不该起 ffmpeg")))[0]
        self.assertFalse(ok)
        self.assertIn("空洞", msg)
        self.assertIn("重新下载", msg)
        self.assertFalse(os.path.exists(self.src + ".mp4"))


class ZeroHolesTest(unittest.TestCase):
    def test_detects_middle_zero_region(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "x")
        try:
            with open(p, "wb") as f:
                f.write(ts_head(188 * 50) + b"\x00" * (188 * 2000) + ts_head(188 * 50))
            self.assertTrue(m.zero_holes(p))
        finally:
            os.unlink(p)
            os.rmdir(d)

    def test_clean_file_has_no_holes(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "x")
        try:
            with open(p, "wb") as f:
                f.write(ts_head(188 * 3000))
            self.assertEqual(m.zero_holes(p), [])
        finally:
            os.unlink(p)
            os.rmdir(d)


class CandidatesTest(unittest.TestCase):
    def test_finds_extensionless_ts_skips_mp4_and_part(self):
        d = tempfile.mkdtemp()
        sub = os.path.join(d, "a")
        os.makedirs(sub)
        try:
            good = os.path.join(sub, "noext")
            open(good, "wb").write(ts_head(188 * 20))
            named = os.path.join(sub, "show.ts")
            open(named, "wb").write(ts_head(188 * 20))
            already = os.path.join(sub, "done")
            open(already, "wb").write(ts_head(188 * 20))
            open(already + ".mp4", "wb").write(b"x")       # 已有同名 mp4 → 跳过
            open(os.path.join(sub, "x.part"), "wb").write(ts_head(188 * 20))
            open(os.path.join(sub, "real.mp4"), "wb").write(mp4_head())
            open(os.path.join(sub, ".hidden"), "wb").write(ts_head(188 * 20))
            open(os.path.join(sub, "tiny"), "wb").write(b"\x47" * 20)
            got = set(m.remux_candidates(d))
            self.assertEqual(got, {good, named})
        finally:
            for root, dirs, files in os.walk(d, topdown=False):
                for n in files:
                    os.unlink(os.path.join(root, n))
                for n in dirs:
                    os.rmdir(os.path.join(root, n))
            os.rmdir(d)

    def test_depth_is_capped(self):
        d = tempfile.mkdtemp()
        deep = os.path.join(d, "1", "2", "3", "4")
        os.makedirs(deep)
        try:
            f = os.path.join(deep, "noext")
            open(f, "wb").write(ts_head(188 * 20))
            self.assertEqual(m.remux_candidates(d, maxdepth=1), [])
            self.assertEqual(m.remux_candidates(d, maxdepth=5), [f])
        finally:
            for root, dirs, files in os.walk(d, topdown=False):
                for n in files:
                    os.unlink(os.path.join(root, n))
                for n in dirs:
                    os.rmdir(os.path.join(root, n))
            os.rmdir(d)


class QueueTest(unittest.TestCase):
    def setUp(self):
        m.REMUX_DONE.clear()
        while not m.REMUX_Q.empty():
            m.REMUX_Q.get_nowait()

    def test_same_path_enqueued_once(self):
        self.assertTrue(m.enqueue_remux("j1", "/tmp/a"))
        self.assertFalse(m.enqueue_remux("j1", "/tmp/a"))

    def test_empty_path_ignored(self):
        self.assertFalse(m.enqueue_remux("j1", ""))

    def test_schedule_only_for_single_file_jobs(self):
        s = {"autoRemuxMp4": True}
        self.assertFalse(m._schedule_remux_for_job({"id": 1, "kind": "download"}, s))
        with mock.patch.object(m, "enqueue_remux", return_value=True) as en, \
             mock.patch.object(m, "local_dest", return_value="/d"), \
             mock.patch.object(m, "load_settings", return_value=s):
            job = {"id": 1, "kind": "download", "single_file": True,
                   "dst_dir": "/data/x", "safe_leaf": "a.mp4"}
            self.assertTrue(m._schedule_remux_for_job(job, s))
            en.assert_called_once_with(1, os.path.join("/d", "a.mp4"))

    def test_switch_off_schedules_nothing(self):
        with mock.patch.object(m, "enqueue_remux", return_value=True) as en:
            self.assertFalse(m._schedule_remux_for_job(
                {"id": 1, "single_file": True, "dst_dir": "/data/x", "safe_leaf": "a.mp4"},
                {"autoRemuxMp4": False}))
            en.assert_not_called()


class SettingsTest(unittest.TestCase):
    def test_writable_whitelist_covers_new_fields(self):
        for k in ("autoRemuxMp4", "remuxKeepSource", "remuxTimeoutSec"):
            self.assertIn(k, m.SETTINGS_WRITABLE, "%s 漏了会被静默丢弃" % k)

    def test_timeout_is_clamped(self):
        self.assertEqual(m._norm_settings({"remuxTimeoutSec": 5})["remuxTimeoutSec"], 60)
        self.assertEqual(m._norm_settings({"remuxTimeoutSec": 999999})["remuxTimeoutSec"], 21600)
        self.assertEqual(m._norm_settings({"remuxTimeoutSec": 600})["remuxTimeoutSec"], 600)

    def test_keep_source_parsed_as_bool(self):
        self.assertFalse(m._norm_settings({"remuxKeepSource": "0"})["remuxKeepSource"])
        self.assertTrue(m._norm_settings({"remuxKeepSource": "yes"})["remuxKeepSource"])

    def test_defaults_on(self):
        d = m.DEFAULTS
        self.assertTrue(d["autoRemuxMp4"])
        self.assertTrue(d["remuxKeepSource"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
