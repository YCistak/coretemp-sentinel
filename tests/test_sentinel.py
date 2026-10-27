#!/usr/bin/env python3
"""Aciklama tipleri ve thread gruplama testleri (stdlib unittest).

Calistirma: python3 -m unittest discover -s tests -v
Ag erisimi gerektirmez; yalnizca saf fonksiyonlar test edilir.
"""

import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sentinel  # noqa: E402


def sentence_count(text):
    return len(re.findall(r"[^.!?]+[.!?]", text))


class ExplanationTest(unittest.TestCase):
    def test_every_explanation_is_two_sentences(self):
        for kind, text in sentinel.EXPLANATIONS.items():
            with self.subTest(kind=kind):
                self.assertEqual(sentence_count(text), 2, text)
                self.assertTrue(text.rstrip().endswith("."), text)

    def test_lore_classification(self):
        cases = [
            ("[PATCH] hwmon: (coretemp) X", "body", "lore_patch_new"),
            ("[PATCH v2 2/3] hwmon: (coretemp) X", "", "lore_patch_new"),
            ("[RFC PATCH] hwmon: (coretemp) X", "", "lore_patch_new"),
            ("Re: [PATCH] hwmon: (coretemp) X",
             "Applied. Thanks.", "lore_applied"),
            ("Re: [PATCH] hwmon: (coretemp) X",
             "Reviewed-by: Guenter Roeck <linux@roeck-us.net>", "lore_review_tag"),
            ("Re: [PATCH] hwmon: (coretemp) X",
             "Tested-by: Someone <a@b.c>", "lore_review_tag"),
            ("Re: [PATCH] hwmon: (coretemp) X", "NACK, this is wrong", "lore_changes"),
            ("Re: [PATCH] hwmon: (coretemp) X",
             "Please fix the locking and send v2", "lore_changes"),
            ("Re: [PATCH] hwmon: (coretemp) X",
             "I think the sensor naming is odd.", "lore_reply"),
        ]
        for title, body, expected in cases:
            with self.subTest(title=title, expected=expected):
                self.assertEqual(sentinel.classify_lore(title, body), expected)

    def test_patchwork_classification(self):
        self.assertEqual(sentinel.classify_patchwork("new", True), "patchwork_new")
        for state, expected in [
            ("accepted", "patchwork_accepted"),
            ("rejected", "patchwork_rejected"),
            ("changes-requested", "patchwork_changes"),
            ("superseded", "patchwork_superseded"),
            ("under-review", "patchwork_other"),
            ("handled-elsewhere", "patchwork_other"),
        ]:
            with self.subTest(state=state):
                self.assertEqual(sentinel.classify_patchwork(state, False), expected)

    def test_bugzilla_classification(self):
        for status in ("RESOLVED", "CLOSED", "VERIFIED", "resolved"):
            self.assertEqual(sentinel.classify_bugzilla(status), "bugzilla_resolved")
        for status in ("NEW", "ASSIGNED", "NEEDINFO", ""):
            self.assertEqual(sentinel.classify_bugzilla(status), "bugzilla_open")

    def test_detail_placeholder_is_filled(self):
        text = sentinel.explain("patchwork_other", "under-review")
        self.assertIn("under-review", text)
        self.assertEqual(sentence_count(text), 2)

    def test_unknown_kind_falls_back_to_default(self):
        self.assertEqual(sentinel.explain("boyle_bir_tip_yok"),
                         sentinel.EXPLANATIONS["default"])


