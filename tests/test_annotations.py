"""GitHub Actions annotations (--github-annotations) and the Action's defaults."""
from __future__ import annotations

import os
import re
import unittest

from tests.test_langcheck import ROOT, TempFiles, fake_urlopen, langcheck, run


class AnnotationTest(TempFiles):
    def test_a_finding_in_a_file_becomes_an_error_on_its_line_and_column(self):
        path = self.write('copy.txt', 'Invite a friend\n\nYour mony in one place\n')
        code, out, _ = run(['--lang', 'en', '--github-annotations', path], opener=fake_urlopen())
        self.assertEqual(code, 1)
        errors = [x for x in out.splitlines() if x.startswith('::error ')]
        self.assertEqual(len(errors), 1, out)
        props, msg = errors[0][len('::error '):].split('::', 1)
        self.assertIn('line=3', props)
        self.assertIn('col=6', props)
        self.assertIn('title=copy-langcheck hunspell', props)
        file_prop = re.search(r'file=([^,]+)', props).group(1)
        self.assertEqual(file_prop.replace('%3A', ':').replace('%2C', ','), path)
        self.assertTrue(msg.startswith('mony -> '), msg)

    def test_copy_from_text_has_no_file_property(self):
        code, out, _ = run(['--lang', 'en', '--github-annotations', '--text', 'Your mony'], opener=fake_urlopen())
        errors = [x for x in out.splitlines() if x.startswith('::error ')]
        self.assertEqual(len(errors), 1, out)
        self.assertNotIn('file=', errors[0])

    def test_an_incomplete_check_is_an_error_too(self):
        code, out, _ = run(['--lang', 'en', '--github-annotations', '--text', 'Your money'])   # LT refused
        self.assertEqual(code, 3)
        self.assertIn('::error title=copy-langcheck::NOT CHECKED', out)

    def test_off_by_default(self):
        _, out, _ = run(['--lang', 'en', '--text', 'Your mony'], opener=fake_urlopen())
        self.assertNotIn('::error', out)

    def test_escaping(self):
        self.assertEqual(langcheck._gh_escape('a%b\nc'), 'a%25b%0Ac')
        self.assertEqual(langcheck._gh_escape('C:\\x,y', prop=True), 'C%3A\\x%2Cy')


class ActionDefaultsTest(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(ROOT, 'action.yml'), encoding='utf-8') as f:
            self.action = f.read()

    def test_action_annotates_and_skips_suggestions_by_default(self):
        check = self.action.split('id: check', 1)[1]
        self.assertIn('--github-annotations', check)
        self.assertIn('--no-suggest', check)
        suggest = self.action.split('\n  suggest:', 1)[1].split('\noutputs:', 1)[0]
        self.assertIn("default: 'false'", suggest)


if __name__ == '__main__':
    unittest.main()
