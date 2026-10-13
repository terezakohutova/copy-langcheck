# Changelog

## 1.3.0 (2026-10-14)

Fixes from a second outside review. Two changes can turn a result that used to pass into a failure, see **Changed behaviour**.

### Changed behaviour

- **Exit 3 wins over exit 1 in strict mode.** When something was found and a layer that applies did not check every text, the result is now 3, not 1, and the findings are still listed (text output and JSON report). Before, findings hid the incomplete check. With `--lenient`, findings still give 1 unless a text was checked by no layer.
- **Path-like arguments are files.** An argument without spaces that has a `/` or `\` or ends in something like an extension (known or not: `ads.yaml`, `copy/ads`, `vis.zkus`) is read as a file, and a missing one is exit 2. Before, only `.txt .md .markdown .html .htm .json` counted, so `copy/ads.yaml` with a typo was checked as if it were copy. **Backward compatibility:** copy as a plain argument still works when it does not look like a path, but prints a deprecation notice; use the new `--text "copy"` (repeatable) or stdin. Plain-argument copy will be removed in 2.0.
- **Skipped markup becomes a placeholder.** Inline code in Markdown and `code`, `kbd`, `samp` in HTML are replaced by `X` instead of being removed, so "Run `npm ci` now" is checked as "Run X now", not "Run now". A text that is nothing but code is not checked.
- **Fewer suggestions by default.** Only the first 20 unknown words per language get Hunspell suggestions (`--suggest-limit N`); the rest are reported without them and a note says how many. Measured numbers are in the README.
- **Retries only where they can help:** network errors, timeouts and 5xx. A 4xx or an unreadable answer is not retried.

### Added

- `--text`, `--no-suggest`, `--suggest-limit N`.
- HTML: `translate="no"` and `class="notranslate"` are skipped (inline ones become the placeholder), `translate="yes"` inside them is checked again. `pre` now ends a text.
- Markdown: YAML front matter is skipped except `title` and `description`; HTML comments over several lines are skipped.
- LanguageTool gets up to 4 texts at once, and every failure is counted in the note instead of only the last one.
- Korektor: when a batch does not come back one to one, the per-text fallback is capped at 20 requests per run; the rest are listed as not checked.
- Action: the LanguageTool container gets a name unique to the run, attempt, step and inputs and a port Docker picks, a healthy server already on port 8081 is reused, and the container is removed at the end of the step by a `trap` (composite actions have no post step). The logic is in `scripts/languagetool.sh` and tested with stub `docker` and `curl`.
- `korektor-smoke.yml`, a workflow you start by hand that checks the real Korektor API.

### Fixed

- **Exact positions.** Every character of a checked text keeps its source offset through the extraction (HTML entities and attributes, JSON escapes, skipped markup). Before, a word was searched for in the raw source, so a word that also appeared in an `href`, inline code or a JSON key or non-copy value could be reported at the wrong place.
- JSON values are located by a small scanner that records the offset of every string value by its path, instead of a text search.
- Dictionary cache keys (Action and CI) depend on `fetch_dicts.py` and its pinned LibreOffice commit, not on `langcheck.py`, so a code change no longer downloads the dictionaries again.
- `requirements.txt` pins `spylls==0.1.7` with hashes. The README describes its maintenance status and the fallback plan.

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