class ThreadGroupTest(unittest.TestCase):
    def make_item(self, title, author, updated, summary="", link="https://l/x"):
        return {
            "id": f"{title}-{updated}",
            "title": title,
            "author": author,
            "updated": updated,
            "summary": summary,
            "link": link,
        }

    def test_same_thread_collapses_to_one_message(self):
        items = [
            self.make_item("[PATCH] hwmon: (coretemp) Add docs", "Asaf",
                           "2026-10-20T10:00:00Z", "Add sysfs documentation."),
            self.make_item("Re: [PATCH] hwmon: (coretemp) Add docs", "Guenter",
                           "2026-10-20T12:00:00Z", "Looks good."),
            self.make_item("Re: [PATCH] hwmon: (coretemp) Add docs", "Ximing",
                           "2026-10-20T14:00:00Z", "One nit below."),
        ]
        groups = sentinel.build_lore_groups(items)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["count"], 3)
        self.assertEqual(groups[0]["date"], "2026-10-20T14:00:00Z")
        self.assertEqual(set(groups[0]["authors"]), {"Asaf", "Guenter", "Ximing"})

    def test_single_patch_group_is_patch_new(self):
        items = [self.make_item("[PATCH] hwmon: (coretemp) X", "Asaf",
                                "2026-10-20T10:00:00Z", "body")]
        group = sentinel.build_lore_groups(items)[0]
        self.assertEqual(sentinel.lore_group_kind(group), "lore_patch_new")

    def test_patch_with_replies_is_patch_thread(self):
        items = [
            self.make_item("[PATCH] hwmon: (coretemp) X", "Asaf",
                           "2026-10-20T10:00:00Z", "body"),
            self.make_item("Re: [PATCH] hwmon: (coretemp) X", "Guenter",
                           "2026-10-20T11:00:00Z", "Looks fine to me."),
        ]
        group = sentinel.build_lore_groups(items)[0]
        self.assertEqual(sentinel.lore_group_kind(group), "lore_patch_thread")

    def test_newest_applied_wins(self):
        items = [
            self.make_item("[PATCH] hwmon: (coretemp) X", "Asaf",
                           "2026-10-20T10:00:00Z", "body"),
            self.make_item("Re: [PATCH] hwmon: (coretemp) X", "Guenter",
                           "2026-10-20T11:00:00Z", "Applied. Thanks."),
        ]
        group = sentinel.build_lore_groups(items)[0]
        self.assertEqual(sentinel.lore_group_kind(group), "lore_applied")

    def test_different_subjects_stay_separate(self):
        items = [
            self.make_item("[PATCH] hwmon: (coretemp) X", "Asaf", "2026-10-20T10:00:00Z"),
            self.make_item("[PATCH] hwmon: (coretemp) Y", "Asaf", "2026-10-20T11:00:00Z"),
        ]
        self.assertEqual(len(sentinel.build_lore_groups(items)), 2)

    def test_message_has_explanation_and_fits_telegram(self):
        long_body = "coretemp " * 500
        items = [self.make_item("Re: [PATCH] hwmon: (coretemp) X", "Asaf",
                                "2026-10-20T10:00:00Z", long_body)]
        msg = sentinel.compose_lore_message(sentinel.build_lore_groups(items)[0])
        text = msg["text"]
        self.assertLessEqual(len(text), sentinel.MAX_MESSAGE_LEN)
        # Mesajda aciklama veya yorum bolumu olmali
        self.assertTrue("<b>" in text and "</b>" in text)
        self.assertIn("Asaf", text)

    def test_clamp_message(self):
        text = "x" * (sentinel.MAX_MESSAGE_LEN + 100)
        self.assertEqual(len(sentinel.clamp_message(text)), sentinel.MAX_MESSAGE_LEN)


class FormatTest(unittest.TestCase):
    def test_messages_have_balanced_html(self):
        """Telegram HTML modu dengesiz etikette mesaji reddeder."""
        from html.parser import HTMLParser

        class Balance(HTMLParser):
            def __init__(self):
                super().__init__()
                self.stack = []
                self.errors = []

            def handle_starttag(self, tag, attrs):
                if tag in ("b", "i", "code", "a"):
                    self.stack.append(tag)

            def handle_endtag(self, tag):
                if tag in ("b", "i", "code", "a"):
                    if not self.stack or self.stack.pop() != tag:
                        self.errors.append(tag)

        titles = [
            "[PATCH] hwmon: (coretemp) Add <docs> & \"labels\"",
            "Re: [PATCH v2] hwmon: (coretemp) X",
        ]
        for title in titles:
            items = [{
                "id": "1", "title": title, "author": "A & B",
                "updated": "2026-10-20T10:00:00Z",
                "summary": "coretemp <b>wrong</b> & more " * 20,
                "link": "https://l/x",
            }]
            pair = Balance()
            pair.feed(sentinel.compose_lore_message(
                sentinel.build_lore_groups(items)[0])["text"])
            self.assertEqual(pair.stack, [])
            self.assertEqual(pair.errors, [])

    def test_normalize_subject(self):
        self.assertEqual(
            sentinel.normalize_subject("Re: [PATCH v3 2/4] hwmon: (coretemp) Fix X"),
            "hwmon: (coretemp) fix x")
        self.assertEqual(sentinel.normalize_subject(""), "(konusuz)")

    def test_display_subject_keeps_patch_tag(self):
        self.assertEqual(
            sentinel.display_subject("Re: [PATCH v2] hwmon: (coretemp) X"),
            "[PATCH v2] hwmon: (coretemp) X")



