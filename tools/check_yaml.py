#!/usr/bin/env python3
"""Parse every plain-YAML manifest under the given roots (multi-document aware).

Used by `make check` as an offline sanity gate on the committed manifests. It is
deliberately conservative about what counts as "plain YAML":

  * files that carry Helm template delimiters (``{{`` ... ``}}``) are Helm chart
    templates, not standalone YAML — they are skipped (validate those with
    ``helm template`` instead);
  * ``*.example`` scaffolding files are skipped.

KRO ResourceGraphDefinitions use ``${...}`` substitutions, which are ordinary
YAML scalars and parse fine, so they ARE checked. Exit status is non-zero if any
checked file fails to parse.
"""

import os
import sys

import yaml


def _is_helm_template(text: str) -> bool:
    return "{{" in text and "}}" in text


def main(roots: list[str]) -> int:
    checked = skipped = 0
    failures: list[str] = []
    for root in roots:
        for dirpath, _dirs, files in os.walk(root):
            if "/.terraform/" in dirpath or dirpath.endswith("/.terraform"):
                continue
            for fn in files:
                if not (fn.endswith(".yaml") or fn.endswith(".yml")):
                    continue
                if fn.endswith(".example"):
                    skipped += 1
                    continue
                path = os.path.join(dirpath, fn)
                with open(path, encoding="utf-8") as fh:
                    text = fh.read()
                if _is_helm_template(text):
                    skipped += 1
                    continue
                try:
                    list(yaml.safe_load_all(text))
                    checked += 1
                except yaml.YAMLError as e:
                    failures.append(f"{path}: {e}")
    for f in failures:
        print(f"  YAML PARSE FAILED: {f}", file=sys.stderr)
    print(f"  yaml: {checked} parsed, {skipped} skipped (helm/example), {len(failures)} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:] or ["platform", "workloads"]))
