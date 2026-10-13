"""Download the Hunspell dictionaries langcheck.py uses from github.com/LibreOffice/dictionaries.

    python fetch_dicts.py              # cs sk pl de en
    python fetch_dicts.py cs pl        # only some
    python fetch_dicts.py --ref master # newest instead of the pinned commit

Files go to ./dicts (or LANGTOOLS_DICTS). The source is pinned to one commit of the
LibreOffice repository, so the same copy gives the same result on every machine and
in CI. Each dictionary keeps its own licence (MPL, LGPL, GPL or similar, see the README
in each folder of that repository); they are downloaded to your machine, not
redistributed by this repo.
"""
from __future__ import annotations

import argparse
import os
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REF = '67032ee957631908098a72edf48dc81cac27e369'  # LibreOffice/dictionaries master, 2026-09-21
BASE = 'https://raw.githubusercontent.com/LibreOffice/dictionaries/{ref}/'
SOURCES = {'cs': 'cs_CZ/cs_CZ', 'sk': 'sk_SK/sk_SK', 'pl': 'pl_PL/pl_PL',
           'de': 'de/de_DE_frami', 'en': 'en/en_GB'}


def download(url: str, dst: str) -> None:
    tmp = dst + '.part'
    with urllib.request.urlopen(url, timeout=60) as r, open(tmp, 'wb') as f:
        while chunk := r.read(1 << 16):
            f.write(chunk)
    os.replace(tmp, dst)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description='Download Hunspell dictionaries for langcheck.py.')
    p.add_argument('langs', nargs='*', help=f'any of {", ".join(SOURCES)} (default: all)')
    p.add_argument('--ref', default=REF, help='commit, tag or branch of LibreOffice/dictionaries')
    p.add_argument('--force', action='store_true', help='download again even if the files exist')
    args = p.parse_args(argv)
    unknown = [x for x in args.langs if x not in SOURCES]
    if unknown:
        print(f'unknown language {", ".join(unknown)}; choose from {", ".join(SOURCES)}', file=sys.stderr)
        return 2
    dicts = os.environ.get('LANGTOOLS_DICTS') or os.path.join(HERE, 'dicts')
    os.makedirs(dicts, exist_ok=True)
    failed = 0
    for lang in args.langs or SOURCES:
        src = SOURCES[lang]
        for ext in ('.aff', '.dic'):
            dst = os.path.join(dicts, os.path.basename(src) + ext)
            if os.path.exists(dst) and not args.force:
                print(f'{lang}: {os.path.basename(dst)} already there')
                continue
            try:
                download(BASE.format(ref=args.ref) + src + ext, dst)
            except Exception as e:
                print(f'{lang}: {os.path.basename(dst)} FAILED ({e})', file=sys.stderr)
                failed += 1
                continue
            print(f'{lang}: {os.path.basename(dst)} ({os.path.getsize(dst) // 1024} KB)')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