# --------------------------------------------------------------------------
# Kaynak-basi taban cizgisi, gonderim hatasi, hwmon-next, saglik sayaci
# --------------------------------------------------------------------------

from unittest import mock  # noqa: E402


def make_config(**over):
    cfg = {"telegram_token": "T", "chat_id": "C", "dry_run": False}
    cfg.update(over)
    return cfg


def patch_json(i, name="hwmon: (coretemp) X", state="new", date="2026-10-20T00:00:00"):
    return {"id": i, "name": name, "state": state, "date": date,
            "submitter": {"name": "Biri"}, "web_url": f"https://p/{i}/"}


class PatchworkTest(unittest.TestCase):
    def setUp(self):
        self.conn = sentinel.init_db(":memory:")
        self.cfg = make_config(patchwork_queries=["q=coretemp", "q=nct6775"])

    def run_check(self, by_query):
        urls = []

        def fake(url, **kw):
            urls.append(url)
            for q, data in by_query.items():
                if q in url:
                    return data
            return []
        msgs = []
        with mock.patch.object(sentinel, "fetch_json", fake):
            sentinel.check_patchwork(self.conn, self.cfg, False, msgs)
        return urls, msgs

    def test_url_has_order_and_per_page(self):
        urls, _ = self.run_check({})
        self.assertEqual(len(urls), 2)
        for u in urls:
            self.assertIn("order=-date", u)
            self.assertIn("per_page=50", u)
        self.assertTrue(any("q=nct6775" in u for u in urls))

    def test_first_time_query_is_silent_then_loud(self):
        _, msgs = self.run_check({"q=coretemp": [patch_json(1)], "q=nct6775": [patch_json(2)]})
        self.assertEqual(msgs, [])
        self.assertTrue(sentinel.is_seen(self.conn, "1", "patchwork"))
        _, msgs = self.run_check({"q=coretemp": [patch_json(1), patch_json(3)]})
        self.assertEqual([m["id"] for m in msgs], ["3"])

    def test_new_query_baseline_does_not_flood(self):
        self.run_check({"q=coretemp": [patch_json(1)]})
        self.cfg["patchwork_queries"].append("submitter=a@b.c")
        _, msgs = self.run_check({"q=coretemp": [patch_json(1)],
                                  "submitter=": [patch_json(10), patch_json(11)]})
        self.assertEqual(msgs, [])
        self.assertTrue(sentinel.is_seen(self.conn, "10", "patchwork"))

    def test_state_change_still_reported(self):
        self.run_check({"q=coretemp": [patch_json(1)]})
        _, msgs = self.run_check({"q=coretemp": [patch_json(1, state="accepted")]})
        self.assertEqual(len(msgs), 1)
        self.assertIn("accepted", msgs[0]["text"])


class DispatchTest(unittest.TestCase):
    def setUp(self):
        self.conn = sentinel.init_db(":memory:")
        self.cfg = make_config()

    def msg(self, i):
        return {"source": "patchwork", "id": str(i), "date": f"2026-10-{i:02d}",
                "text": "x", "marks": [(str(i), "patchwork", "t", "new")]}

    def test_failed_send_is_not_marked_seen(self):
        with mock.patch.object(sentinel, "send_telegram", return_value=False):
            sent = sentinel.dispatch(self.conn, self.cfg, [self.msg(1)], False)
        self.assertEqual(sent, 0)
        self.assertFalse(sentinel.is_seen(self.conn, "1", "patchwork"))

    def test_ok_send_is_marked_seen(self):
        with mock.patch.object(sentinel, "send_telegram", return_value=True):
            sent = sentinel.dispatch(self.conn, self.cfg, [self.msg(1)], False)
        self.assertEqual(sent, 1)
        self.assertTrue(sentinel.is_seen(self.conn, "1", "patchwork"))

    def test_dropped_over_limit_are_marked(self):
        cfg = make_config(max_messages=1)
        with mock.patch.object(sentinel, "send_telegram", return_value=True):
            sentinel.dispatch(self.conn, cfg, [self.msg(1), self.msg(2)], False)
        self.assertTrue(sentinel.is_seen(self.conn, "1", "patchwork"))
        self.assertTrue(sentinel.is_seen(self.conn, "2", "patchwork"))


