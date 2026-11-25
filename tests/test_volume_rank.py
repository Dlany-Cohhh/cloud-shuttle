#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v4.09：存储卷的排序规则 —— 大盘优先，不可写的永远垫底。

背景（2026-11-24）：把 1 号盘（56 GB 系统副盘，剩 49.5 GB）挂进云梭后，
排序若只按路径字符串，`/vol1/downloads` 会排在 `/vol3/downloads` 前面，
于是「新建任务不指定卷 → 取第一个」就成了默认写进 50 GB 的小盘 ——
几十个任务就能把系统副盘写满。

规则：可写优先 → 总容量大的优先 → 路径（稳定兜底）。
用总容量而不是剩余空间：容量不变，界面上的「存储空间 N」编号才不会跳。

运行：python -m unittest discover -s tests -v
"""
import importlib.util
import os
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("console", os.path.join(ROOT, "download-console.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

GB = 1024 ** 3
CAPS = {
    "/vol3/downloads": 927 * GB,
    "/vol1/downloads": 56 * GB,
    "/data": 55 * GB,
}
WRITABLE = {"/data": False}          # 其余路径都当作可写


def pair(rclone, writable=True):
    return {"rclone": rclone, "console": rclone, "host": "/host" + rclone, "writable": writable}


class RankPairsTest(unittest.TestCase):

    def _rank(self, pairs):
        with mock.patch.object(m, "_volume_capacity",
                               side_effect=lambda p: CAPS.get(p.get("console"), 0)), \
             mock.patch.object(m, "volume_writable",
                               side_effect=lambda path, ttl=None: WRITABLE.get(path, True)):
            return [p["rclone"] for p in m.rank_pairs(pairs)]

    def test_big_disk_first_among_writable(self):
        """两个都可写时，927 GB 的盘必须排在 56 GB 前面（哪怕路径靠后）。"""
        got = self._rank([pair("/vol1/downloads"), pair("/vol3/downloads")])
        self.assertEqual(got, ["/vol3/downloads", "/vol1/downloads"])

    def test_unwritable_never_primary(self):
        """不可写的卷即使容量最大也不能当主卷。"""
        got = self._rank([pair("/data", writable=False), pair("/vol1/downloads")])
        self.assertEqual(got, ["/vol1/downloads", "/data"])

    def test_unwritable_last_among_three(self):
        got = self._rank([pair("/data", writable=False),
                          pair("/vol1/downloads"),
                          pair("/vol3/downloads")])
        self.assertEqual(got, ["/vol3/downloads", "/vol1/downloads", "/data"])

    def test_same_capacity_stable_by_path(self):
        with mock.patch.object(m, "_volume_capacity", return_value=100 * GB), \
             mock.patch.object(m, "volume_writable", return_value=True):
            got = [p["rclone"] for p in m.rank_pairs([pair("/b"), pair("/a")])]
        self.assertEqual(got, ["/a", "/b"])

    def test_writable_reprobed_every_round(self):
        """卷修好后不用重启控制台：每轮重排都重新探一次可写性（v4.07 只探一次）。"""
        flags = {"/vol1/downloads": False, "/vol3/downloads": True}
        pairs = [pair("/vol3/downloads", writable=True),
                 pair("/vol1/downloads", writable=False)]
        with mock.patch.object(m, "_volume_capacity", return_value=100 * GB), \
             mock.patch.object(m, "volume_writable",
                               side_effect=lambda path, ttl=None: flags[path]):
            m.rank_pairs(pairs)
            self.assertFalse(pairs[1]["writable"])   # 第一轮：探到不可写
            flags["/vol1/downloads"] = True          # 卷被修好
            m.rank_pairs(pairs)
            self.assertTrue(pairs[1]["writable"])    # 第二轮：自动重新判定

    def test_capacity_unknown_sinks(self):
        """拿不到容量的卷（挂载异常）排在有容量的可写卷后面。"""
        got = self._rank([pair("/mystery"), pair("/vol1/downloads")])
        self.assertEqual(got[0], "/vol1/downloads")


if __name__ == "__main__":
    unittest.main(verbosity=2)
