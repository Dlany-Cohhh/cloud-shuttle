#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v3.84：网络抖动 vs 凭证失效的错误分类顺序。

背景：rclone 取 captcha token 的请求被线路 RST 时，报错原文含 "captcha" 字样，
曾经被判定成「需重新登录」，用户重新登录后依旧失败。

运行：python -m unittest discover -s tests -v
"""
import importlib.util
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("console", os.path.join(ROOT, "download-console.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

RESET_ERR = ('预检源目录失败: error in ListJSON: couldn\'t list files: couldn\'t get '
             'captcha token: failed to retrieve captcha token from api: Post '
             '"https://user.mypikpak.com/v1/shield/captcha/init": read tcp '
             '192.168.10.108:46226->43.160.170.231:443: read: connection reset by peer')
TIMEOUT_ERR = "i/o timeout"
EOF_ERR = "tls: failed to verify certificate: Unexpected EOF"
REAL_CAPTCHA = ("PikPak 要求人工验证码（code=4002 captcha_invalid）。账号密码无法在服务器端全自动登录，"
                "请用「⚡ 全自动获取」从本机浏览器提取")
REAL_INVALID = "invalid_grant: refresh_token 无效"
REAL_ROTATED = "这个 refresh_token 已被别处刷新过（refreshed by other）"
REAL_UNAUTH = ('预检源目录失败: error in ListJSON: couldn\'t list files: '
               'Error "unauthenticated" (16): Unauthenticated, please re-login')


class NetVsCredTest(unittest.TestCase):
    def test_is_net_error_recognizes_transport_failures(self):
        for err in (RESET_ERR, TIMEOUT_ERR, EOF_ERR, "dial tcp 1.2.3.4:443: connect: connection refused"):
            self.assertTrue(m.is_net_error(err), err)

    def test_is_net_error_ignores_business_errors(self):
        for err in (REAL_CAPTCHA, REAL_INVALID, REAL_ROTATED, "daily_download_size_exceeded"):
            self.assertFalse(m.is_net_error(err), err)

    def test_need_relink_false_for_network_error(self):
        # 这条曾经被判定为「需重新登录」，是本次修复的核心回归点
        self.assertFalse(m.need_relink(RESET_ERR))
        self.assertFalse(m.need_relink(TIMEOUT_ERR))

    def test_need_relink_true_for_real_credential_errors(self):
        self.assertTrue(m.need_relink(REAL_CAPTCHA))
        self.assertTrue(m.need_relink(REAL_INVALID))
        self.assertTrue(m.need_relink(REAL_ROTATED))

    def test_classify_error_orders_network_before_account(self):
        kind, hint = m.classify_error(RESET_ERR)
        self.assertEqual(kind, "TRANSIENT")
        self.assertIn("网络", hint)

    def test_classify_error_still_account_for_real_captcha(self):
        kind, _ = m.classify_error(REAL_CAPTCHA)
        self.assertEqual(kind, "ACCOUNT_FATAL")

    def test_fatal_job_error_skips_network(self):
        self.assertIsNone(m._fatal_job_error(RESET_ERR))
        # 真凭证错误仍然是终态
        self.assertIsNotNone(m._fatal_job_error(REAL_INVALID))

    def test_file_fatal_still_wins_over_network(self):
        kind, hint = m.classify_error("File name too long: 很长的名字.mp4 /data/x")
        self.assertEqual(kind, "FILE_FATAL")

    def test_trim_err_keeps_tail_reason(self):
        # v3.85 回归点：rclone 前缀本身就吃掉 150 字符，压尾必须留下 reset 的原因，
        # 否则界面只看得到「couldn't get captcha token」，用户又会去重新登录。
        s = m.trim_err(RESET_ERR, 220)
        self.assertLessEqual(len(s), 221)
        self.assertIn("connection reset by peer", s)
        self.assertTrue(m.is_net_error(s))

    def test_precheck_message_stays_transient_after_trim(self):
        msg = "预检源目录失败: %s" % m.trim_err(RESET_ERR, 220)
        self.assertFalse(m.need_relink(msg))
        self.assertEqual(m.classify_error(msg)[0], "TRANSIENT")

    def test_chinese_network_phrase_is_net_error(self):
        # 后端自己翻译的中文文案不含英文指纹，必须靠中文关键词兜住
        self.assertTrue(m.is_net_error("网络连接被重置/超时（已自动重试 2 次，这不是凭证问题）"))

    def test_trimmed_150_prefix_is_the_old_bug(self):
        # 旧实现 [:150] 的结果里已经没有 reset 指纹 —— 复现旧 bug，说明压尾的必要性
        old = RESET_ERR[:150]
        self.assertNotIn("connection reset", old)
        self.assertFalse(m.is_net_error(old))

    def test_unauthenticated_is_relink_and_account_fatal(self):
        # v3.86：PikPak 作废 token 时回 unauthenticated / please re-login，
        # 以前不在 RELINK 表里，界面只剩英文原文，用户不知道去点哪里
        self.assertTrue(m.need_relink(REAL_UNAUTH))
        self.assertFalse(m.is_net_error(REAL_UNAUTH))
        kind, hint = m.classify_error(REAL_UNAUTH)
        self.assertEqual(kind, "ACCOUNT_FATAL")
        self.assertIn("重新登录", hint)


class SourceNoLinkTest(unittest.TestCase):
    """v4.06：源端「只有壳没有内容」必须立刻判死，不能当网络抖动重试。

    现场（2026-11-24）：PikPak 上一个 1.71 GB 的 mp4，列表 size 正常、audit 也 Normal，
    但 hash 以 TEMP 结尾、medias / web_content_link 全空。rclone 原文：
        failed to open source object: can't download: no link
    一个字节都传不出来。旧分类里这条落到 UNKNOWN，配合看门狗就成了无限重启。
    """

    REAL = ("rc operations/copyfile: failed to open source object: "
            "can't download: no link")

    def test_no_link_is_file_fatal_not_transient(self):
        kind, hint = m.classify_error(self.REAL)
        self.assertEqual(kind, "FILE_FATAL")
        self.assertIn("直链", hint)
        self.assertNotIn("可重试", hint)

    def test_variants(self):
        for msg in ("can't download: no link",
                    "failed to open source object: no available link",
                    "cannot download: no link"):
            self.assertEqual(m.classify_error(msg)[0], "FILE_FATAL", msg)

    def test_not_confused_with_network_reset(self):
        # 带 reset 但核心是 no link 的，仍应判文件级（否则会一直重试）
        mixed = "failed to open source object: can't download: no link (read: connection reset)"
        self.assertEqual(m.classify_error(mixed)[0], "FILE_FATAL")

    def test_plain_network_error_unchanged(self):
        self.assertEqual(m.classify_error(RESET_ERR)[0], "TRANSIENT")


class ProbeSourceDeadTest(unittest.TestCase):
    """v4.06：零字节久候时主动探源，把「悬着」变成「确定失败」。"""

    def test_builds_cloud_path_from_subpath(self):
        from unittest import mock
        with mock.patch.object(m, "pk_play_info", return_value={}) as p:
            self.assertEqual(m._probe_source_dead(
                {"remote": "会员号", "subpath": "Pack From Shared/Chrisgag"}, "32.mp4"), "")
        p.assert_called_once_with("会员号", "Pack From Shared/Chrisgag/32.mp4")

    def test_single_file_job_uses_bare_name(self):
        from unittest import mock
        with mock.patch.object(m, "pk_play_info", return_value={}) as p:
            m._probe_source_dead({"remote": "会员号", "single_file": True}, "a.mp4")
        p.assert_called_once_with("会员号", "a.mp4")

    def test_no_link_means_dead(self):
        from unittest import mock
        msg = "这个文件暂时没有可播放的直链（可能仍在转码、已损坏或不在可播状态）"
        with mock.patch.object(m, "pk_play_info", return_value={"error": msg}):
            self.assertIn("直链", m._probe_source_dead({"remote": "R", "subpath": "a"}, "b.mp4"))

    def test_other_errors_are_ignored(self):
        from unittest import mock
        for err in ("读取播放地址失败：502 Bad Gateway", "取不到文件 id，可能路径写错了"):
            with mock.patch.object(m, "pk_play_info", return_value={"error": err}):
                self.assertEqual(m._probe_source_dead({"remote": "R", "subpath": "a"}, "b.mp4"), "")

    def test_exception_never_breaks_download(self):
        from unittest import mock
        with mock.patch.object(m, "pk_play_info", side_effect=RuntimeError("boom")):
            self.assertEqual(m._probe_source_dead({"remote": "R", "subpath": "a"}, "b.mp4"), "")


if __name__ == "__main__":
    unittest.main()