def atom(entries):
    body = "".join(
        f"<entry><title>{t}</title><updated>{d}</updated>"
        f"<author><name>{a}</name></author>"
        f"<link rel='alternate' href='http://g/commit/?id={sha}'/>"
        f"<id>urn:sha1:{sha}</id><content>{c}</content></entry>"
        for t, d, a, sha, c in entries)
    return f"<feed xmlns='http://www.w3.org/2005/Atom'>{body}</feed>"


class GitTest(unittest.TestCase):
    TREES = [
        {"tree": "mainline", "label": "mainline", "atom": "http://m/{path}", "api": None},
        {"tree": "hwmon-next", "label": "hwmon-next — test zamanı",
         "atom": "http://n/{path}", "api": None},
    ]

    def setUp(self):
        self.conn = sentinel.init_db(":memory:")
        self.cfg = make_config(git_trees=self.TREES, git_files=["f.c"])
        self.feeds = {}

    def run_check(self):
        def fake(url, **kw):
            return self.feeds.get(url.split("/")[2], atom([]))
        msgs = []
        with mock.patch.object(sentinel, "http_get", fake):
            sentinel.check_git_commits(self.conn, self.cfg, False, msgs)
        return msgs

    def test_hwmon_next_message_and_star(self):
        self.run_check()  # bos besleme -> taban cizgisi
        self.feeds["n"] = atom([("hwmon: X", "2026-10-20T00:00:00Z", "Biri", "aaa",
                                 "Tested-by: Asaf Nuri Bertizlio&#287;lu")])
        msgs = self.run_check()
        self.assertEqual(len(msgs), 1)
        self.assertIn("hwmon-next — test zamanı", msgs[0]["text"])
        self.assertIn("⭐", msgs[0]["text"])

    def test_rebase_does_not_renotify_but_mainline_does(self):
        self.run_check()
        entry = ("hwmon: X", "2026-10-20T00:00:00Z", "Biri", "aaa", "")
        self.feeds["n"] = atom([entry])
        msgs = self.run_check()
        for m in msgs:
            sentinel.apply_marks(self.conn, m)
        # rebase: sha degisti, baslik+yazar ayni
        self.feeds["n"] = atom([("hwmon: X", "2026-10-21T00:00:00Z", "Biri", "bbb", "")])
        self.assertEqual(self.run_check(), [])
        # mainline'a gecis ayri bildirim
        self.feeds["m"] = atom([("hwmon: X", "2026-10-22T00:00:00Z", "Biri", "ccc", "")])
        msgs = self.run_check()
        self.assertEqual(len(msgs), 1)
        self.assertIn("[git · mainline]", msgs[0]["text"])
        self.assertIn("mainline'a geçti", msgs[0]["text"])

    def test_no_star_without_word(self):
        self.run_check()
        self.feeds["m"] = atom([("hwmon: Y", "2026-10-20T00:00:00Z", "Biri", "aaa", "")])
        msgs = self.run_check()
        self.assertNotIn("⭐", msgs[0]["text"])


class HealthTest(unittest.TestCase):
    def setUp(self):
        self.conn = sentinel.init_db(":memory:")
        self.cfg = make_config()
        sentinel._HEALTH.clear()

    def run_health(self, ok):
        sentinel._HEALTH.clear()
        sentinel.note_health("kaynak", ok)
        with mock.patch.object(sentinel, "send_telegram", return_value=True) as m:
            sentinel.update_health(self.conn, self.cfg, "T", "C")
        return m

    def test_alert_once_after_six_and_recovery_once(self):
        for _ in range(5):
            self.assertFalse(self.run_health(False).called)
        self.assertTrue(self.run_health(False).called)       # 6. basarisizlik
        self.assertFalse(self.run_health(False).called)      # tekrar uyarma yok
        m = self.run_health(True)
        self.assertTrue(m.called)
        self.assertIn("tekrar bağlandı", m.call_args[0][2])
        self.assertFalse(self.run_health(True).called)


class MetaTest(unittest.TestCase):
    def test_meta_roundtrip(self):
        conn = sentinel.init_db(":memory:")
        self.assertIsNone(sentinel.meta_get(conn, "k"))
        sentinel.meta_set(conn, "k", 3)
        self.assertEqual(sentinel.meta_get(conn, "k"), "3")
        sentinel.meta_del(conn, "k")
        self.assertIsNone(sentinel.meta_get(conn, "k"))


if __name__ == "__main__":
    unittest.main()
