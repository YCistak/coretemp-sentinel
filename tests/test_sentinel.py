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
        self.cfg = make_config(patchwork_queries=["q=coretemp", "q=ikinci"])

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
        self.assertTrue(any("q=ikinci" in u for u in urls))

    def test_first_time_query_is_silent_then_loud(self):
        _, msgs = self.run_check({"q=coretemp": [patch_json(1)], "q=ikinci": [patch_json(2)]})
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

    def test_second_submitter_query_is_silent_first_time_and_marks_mine(self):
        self.cfg["patchwork_queries"] = ["q=coretemp", "submitter=ycistak@gmail.com"]
        self.run_check({"q=coretemp": [patch_json(1)], "ycistak": [patch_json(5)]})
        self.cfg["patchwork_queries"].append("submitter=asafbertizlioglu@gmail.com")
        mine = patch_json(20)
        mine["submitter"] = {"name": "Asaf", "email": "asafbertizlioglu@gmail.com"}
        sentinel.MY_PATCHES.clear()
        _, msgs = self.run_check({"asafbertizlioglu": [mine, patch_json(21)]})
        self.assertEqual(msgs, [])  # yeni sorgunun gecmisi spam yapmaz
        self.assertTrue(sentinel.is_seen(self.conn, "20", "patchwork"))
        self.assertEqual({p["id"] for p in sentinel.MY_PATCHES}, {20, 21})
        mine2 = patch_json(22)
        mine2["submitter"] = {"name": "Asaf", "email": "asafbertizlioglu@gmail.com"}
        _, msgs = self.run_check({"asafbertizlioglu": [mine, mine2]})
        self.assertEqual([m["id"] for m in msgs], ["22"])
        self.assertIn("🔴", msgs[0]["text"])

    def test_state_change_still_reported(self):
        self.run_check({"q=coretemp": [patch_json(1)]})
        _, msgs = self.run_check({"q=coretemp": [patch_json(1, state="accepted")]})
        self.assertEqual(len(msgs), 1)
        self.assertIn("kabul edildi", msgs[0]["text"])


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
        {"tree": "hwmon", "label": sentinel.DEFAULTS["git_trees"][2]["label"],
         "atom": "http://h/{path}", "api": None},
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

    def test_hwmon_label_and_baseline_silent(self):
        e = ("hwmon: (coretemp) Fix", "2026-10-31T00:00:00Z", "Biri", "aaa", "")
        self.feeds["h"] = atom([e])
        self.assertEqual(self.run_check(), [])  # ilk gorulme: sessiz taban cizgisi
        self.feeds["h"] = atom([e, ("hwmon: (coretemp) Yeni", "2026-11-01T00:00:00Z", "Biri", "bbb", "")])
        msgs = self.run_check()
        self.assertEqual(len(msgs), 1)  # eski sessiz, yeni bildirildi
        self.assertIn("hwmon (acil düzeltme dalı) — hemen test et, birkaç gün içinde Linus&#x27;a gider", msgs[0]["text"])
        self.assertIn("Yeni", msgs[0]["text"])

    def test_hwmon_to_mainline_transition(self):
        self.run_check()
        self.feeds["h"] = atom([("hwmon: F", "2026-10-31T00:00:00Z", "Biri", "aaa", "")])
        for m in self.run_check():
            sentinel.apply_marks(self.conn, m)
        self.feeds["m"] = atom([("hwmon: F", "2026-11-03T00:00:00Z", "Biri", "ccc", "")])
        msgs = self.run_check()
        self.assertEqual(len(msgs), 1)
        self.assertIn("[git · mainline]", msgs[0]["text"])
        self.assertIn("hwmon'dan mainline'a geçti", msgs[0]["text"])

    def test_hwmon_and_hwmon_next_no_double_notify(self):
        self.run_check()
        e = ("hwmon: Z", "2026-10-31T00:00:00Z", "Biri", "aaa", "")
        # ayni kosuda iki dalda birden
        self.feeds["h"] = atom([e])
        self.feeds["n"] = atom([e])
        msgs = self.run_check()
        self.assertEqual(len(msgs), 1)
        for m in msgs:
            sentinel.apply_marks(self.conn, m)
        # sonraki kosuda yine ikisinde de: yeni bildirim yok
        self.assertEqual(self.run_check(), [])
        # sonradan yalniz hwmon-next'e giren, hwmon'da gorulmus commit
        e2 = ("hwmon: W", "2026-11-01T00:00:00Z", "Biri", "bbb", "")
        self.feeds["h"] = atom([e, e2])
        for m in self.run_check():
            sentinel.apply_marks(self.conn, m)
        self.feeds["n"] = atom([e, e2])
        self.assertEqual(self.run_check(), [])

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


