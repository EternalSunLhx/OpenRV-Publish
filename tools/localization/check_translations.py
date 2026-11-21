"""Validate the shipped Chinese Qt catalog and display-only menu coverage."""
from __future__ import annotations

import argparse
import collections
import json
from pathlib import Path
import re
import subprocess
import sys
import xml.etree.ElementTree as ET

from update_menu_paths import checkout_path, report_path


def placeholders(text: str) -> collections.Counter:
    return collections.Counter(re.findall(r"%L?\d+|%n|%(?:\d+\$)?[-+#0]?(?:\d+)?(?:\.\d+)?[sdifgeuoxXc]", text))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    repo = args.repo.resolve()
    source = checkout_path(repo, repo / "src/lib/app/RvCommon/qrc/translations/i18n_zh.ts")
    document = ET.parse(source).getroot()
    issues = []
    keys = set()
    native = menu = translated = identity = 0
    menu_sources = set()
    for context in document.findall("context"):
        name = context.findtext("name", "")
        for message in context.findall("message"):
            english = message.findtext("source", "")
            comment = message.findtext("comment", "")
            key = (name, english, comment)
            if key in keys:
                issues.append(f"Duplicate key: {key!r}")
            keys.add(key)
            translation = message.find("translation")
            if translation is None or translation.get("type") in {"unfinished", "vanished", "obsolete"}:
                issues.append(f"Unfinished/missing translation: {key!r}")
                continue
            chinese = "".join(translation.itertext())
            if english and not chinese:
                issues.append(f"Empty translation: {key!r}")
            if placeholders(english) != placeholders(chinese):
                issues.append(f"Formatting arguments differ: {key!r}")
            if english.count("\n") != chinese.count("\n"):
                issues.append(f"Paragraph breaks differ: {key!r}")
            if name == "RvMenu":
                menu += 1
                menu_sources.add(english.strip())
                for edge in (r"^\s*", r"\s*$"):
                    if re.search(edge, english).group() != re.search(edge, chinese).group():
                        issues.append(f"Menu indentation differs: {key!r}")
            else:
                native += 1
            translated += english != chinese
            identity += english == chinese
    header = checkout_path(repo, repo / "src/lib/app/RvCommon/RvCommon/RvMenuPaths.h").read_text(encoding="utf-8")
    paths = [json.loads(value) for value in re.findall(r'QStringLiteral\(("(?:\\.|[^"\\])*")\)', header)]
    for path in paths:
        if path.startswith("@popup/") and path.count("/") == 1:
            continue  # Explicit popup translation namespace; never a caption.
        if re.fullmatch(r"\d+(?:\.\d+)?(?::\d+(?:\.\d+)?)?", path.rsplit("/", 1)[-1]):
            continue  # Frame rates, scales and bit counts have no language.
        if not any(path == caption or path.endswith("/" + caption) for caption in menu_sources if caption):
            issues.append(f"Static menu caption has no catalog entry: {path}")
    # The checkout can be a newly merged upstream candidate. Always execute the
    # trusted sibling, never a Python script from that candidate with a write token.
    menu_check = subprocess.run([sys.executable, str(Path(__file__).with_name("update_menu_paths.py")),
                                 "--repo", str(repo), "--check"], capture_output=True, text=True)
    if menu_check.returncode:
        issues.append("Static menu path generation differs: " + menu_check.stderr.strip())
    report = {"passed": not issues, "catalog": source.relative_to(repo).as_posix(),
              "messages": len(keys), "nativeMessages": native, "menuMessages": menu,
              "translatedMessages": translated, "technicalIdentityMessages": identity,
              "staticCaptionPaths": len(paths), "issues": issues}
    if args.report:
        report_path(repo, args.report)
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return 0 if not issues else 1


if __name__ == "__main__":
    raise SystemExit(main())
