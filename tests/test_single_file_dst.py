#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v4.08：单文件任务的落点必须与界面显示一致。

背景（2026-11-24 现场）：一个单文件任务卡上写「目标 /data/Downloads/#Chrisgag xxx.mp4」，
rclone 报的却是 `open /data/#Chrisgag xxx.mp4.7fefa8fb.partial: operation not permitted`
—— 差一层 Downloads。旧版 `dst_dir` 在未指定 folder 时直接取卷根，于是：
  · 文件真被写进 /data/（卷根），不在 Downloads 里；
  · 卡片显示的是 /data/Downloads/xxx，报错路径和界面路径对不上，排障被带偏；
  · 即便下成功，文件也散在卷根，不在同目录其它文件旁边。

运行：python -m unittest discover -s tests -v
"""
import importlib.util
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("console", os.path.join(ROOT, "download-console.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

VROOT = "/vol3/downloads"


class SingleFileDstTest(unittest.TestCase):

    def test_default_keeps_parent_folder(self):
        """未指定 folder：Downloads/<源文件所在文件夹名>/，与目录任务同层级。"""
        d = m.single_file_dst_dir(VROOT, "", "3/Chrisgag", VROOT + "/Downloads/ignored.mp4")
        self.assertEqual(d, VROOT + "/Downloads/Chrisgag")

    def test_default_without_parent(self):
        """源文件就在远端根目录：落到 Downloads/ 根，不写卷根。"""
        d = m.single_file_dst_dir(VROOT, "", "", VROOT + "/Downloads/ignored.mp4")
        self.assertEqual(d, VROOT + "/Downloads")

    def test_explicit_folder_wins(self):
        """显式指定保存目录时，该目录就是目标目录。"""
        d = m.single_file_dst_dir(VROOT, "MyDir/sub", "3/Chrisgag", VROOT + "/MyDir/sub")
        self.assertEqual(d, VROOT + "/MyDir/sub")

    def test_dest_matches_dst_dir_plus_leaf(self):
        """dest 必须等于 dst_dir + 落盘名 —— 这就是「显示 == 真实」的约束。"""
        leaf = "#Chrisgag 【X 猥琐彪叔】陷入危险的壁虎女 丝袜捆绑自缚、丝袜堵嘴蒙嘴.mp4"
        dst_dir = m.single_file_dst_dir(VROOT, "", "3/Chrisgag", "")
        dest = dst_dir + "/" + leaf
        self.assertTrue(dest.startswith(dst_dir + "/"))
        self.assertEqual(os.path.dirname(dest), dst_dir)
        # 旧版真实写盘位置（卷根 + leaf）不能再出现
        self.assertNotEqual(os.path.dirname(dest), VROOT)

    def test_subdir_comes_from_download_subdir_const(self):
        """目录层级来自 DOWNLOAD_SUBDIR，环境变量覆盖时依然一致。"""
        self.assertIn("/" + m.DOWNLOAD_SUBDIR, m.single_file_dst_dir(VROOT, "", "a/b", ""))


if __name__ == "__main__":
    unittest.main(verbosity=2)