# --------------------------------------------------------------------------
# Düz Türkçe mesaj biçimi: karar rengi, ne oldu, ne yapmalısın
# --------------------------------------------------------------------------

def lore_item(title, author, updated, summary="", link="https://lore/x"):
    return {"id": f"{title}-{updated}-{author}", "title": title, "author": author,
            "updated": updated, "summary": summary, "link": link}


NCT_TITLE = "[PATCH v3 1/2] hwmon: Add driver for the NCT6801D EC sensor window"


def nct_group():
    items = [
        lore_item(NCT_TITLE, "Lenovo Muhendis", "2026-11-16T08:00:00Z",
                  "Cover letter. Compared with coretemp 101.0 C in my tests."),
        lore_item("Re: " + NCT_TITLE, "Guenter Roeck", "2026-11-16T09:00:00Z", "Applied."),
        lore_item("Re: " + NCT_TITLE, "sashiko-bot@kernel.org", "2026-11-16T09:30:00Z",
                  "I found no issues in this patch."),
    ]
    return sentinel.build_lore_groups(items)[0]


class PlainMessageTest(unittest.TestCase):
    def test_emails_constant_has_both_addresses(self):
        self.assertIn("ycistak@gmail.com", sentinel.MY_EMAILS)
        self.assertIn("asafbertizlioglu@gmail.com", sentinel.MY_EMAILS)

    def test_white_when_coretemp_only_in_body(self):
        group = nct_group()
        self.assertEqual(sentinel.lore_relevance(group), "white")
        text = sentinel.compose_lore_message(group)["text"]
        first = text.split("\n")[0]
        self.assertIn("⚪", first)
        self.assertIn("geçerken anılıyor", first)
        self.assertIn("kabul etti (Applied)", text)
        self.assertIn("yapay zekâ incelemesi", text)
        self.assertIn("yamanın 3. düzeltilmiş hali", text)
        self.assertIn("Göz at: coretemp'i etkiliyorsa", text)
        self.assertNotIn("Orijinal (İngilizce)", text)
        self.assertTrue(text.rstrip().endswith("https://lore/x"))

    def test_yellow_when_title_has_coretemp(self):
        items = [lore_item("[PATCH] hwmon: (coretemp) Fix X", "Biri", "2026-11-16T08:00:00Z",
                           "Fixes a thing.")]
        group = sentinel.build_lore_groups(items)[0]
        self.assertEqual(sentinel.lore_relevance(group), "yellow")
        text = sentinel.compose_lore_message(group)["text"]
        self.assertIn("🟡", text.split("\n")[0])
        self.assertIn("Reviewed-by + Tested-by", text)
        self.assertIn("Biri", text)
        self.assertTrue(text.rstrip().endswith("https://lore/x"))

    def test_yellow_when_diff_touches_coretemp_c(self):
        diff = "Subject\n---\ndiff --git a/drivers/hwmon/coretemp.c b/drivers/hwmon/coretemp.c\n+x"
        items = [lore_item("[PATCH] hwmon: tweak", "Biri", "2026-11-16T08:00:00Z", diff)]
        group = sentinel.build_lore_groups(items)[0]
        self.assertEqual(sentinel.lore_relevance(group), "yellow")

    def test_red_for_my_email_and_name(self):
        for who, body in [("Asaf Nuri Bertizlioğlu", "x coretemp"),
                          ("Biri", "Cc: Asaf <ycistak@gmail.com>\ncoretemp"),
                          ("Biri", "Tested-by: A <asafbertizlioglu@gmail.com>")]:
            items = [lore_item("[PATCH] hwmon: foo", who, "2026-11-16T08:00:00Z", body)]
            group = sentinel.build_lore_groups(items)[0]
            with self.subTest(who=who):
                self.assertEqual(sentinel.lore_relevance(group), "red")
                text = sentinel.compose_lore_message(group)["text"]
                self.assertIn("🔴 Senin yaman hakkında — bakman lazım", text)
                self.assertIn("Orijinal (İngilizce)", text)

    def test_changes_requested_todo(self):
        items = [
            lore_item("[PATCH] hwmon: (coretemp) X", "Asaf Nuri Bertizlioğlu",
                      "2026-11-16T08:00:00Z", "body"),
            lore_item("Re: [PATCH] hwmon: (coretemp) X", "Guenter Roeck",
                      "2026-11-16T09:00:00Z", "Please fix the locking and send v2"),
        ]
        text = sentinel.compose_lore_message(sentinel.build_lore_groups(items)[0])["text"]
        self.assertIn("yeni bir sürüm", text)
        self.assertIn("v2 gönder", text)

    def test_changes_requested_next_version_from_title(self):
        def todo_for(first_title, reply_title):
            items = [
                lore_item(first_title, "Asaf Nuri Bertizlioğlu", "2026-11-16T08:00:00Z", "body"),
                lore_item(reply_title, "Guenter Roeck", "2026-11-16T09:00:00Z",
                          "Please fix the locking"),
            ]
            return sentinel.compose_lore_message(sentinel.build_lore_groups(items)[0])["text"]
        text = todo_for("[PATCH v2] hwmon: (coretemp) X", "Re: [PATCH v2] hwmon: (coretemp) X")
        self.assertIn("v3 gönder", text)
        self.assertNotIn("v2 gönder", text)
        text = todo_for("[PATCH v4 1/2] hwmon: (coretemp) X",
                        "Re: [PATCH v4 1/2] hwmon: (coretemp) X")
        self.assertIn("v5 gönder", text)
        text = todo_for("[PATCH] hwmon: (coretemp) X", "Re: [PATCH] hwmon: (coretemp) X")
        self.assertIn("v2 gönder", text)

    def test_next_version_helper(self):
        self.assertEqual(sentinel.next_version(["[PATCH] a"]), 2)
        self.assertEqual(sentinel.next_version(["[PATCH v3] a", "Re: [PATCH v2] a"]), 4)
        self.assertEqual(sentinel.next_version([]), 2)

    def test_english_excerpt_is_short_and_labelled(self):
        long = "word " * 200
        items = [lore_item("[PATCH] hwmon: (coretemp) X", "Biri", "2026-11-16T08:00:00Z", long)]
        text = sentinel.compose_lore_message(sentinel.build_lore_groups(items)[0])["text"]
        part = text.split("Orijinal (İngilizce):\n<i>")[1].split("</i>")[0]
        self.assertLessEqual(len(part), 150 + 20)

    def test_at_most_three_sentences_in_what_happened(self):
        group = nct_group()
        sentences, _ = sentinel.explain_lore_group(group, "white")
        self.assertLessEqual(len(sentences), 3)

    def test_html_escaped_and_clamped(self):
        items = [lore_item("[PATCH] hwmon: (coretemp) <b>x</b> & y", "A & <B>",
                           "2026-11-16T08:00:00Z", "<script> " * 2000)]
        text = sentinel.compose_lore_message(sentinel.build_lore_groups(items)[0])["text"]
        self.assertNotIn("<script>", text)
        self.assertNotIn("<B>", text)
        self.assertLessEqual(len(text), sentinel.MAX_MESSAGE_LEN)

    def test_default_queries_include_both_addresses(self):
        queries = sentinel.DEFAULTS["patchwork_queries"]
        for email in sentinel.MY_EMAILS:
            self.assertIn("submitter=" + email, queries)

    def test_patchwork_relevance_second_address(self):
        mine = {"name": "hwmon: foo",
                "submitter": {"name": "X", "email": "asafbertizlioglu@gmail.com"}}
        self.assertEqual(sentinel.patchwork_relevance(mine), "red")

    def test_patchwork_relevance(self):
        mine = {"name": "hwmon: foo", "submitter": {"name": "Asaf", "email": "ycistak@gmail.com"}}
        other = {"name": "hwmon: (coretemp) foo", "submitter": {"name": "X", "email": "x@y.z"}}
        away = {"name": "hwmon: nct6775", "submitter": {"name": "X", "email": "x@y.z"}}
        self.assertEqual(sentinel.patchwork_relevance(mine), "red")
        self.assertEqual(sentinel.patchwork_relevance(other), "yellow")
        self.assertEqual(sentinel.patchwork_relevance(away), "white")

    def test_git_message_is_yellow_with_todo(self):
        conn = sentinel.init_db(":memory:")
        cfg = make_config(git_trees=GitTest.TREES, git_files=["f.c"])
        feeds = {}

        def fake(url, **kw):
            return feeds.get(url.split("/")[2], atom([]))
        with mock.patch.object(sentinel, "http_get", fake):
            sentinel.check_git_commits(conn, cfg, False, [])
            feeds["m"] = atom([("hwmon: (coretemp) Y", "2026-11-01T00:00:00Z", "Biri", "aaa", "")])
            msgs = []
            sentinel.check_git_commits(conn, cfg, False, msgs)
        text = msgs[0]["text"]
        self.assertIn("🟡", text.split("\n")[0])
        self.assertIn("Linus'un ana çekirdeğine (mainline)", text)
        self.assertIn("Ne yapmalısın?", text)
        self.assertIn("Bilgi: birleştirildi.", text)
        self.assertNotIn("Reviewed-by", text)


