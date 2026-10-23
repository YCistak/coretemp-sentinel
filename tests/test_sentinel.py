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


if __name__ == "__main__":
    unittest.main()
