#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v4.07：存储卷可写性实探。

背景（2026-11-24 现场）：NAS 换盘后，宿主上 `/vol2/Data-storage` 变成 55 GB 系统盘里的
一个残留目录（不是任何真实卷），而两个容器的 bind 还指着它。它最坑的地方是「半可写」：
`touch` / `open(O_CREAT)` 能建出 0 字节文件，`write()` 才回 EPERM，`cat` 也读不出来。
旧版把它认成主卷，新任务 100% 落进去、每秒重试、界面永远 0.0%。

因此：可写性必须真写一次数据来判定，而且不可写的卷绝不能当主卷、不能接单。

运行：python -m unittest discover -s tests -v
"""
import importlib.util
import os
import shutil
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("console", os.path.join(ROOT, "download-console.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

EPERM_MSG = ("rc job/status: open /data/Downloads/#Chrisgag 【X 兽娘秘藏】.mp4.7fefa8fb.partial: "
             "operation not permitted")


class VolumeWritableTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cs-wtest-")
        m._WRITABLE_CACHE.clear()

    def tearDown(self):
        m._WRITABLE_CACHE.clear()
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_writable_dir_passes(self):
        self.assertTrue(m.volume_writable(self.dir))
        self.assertEqual(os.listdir(self.dir), [], "探针文件必须自己删干净，不能留在卷根")

    def test_missing_dir_fails(self):
        self.assertFalse(m.volume_writable(os.path.join(self.dir, "nope")))
        self.assertFalse(m.volume_writable(""))

    def test_eperm_on_write_is_not_writable(self):
        """现场那个假卷就是这条路：能建文件、写数据 EPERM。"""
        with mock.patch("os.open", side_effect=OSError(1, "Operation not permitted")):
            self.assertFalse(m.volume_writable(self.dir))

    def test_disk_full_on_write_is_not_writable(self):
        with mock.patch("os.open", side_effect=OSError(28, "No space left on device")):
            self.assertFalse(m.volume_writable(self.dir))

    def test_result_is_cached_within_ttl(self):
        self.assertTrue(m.volume_writable(self.dir))
        # 目录都删掉了还返回 True，说明第二次走的是缓存而不是实探
        shutil.rmtree(self.dir, ignore_errors=True)
        self.assertTrue(m.volume_writable(self.dir))
        # ttl=0 强制重探，此时目录已不存在 → False
        self.assertFalse(m.volume_writable(self.dir, ttl=0))

    def test_probe_writes_real_bytes(self):
        """只建文件不写数据的实现会漏判假卷，这里确认探针真的写了字节。"""
        seen = {}
        real_open = os.open

        def spy(path, flags, mode=0o777):
            fd = real_open(path, flags, mode)
            if ".cloudshuttle-wtest." in str(path):
                seen["path"] = path
                seen["fd"] = fd
            return fd

        with mock.patch("os.open", side_effect=spy):
            self.assertTrue(m.volume_writable(self.dir))
        self.assertIn("path", seen, "必须真的 open 一个探针文件")


class RankPairsTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cs-rank-")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_unwritable_volume_never_first(self):
        """主卷 = 列表第一个；不可写的卷排第一就等于把新任务全送进死路。
        v4.09 起 rank_pairs 每轮都重探可写性，所以这里用 mock 指定探针结果。"""
        stale = {"rclone": "/data", "console": "/data"}
        real = {"rclone": "/vol3/downloads", "console": "/vol3/downloads"}
        with mock.patch.object(m, "volume_writable",
                               side_effect=lambda p, ttl=None: "/vol3" in p):
            out = m.rank_pairs([stale, real])
        self.assertEqual(out[0]["rclone"], "/vol3/downloads")
        self.assertFalse(out[1]["writable"])

    def test_capacity_beats_path_among_writable(self):
        """同为可写时按总容量排，不再让 /data 这个路径有特权。"""
        small = {"rclone": "/vol1/downloads", "console": self.dir}
        big = {"rclone": "/vol3/downloads", "console": self.dir}
        with mock.patch.object(m, "volume_writable", return_value=True), \
             mock.patch.object(m, "_volume_capacity",
                               side_effect=lambda p: 927 * 1024 ** 3 if "/vol3" in p["rclone"] else 56 * 1024 ** 3):
            self.assertEqual(m.rank_pairs([small, big])[0]["rclone"], "/vol3/downloads")

    def test_rank_probes_when_flag_missing(self):
        out = m.rank_pairs([{"rclone": "/data", "console": self.dir}])
        self.assertIn("writable", out[0])
        self.assertTrue(out[0]["writable"])


class StorageStateTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cs-stor-")
        self._old = m.STATE.get("volumePairs")
        m._WRITABLE_CACHE.clear()
        m.STATE["volumePairs"] = [{"rclone": "/data", "console": self.dir, "host": "/x"}]

    def tearDown(self):
        m.STATE["volumePairs"] = self._old
        m._WRITABLE_CACHE.clear()
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_storages_carry_writable_flag(self):
        st = m.refresh_storages()
        self.assertEqual(len(st), 1)
        self.assertTrue(st[0]["writable"])
        self.assertIn("totalGB", st[0])

    def test_unwritable_flag_reaches_state(self):
        with mock.patch("os.open", side_effect=OSError(1, "Operation not permitted")):
            m._WRITABLE_CACHE.clear()
            st = m.refresh_storages()
        self.assertFalse(st[0]["writable"])
        self.assertFalse(m.STATE["volumePairs"][0]["writable"])


class EpermClassifyTest(unittest.TestCase):
    def test_eperm_is_disk_fatal_not_unknown(self):
        kind, hint = m.classify_error(EPERM_MSG)
        self.assertEqual(kind, "DISK_FATAL", "EPERM 归到 UNKNOWN 就会无限重试、界面卡 0.0%")
        self.assertIn("写入权限", hint)

    def test_eperm_does_not_leak_into_transient(self):
        for msg in ("open /data/a.mp4.partial: operation not permitted",
                    "mkdir /data/x: operation not permitted"):
            self.assertEqual(m.classify_error(msg)[0], "DISK_FATAL", msg)


if __name__ == "__main__":
    unittest.main()