class ReviewTodoTest(unittest.TestCase):
    def test_patchwork_todo_by_level(self):
        self.assertEqual(sentinel._patchwork_todo("yellow", "x"), sentinel.TODO_REVIEW)
        self.assertEqual(sentinel._patchwork_todo("white", "x"), sentinel.TODO_GLANCE)
        self.assertEqual(sentinel._patchwork_todo("red", "x"), "x")

    def test_lore_v1_version_shown(self):
        items = [lore_item("[PATCH v1] hwmon: (coretemp) X", "Biri", "2026-11-16T08:00:00Z", "b")]
        text = sentinel.compose_lore_message(sentinel.build_lore_groups(items)[0])["text"]
        self.assertIn("(v1)", text)


def atom_entry(title, author, updated, link, uid, summary="x"):
    return (f"<entry><author><name>{author}</name></author><title>{title}</title>"
            f"<link href=\"{link}\"/><id>{uid}</id><updated>{updated}</updated>"
            f"<content type=\"text\">{summary}</content></entry>")


def atom_feed(*entries):
    return ('<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">'
            + "".join(entries) + "</feed>")


class StableLoreTest(unittest.TestCase):
    def setUp(self):
        self.conn = sentinel.init_db(":memory:")
        self.cfg = {}
        now = sentinel.datetime.now(sentinel.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.now = now
        self.bk = atom_entry("[PATCH 6.6 012/123] hwmon: (coretemp) Fix X", "Greg KH", now,
                             "https://lore.kernel.org/stable/m1@x/", "urn:uuid:s1")
        self.bk2 = atom_entry("[PATCH 6.1.y] hwmon: (coretemp) Fix X", "Ximing Zhang", now,
                              "https://lore.kernel.org/stable/m2@x/", "urn:uuid:s2")
        self.cover = atom_entry("[PATCH 6.6 000/123] 6.6.99-rc1 review coretemp", "Greg KH",
                                now, "https://lore.kernel.org/stable/m3@x/", "urn:uuid:s3")
        self.failed = atom_entry(
            'FAILED: patch "[PATCH] hwmon: (coretemp) Fix X" failed to apply to 5.10-stable tree',
            "Sasha Levin", now, "https://lore.kernel.org/stable/m4@x/", "urn:uuid:s4")
        self.queued = atom_entry("Patch \"hwmon: (coretemp) Fix X\" has been added to the 6.6 tree",
                                 "Sasha Levin", now, "https://lore.kernel.org/stable/m5@x/",
                                 "urn:uuid:s5")

    def run_check(self, hwmon, stable):
        def fake(url, **kw):
            return stable if "/stable/" in url else hwmon
        msgs = []
        with mock.patch.object(sentinel, "http_get", fake):
            sentinel.check_lore(self.conn, self.cfg, False, msgs)
        return msgs

    def test_first_run_stable_is_silent_then_loud(self):
        feed = atom_feed(self.bk, self.cover, self.failed)
        self.assertEqual(self.run_check(atom_feed(), feed), [])
        self.assertTrue(sentinel.is_seen(self.conn, "urn:uuid:s1", "lore"))
        feed2 = atom_feed(self.bk, self.bk2, self.cover, self.failed, self.queued)
        msgs = self.run_check(atom_feed(), feed2)
        self.assertEqual(len(msgs), 1)
        self.assertIn("6.1.y", msgs[0]["text"])

    def test_backport_message_text_and_noise_filtered(self):
        self.run_check(atom_feed(), atom_feed())
        msgs = self.run_check(atom_feed(), atom_feed(self.bk, self.bk2, self.cover,
                                                     self.failed, self.queued))
        self.assertEqual(len(msgs), 2)  # backport grubu + FAILED; kapak ve queue yok
        texts = "\n".join(m["text"] for m in msgs)
        self.assertIn("🟡 Eski sürüme taşınan coretemp yaması (6.1.y, 6.6)", texts)
        self.assertIn("Reviewed-by", texts)
        self.assertIn("Eski sürüme uygulanamayan coretemp yaması (5.10)", texts)
        self.assertIn("elle taşıma fırsatı", texts)
        self.assertNotIn("review coretemp", texts)

    def test_same_message_id_in_both_lists_notifies_once(self):
        self.run_check(atom_feed(), atom_feed())
        hw = atom_entry("[PATCH 6.6 012/123] hwmon: (coretemp) Fix X", "Greg KH", self.now,
                        "https://lore.kernel.org/linux-hwmon/m1@x/", "urn:uuid:h1",
                        "coretemp")
        # hwmon listesinde de gorulen mesaj stable'da farkli uuid ile gelir
        self.run_check(atom_feed(), atom_feed())
        msgs = self.run_check(atom_feed(hw), atom_feed(self.bk))
        self.assertEqual(sum(1 for m in msgs if "stable" in m["id"] or "coretemp" in m["id"]), 1)
        self.assertEqual(len(msgs), 1)

    def test_cover_and_noise_helpers(self):
        self.assertTrue(sentinel.is_stable_noise("[PATCH 6.6 000/123] 6.6.1-rc1 review"))
        self.assertFalse(sentinel.is_stable_noise("[PATCH 6.6 012/123] hwmon: (coretemp) X"))
        self.assertEqual(sentinel.message_id_from_link("https://lore.kernel.org/stable/a%40b/"),
                         "a@b")
        self.assertEqual(sentinel.stable_kind('FAILED: patch "[PATCH] x" failed to apply'),
                         "stable_failed")
