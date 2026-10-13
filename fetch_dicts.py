# -*- coding: utf-8 -*-
"""Download the Hunspell dictionaries langcheck.py uses from github.com/LibreOffice/dictionaries.

    python fetch_dicts.py              # cs sk pl de en
    python fetch_dicts.py cs pl        # only some

Files go to ./dicts (or LANGTOOLS_DICTS). Each dictionary keeps its own licence
(MPL, LGPL, GPL or similar, see the README files in the LibreOffice repo); they are
downloaded to your machine, not redistributed by this repo.
"""
import os
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DICTS = os.environ.get('LANGTOOLS_DICTS') or os.path.join(HERE, 'dicts')
BASE = 'https://raw.githubusercontent.com/LibreOffice/dictionaries/master/'
SOURCES = {'cs': 'cs_CZ/cs_CZ', 'sk': 'sk_SK/sk_SK', 'pl': 'pl_PL/pl_PL',
           'de': 'de/de_DE_frami', 'en': 'en/en_GB'}


def main(langs):
    os.makedirs(DICTS, exist_ok=True)
    for lang in langs or SOURCES:
        src = SOURCES.get(lang)
        if not src:
            print(f'{lang}: unknown, choose from {", ".join(SOURCES)}')
            continue
        for ext in ('.aff', '.dic'):
            dst = os.path.join(DICTS, os.path.basename(src) + ext)
            urllib.request.urlretrieve(BASE + src + ext, dst)
            print(f'{lang}: {os.path.basename(dst)} ({os.path.getsize(dst) // 1024} KB)')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
