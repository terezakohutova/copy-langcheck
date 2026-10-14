# copy-langcheck

[![CI](https://github.com/terezakohutova/copy-langcheck/actions/workflows/tests.yml/badge.svg)](https://github.com/terezakohutova/copy-langcheck/actions/workflows/tests.yml)

A spelling and grammar gate for marketing copy in **Czech, Slovak, Polish, German and
English**: ad texts, landing pages, newsletters, app strings. Grammar for Slovak,
Polish, German and English comes from LanguageTool. Czech is spelling only (Hunspell,
plus Korektor with `--online`), because LanguageTool has no Czech module. One Python
file, read-only, standard library plus one pure-Python dependency. Exit code 1 when
it finds something, 3 when it could not check everything, so it can sit in front of
an upload, a build or a pull request.

Most copy tools are built for English, and for Czech and Slovak there is very little
you can run locally and script. This combines three open tools that are good at
different things:

| Layer | Catches | Languages | Where it runs |
|---|---|---|---|
| **Hunspell** ([spylls](https://github.com/zverok/spylls) + [LibreOffice dictionaries](https://github.com/LibreOffice/dictionaries)) | unknown words, missing diacritics, typos | cs, sk, pl, de-DE, de-AT, de-CH, en-GB, en-US | locally, always |
| **[LanguageTool](https://github.com/languagetool-org/languagetool)** | grammar, agreement, cases, repeated words, commas | the same minus Czech | a server you run |
| **[Korektor](https://ufal.mff.cuni.cz/korektor)** (UFAL, Charles University) | Czech spelling and diacritics in context | cs | public LINDAT API, **off unless `--online`** (see [Korektor terms](#korektor-terms)) |

## What each layer catches in the examples

Planted mistakes in [`examples/`](examples/), and which layer reported each one. The
unedited runs are in [`examples/output/`](examples/output/).

| Example | Mistake | Hunspell | LanguageTool | Korektor |
|---|---|---|---|---|
| Czech ad (JSON) | `kamarada`, `ziskej`, `odmenu` (missing diacritics) | yes | no Czech module | yes (`--online`) |
| Polish landing page (HTML) | `nagrode`, `Sprawdz` (missing diacritics) | yes | yes | |
| German grammar text | `ein gute Zeichen`, `Mit die` (agreement, case) | no, every word is a real word | yes | |
| German newsletter (Markdown) | `Weihnachen` (typo) | yes | no | |
| German newsletter (Markdown) | `ein gute Zeichen`, `Mit die` | no | yes | |
| Slovak text, no mistakes | | clean | clean | |

Neither layer alone would have caught everything: grammar needs LanguageTool, and the
`Weihnachen` typo was only caught by Hunspell.

## Try it in a minute

```
python -m pip install -r requirements.txt
python fetch_dicts.py cs
python langcheck.py --no-languagetool examples/ad-copy.json
```

```
examples/ad-copy.json:3:21: [hunspell] "kamarada" -> kamaráda, Kamrada, kamarádka  (unknown word)  in: Pozvi kamarada a ziskej odmenu
examples/ad-copy.json:3:32: [hunspell] "ziskej" -> získej, ziskem  (unknown word)  in: Pozvi kamarada a ziskej odmenu
examples/ad-copy.json:3:39: [hunspell] "odmenu" -> odměnu, podmenu, odmetu  (unknown word)  in: Pozvi kamarada a ziskej odmenu
4 texts checked in cs-CZ by hunspell
3 findings
```

Exit code 1. Each finding starts with `file:line:column`, so editors and CI logs can
jump to it. With a LanguageTool server (`LT_URL`), grammar is checked too:

```
$ python langcheck.py --lang de examples/grammar-de.txt
examples/grammar-de.txt:1:9: [languagetool DE_AGREEMENT] "ein gute Zeichen" -> ein gutes Zeichen  (Möglicherweise passen das Nomen und die Wörter, ...)
examples/grammar-de.txt:3:5: [languagetool PRAEP_DAT] "die" -> der  (Die Präposition „Mit“ erwartet den Dativ. ...)
2 texts checked in de-DE by hunspell, languagetool
2 findings
```

If LanguageTool is not reachable, the same command ends with `NOT CHECKED` and exit
3, never "clean".

## Setup

Python 3.10 or newer, on Linux, macOS or Windows.

```
python -m pip install -r requirements.txt   # spylls, pure Python, pinned with hashes
python fetch_dicts.py                       # all languages into ./dicts (pinned LibreOffice commit)
```

For grammar, run a LanguageTool server and point `LT_URL` at it (default
`http://localhost:8081/v2/check`): `docker run -d -p 8081:8010 erikvl87/languagetool:6.8`,
or the [server zip](https://languagetool.org/download/) with Java 17+. The public
LanguageTool API also works, but it has rate limits and sends your text to a third
party, so don't use it for anything unpublished.

Languages, input formats (HTML, Markdown, JSON, plain text), ignore lists, every
option and exit code, and what happens when a layer is missing:
[`docs/usage.md`](docs/usage.md).

## GitHub Action

Fail a pull request when the copy has a mistake, with each finding on its line in the
diff:

```yaml
# .github/workflows/copy.yml
name: copy
on: [pull_request]
jobs:
  langcheck:
    runs-on: ubuntu-latest   # the LanguageTool container needs a Linux runner
    steps:
      - uses: actions/checkout@v5
      - uses: terezakohutova/copy-langcheck@v1
        with:
          files: copy/*.json copy/sk-landing.html   # or one path per line
          ignore: copy/brand-words.txt
```

The Action starts a LanguageTool container pinned by digest and removes it at the end
of the step, fails with exit 3 if it did not come up (`strict: true`, the default),
and prints a GitHub annotation per finding:

```
::error file=examples/landing-pl.html,line=5,col=15,title=copy-langcheck hunspell::nagrode -> nagrodę, Nagrodę, nagrody (unknown word)
::error file=examples/landing-pl.html,line=6,col=6,title=copy-langcheck hunspell::Sprawdz -> Sprawdź, Sprawdza, Sprawdzi (unknown word)
```

| Input | Default | |
|---|---|---|
| `files` | (required) | paths or globs, space-separated or one per line |
| `lang` / `force-lang` | | fallback language / override for every file |
| `languagetool` | `true` | start the pinned container (Linux runners) |
| `lt-url` | | use your own LanguageTool server instead (any runner) |
| `strict` | `true` | exit 3 when a layer did not check every text |
| `online` | `false` | also Korektor for Czech; the text leaves the runner |
| `suggest` | `false` | Hunspell suggestions (seconds per unknown word) |
| `ignore`, `disable-rules`, `all-strings` | | as on the command line |

Outputs: `exit-code`, and `report`, the path of the JSON report. A
[selftest workflow](.github/workflows/selftest.yml) runs the Action on every push
against a real LanguageTool container and checks the exact exit codes.

## Limits, honestly

- **A wrong word that is also a real word passes.** Slovak `prehlad` instead of
  `prehľad` is in the dictionary, so Hunspell accepts it, and LanguageTool did not
  flag it in testing either. Read the copy yourself.
- **Korektor's suggestion can be wrong even when the flag is right.** In the Czech
  example it correctly flags `ziskej` but suggests `ziskem`; Hunspell suggests the
  right `získej`. Treat suggestions as hints.
- **No tone, truth or brand rules.** The tools don't know your tone of voice, your
  banned claims or whether a number is true.
- **Short headlines without a verb** can trip grammar rules. Formatting-only rules
  (missing full stop, typographic quotes) are already off; switch off more with
  `--disable-rule`.
- **Czech has no grammar layer here**, only spelling (Hunspell, plus Korektor if you
  may use it).
- **spylls is finished, not maintained** (last release 2022). It is pinned, tested on
  three OSes, and the fallback plan is in [`docs/usage.md`](docs/usage.md#spylls).

## Korektor terms

Korektor's code is BSD-licensed, but the models behind the public LINDAT service are
**CC BY-NC-SA**, and the service page says that commercial use needs explicit written
permission of the authors and that submitted text may be kept to improve their
systems. Checking commercial marketing copy through it is therefore off by default.
Turn it on with `--online` (or `online: true` in the Action) only if your use is
non-commercial or you have that permission. Czech is still checked by Hunspell
without it.

## Tests

```
python -m unittest -v
```

The unit tests run offline: tiny fixture dictionaries stand in for the LibreOffice
ones, and LanguageTool and Korektor are mocked, including their failure modes. If
`./dicts` exists, the bundled examples are also checked against the real
dictionaries.

## Licence

The code is MIT, see [LICENSE](LICENSE). The dictionaries are downloaded from the
LibreOffice repository by `fetch_dicts.py` and keep their own licences (MPL, LGPL,
GPL and others); they are not redistributed here. LanguageTool is LGPL. Korektor: see
[Korektor terms](#korektor-terms). Changes per version: [CHANGELOG.md](CHANGELOG.md).

---

### Česky

Kontrola pravopisu a gramatiky pro marketingové texty v češtině, slovenštině, polštině, němčině (de-DE, de-AT, de-CH) a angličtině (en-GB, en-US). Spojuje Hunspell (lokálně, slovníky LibreOffice), LanguageTool (vlastní server, gramatika pro sk, pl, de, en) a volitelně Korektor z ÚFAL MFF UK (čeština, pravopis a diakritika v kontextu, veřejné API; modely mají licenci CC BY-NC-SA, proto je ve výchozím stavu vypnutý). Čeština je tu jen pravopis, gramatickou vrstvu nemá. Nic nepřepisuje, jen hlásí, u každého nálezu se souborem, řádkem a sloupcem: exit 1 při nálezu, exit 3 když některá vrstva nemohla zkontrolovat všechno (například neběží LanguageTool), takže může hlídat texty před nahráním reklamy nebo před vydáním webu. Jazyk se zadává jako BCP 47 (`cs`, ne `cz`). Instalace: `python -m pip install -r requirements.txt` a `python fetch_dicts.py`. Na tón, pravdivost a značku se dívá pořád člověk.
