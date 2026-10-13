# Changelog

## 1.2.0 (2026-10-13)

Fixes from an outside review. Some defaults change, see **Changed behaviour**.

### Changed behaviour

- **Strict by default.** Exit 3 when a layer that applies did not check every text, for example LanguageTool is down. Before, that was a note and exit 0. `--lenient` restores the old rule (exit 3 only when a text was checked by no layer); `--strict` is still accepted. The Action's `strict` input now defaults to `true`.
- **`--lang` is a fallback.** A language stated in the file (HTML `lang`, JSON `lang`) wins. The new `--force-lang` overrides everything.
- **Language tags are BCP 47.** `en-US` and `en-GB` have their own dictionaries and LanguageTool codes (before, `en-US` was checked as British English); `de-AT` and `de-CH` use their own dictionaries and LanguageTool variants (before, both were checked as `de-DE`). `en` still means `en-GB`, `de` means `de-DE`.
- **Country codes are no longer languages.** `uk` was read as English; it is Ukrainian, so it is now an error, as are `cz`, `at`, `ch` and `gb`. Unsupported languages exit 2 with a message saying what to use. A JSON `market` key is no longer read as the language.
- **Ignore lists are case-sensitive**, Hunspell-style: `zorbly` covers `Zorbly` and `ZORBLY`, but `Zorbly` does not cover `zorbly`. Prefix an entry with `i:` for any case.
- **Words in capitals are checked.** Before, any capital word of up to 6 letters was skipped, so `STÁHNOUT ZDRAMA` passed. Now only tokens of up to 4 characters with a digit (`B2B`, `4K`) and listed words are skipped.
- **HTML is read as sentences.** Inline elements (`a`, `span`, `b`...) no longer split a sentence, and `<br>` is a space. `<title>`, the meta description and `og:`/`twitter:` title and description are now checked (`<head>` was skipped entirely).
- The text output starts every finding with `file:line:column`, and the summary names the full tag (`cs-CZ`).
- Czech slang (`appka` and its forms) is no longer in the built-in ignore list. It is in [`examples/ignore.example.txt`](examples/ignore.example.txt).

### Added

- Element-level `lang` in HTML and per-object `lang` in JSON, for mixed-language pages and ad sets. A part in an unsupported language is reported as not checked.
- Positions (`line`, `col`, `offset`) in findings and the JSON report; `--json-out FILE`.
- `--disable-rule ID` and `rule:ID` lines in ignore files to switch off LanguageTool rules.
- One retry for a failed LanguageTool or Korektor request. Texts that still fail are listed as not checked instead of being dropped, findings from earlier texts are kept.
- The same span flagged by two layers is one finding (`[hunspell +languagetool RULE]`).
- Korektor requests are batched, and its output is aligned with the original by tokens and character offsets, so one extra or merged word no longer turns the whole text into a single finding.
- Action: `force-lang`, `lt-url`, `disable-rules` inputs and a `report` output. The LanguageTool image is pinned by digest. On a non-Linux runner the container step fails clearly in strict mode. The job's own Python is used when it is 3.10 or newer, and spylls goes into a private folder instead of the job's environment.
- `examples/grammar-de.txt` and a selftest job that checks, through the JSON report, that a grammar-only mistake is reported by LanguageTool alone.

### Fixed

- Bare-domain detection swallowed words after a missing space (`vis.zkus`). Only a known top-level domain, a scheme or `www.` now counts as an address.

### Deprecated

- `--no-online`: off is the default. It still overrides `--online` and prints a notice; it will be removed in 2.0.

## 1.1.0 (2026-10-13)

Tests and CI on Linux, macOS and Windows, a composite GitHub Action with a self-test, pinned dictionary download, exit codes 2 and 3, and real example outputs. Tagged `v1`.
