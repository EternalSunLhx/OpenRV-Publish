"""Extract current UI sources and prepare a manual Chinese translation checklist.

Run this trusted script against a checkout with --repo. No checkout Python, Mu,
CMake, plugin, or build hook is executed. Qt lupdate parses the native sources;
the literal script/menu bridge is parsed by the trusted sibling tool.
"""
from __future__ import annotations

import argparse
import ast
import copy
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET

from update_menu_paths import args_at, checkout_path, extract_menus, mask_comments

CATALOG = Path("src/lib/app/RvCommon/qrc/translations/i18n_zh.ts")
COMMON = Path("src/lib/app/RvCommon")
UI_BASELINE = Path(__file__).with_name("unwrapped-ui-baseline.json")


def script_files(repo):
    files = sorted((repo / "src/lib/app/mu_rvui").glob("*.mu*"))
    files += sorted((repo / "src/plugins/rv-packages").rglob("*.mu*"))
    files += sorted((repo / "src/plugins/rv-packages").rglob("*.py"))
    return [p for p in files if not (p.suffix == ".mu" and p.with_name(p.name + ".in").exists())]


def unwrapped_ui_sources(repo):
    """Inventory literal UI sinks which do not call the translation bridge.

    A trusted, reviewed baseline permits existing technical/internal captions.
    Newly introduced literals need a human to wire translation or explicitly
    record an intentional technical identity. Values computed from user data
    are never extracted or translated.
    """
    methods = {"setText", "setToolTip", "setStatusTip", "setWindowTitle", "setTitle",
               "setPlaceholderText", "addAction", "addMenu", "QLabel", "QPushButton",
               "QCheckBox", "QRadioButton", "QGroupBox", "QMenu"}
    files = script_files(repo)
    files += sorted((repo / COMMON).glob("*.cpp"))
    found = {}
    pattern = r'\b(' + "|".join(sorted(methods)) + r')\s*\(\s*("(?:\\.|[^"\\])*")'
    for path in files:
        relative = path.relative_to(repo).as_posix()
        text = checkout_path(repo, path).read_text(encoding="utf-8")
        if path.suffix == ".py":
            for node in ast.walk(ast.parse(text, filename=relative)):
                if not isinstance(node, ast.Call) or not node.args:
                    continue
                function = node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id if isinstance(node.func, ast.Name) else ""
                value = node.args[0]
                if function in methods and isinstance(value, ast.Constant) and isinstance(value.value, str) and value.value:
                    found[(relative, function, value.value)] = node.lineno
        else:
            clean = mask_comments(text)
            for match in re.finditer(pattern, clean):
                source = literal(match[2])
                if source:
                    found[(relative, match[1], source)] = clean.count("\n", 0, match.start()) + 1
    return found


def other_form_sources(repo):
    """New UI forms outside the reviewed native/package scope need a bridge review."""
    found = {}
    for path in sorted((repo / "src").rglob("*.ui")):
        relative = path.relative_to(repo)
        if (relative.parent == COMMON / "ui"
                or relative.parent == COMMON / "ui/RvPreferences/Qt6"
                or relative.parent in {Path("src/plugins/rv-packages/session_manager"), Path("src/plugins/rv-packages/annotate")}
                or "Qt5" in relative.parts):
            continue
        root = ET.parse(checkout_path(repo, path)).getroot()
        context = root.findtext("class", "")
        for item in root.iter("string"):
            if item.get("notr") != "true" and item.text:
                found[(relative.as_posix(), context, item.text, item.get("comment", ""))] = ""
    return found


def key(context, message):
    return context, message.findtext("source", ""), message.findtext("comment", "")


def completed(message):
    translation = message.find("translation")
    return (translation is not None
            and translation.get("type") not in {"unfinished", "vanished", "obsolete"}
            and (not message.findtext("source", "") or bool("".join(translation.itertext()))))


def write_xml(path, root):
    ET.indent(root, space="    ")
    content = '<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE TS>\n'
    content += ET.tostring(root, encoding="unicode") + "\n"
    path.write_text(content, encoding="utf-8")


def literal(expression):
    try:
        value = ast.literal_eval(expression.strip())
    except (ValueError, SyntaxError):
        return None
    return value if isinstance(value, str) else None


def script_sources(repo):
    """Only explicit display translation calls, never arbitrary data strings."""
    for path in script_files(repo):
        text = checkout_path(repo, path).read_text(encoding="utf-8")
        relative = path.relative_to(repo).as_posix()
        if path.suffix == ".py":
            # AST parsing does not import the module or run its statements.
            tree = ast.parse(text, filename=relative)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                name = node.func.id if isinstance(node.func, ast.Name) else ""
                if name in {"_rv_tr", "translate_ui"} and node.args and isinstance(node.args[0], ast.Constant):
                    source = node.args[0].value
                    if isinstance(source, str):
                        yield ("RvMenu", source, ""), relative, node.lineno, False
                if (isinstance(node.func, ast.Attribute) and node.func.attr == "translate"
                        and isinstance(node.func.value, ast.Attribute) and node.func.value.attr == "QCoreApplication"
                        and len(node.args) >= 2
                        and all(isinstance(arg, ast.Constant) and isinstance(arg.value, str) for arg in node.args[:2])):
                    comment = node.args[2].value if len(node.args) > 2 and isinstance(node.args[2], ast.Constant) else ""
                    numerus = len(node.args) > 3 and not (isinstance(node.args[3], ast.Constant) and node.args[3].value == -1)
                    yield (node.args[0].value, node.args[1].value, comment or ""), relative, node.lineno, numerus
            continue
        # Mu and fully qualified Qt translate calls use literal contexts/sources.
        clean = mask_comments(text)
        pattern = r'\bQCoreApplication\.translate\s*\('
        for match in re.finditer(pattern, clean):
            arguments, _ = args_at(clean, clean.index("(", match.start()))
            if len(arguments) < 2:
                continue
            context = literal(clean[slice(*arguments[0])])
            source = literal(clean[slice(*arguments[1])])
            comment = literal(clean[slice(*arguments[2])]) if len(arguments) > 2 else ""
            comment = comment or ""
            if context is not None and source is not None:
                numerus = len(arguments) > 3 and clean[slice(*arguments[3])].strip() != "-1"
                yield (context, source, comment), relative, clean.count("\n", 0, match.start()) + 1, numerus


def find_lupdate(explicit):
    if explicit:
        candidate = Path(explicit).expanduser()
        if candidate.is_file():
            return str(candidate.resolve())
        resolved = shutil.which(explicit)
        if resolved:
            return resolved
        raise RuntimeError("The supplied lupdate executable does not exist")
    for name in ("lupdate", "lupdate6", "/usr/lib/qt6/bin/lupdate"):
        resolved = shutil.which(name)
        if resolved:
            return resolved
    raise RuntimeError("Qt lupdate is required; pass --lupdate /path/to/Qt/bin/lupdate")


def extract_native(repo, lupdate, scratch):
    common = repo / COMMON
    # Headers must precede implementations and include root must be supplied so
    # Qt resolves Rv:: class namespaces exactly as it does in the runtime.
    files = sorted((common / "RvCommon").rglob("*.h"))
    files += sorted(common.glob("*.cpp"))
    files += sorted((common / "ui").glob("*.ui"))
    files += sorted((common / "ui/RvPreferences/Qt6").glob("*.ui"))
    for package in ("session_manager", "annotate"):
        files += sorted((repo / "src/plugins/rv-packages" / package).glob("*.ui"))
    if not files:
        raise RuntimeError("No native Qt UI sources found in --repo checkout")
    for path in files:
        checkout_path(repo, path)
    target = scratch / "native.ts"
    arguments = [str(p.relative_to(repo).as_posix()) for p in files]
    if any("\n" in argument or "\r" in argument for argument in arguments):
        raise RuntimeError("Source filenames cannot contain response-file line breaks")
    # A response file avoids the Windows command length limit. Every line is a
    # source filename; options and -ts target are always controlled by this tool.
    response = scratch / "native-sources.txt"
    response.write_text("\n".join(arguments) + "\n", encoding="utf-8")
    result = subprocess.run([lupdate, "@" + str(response), "-I", COMMON.as_posix(),
                             "-no-obsolete", "-locations", "absolute", "-ts", str(target)],
                            cwd=repo, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode or not target.exists():
        raise RuntimeError("Qt lupdate failed: " + (result.stderr + result.stdout)[-5000:])
    root = ET.parse(target).getroot()
    result = {}
    for context in root.findall("context"):
        for message in context.findall("message"):
            locations = []
            for location in message.findall("location"):
                path = Path(location.get("filename", ""))
                if not path.is_absolute():
                    path = target.parent / path
                try:
                    filename = path.resolve().relative_to(repo).as_posix()
                except ValueError:
                    raise RuntimeError("lupdate returned a location outside the checkout")
                locations.append({"file": filename, "line": location.get("line", "")})
            result[key(context.findtext("name", ""), message)] = {
                "locations": locations, "numerus": message.get("numerus") == "yes",
                "extraComment": message.findtext("extracomment", "")}
    return result


def synchronize(repo, output, lupdate, apply=False):
    repo = repo.resolve()
    absolute_output = Path(os.path.abspath(output))
    if absolute_output.is_relative_to(repo) and not output.resolve().is_relative_to(repo):
        raise RuntimeError("Candidate report directory resolves outside --repo")
    output.mkdir(parents=True, exist_ok=True)
    for filename in ("missing-translations.json", "missing-translations.md", "candidate-i18n_zh.ts"):
        if (output / filename).is_symlink():
            raise RuntimeError("Generated report targets must not be symbolic links")
    root = ET.parse(checkout_path(repo, repo / CATALOG)).getroot()
    contexts = {c.findtext("name", ""): c for c in root.findall("context")}
    existing = {key(c.findtext("name", ""), m): m
                for c in root.findall("context") for m in c.findall("message")}
    with tempfile.TemporaryDirectory(prefix="openrv-lupdate-", dir=output.resolve()) as temporary:
        observed = extract_native(repo, lupdate, Path(temporary))
    menus = extract_menus(repo)
    for caption, locations in menus["captions"].items():
        if not re.fullmatch(r"\d+(?:\.\d+)?(?::\d+(?:\.\d+)?)?", caption.strip()):
            observed.setdefault(("RvMenu", caption, ""), {"locations": [], "numerus": False, "extraComment": ""})["locations"].extend(locations)
    for item, filename, line, numerus in script_sources(repo):
        details = observed.setdefault(item, {"locations": [], "numerus": numerus, "extraComment": ""})
        details["locations"].append({"file": filename, "line": str(line)})
        details["numerus"] |= numerus
    missing = []
    baseline_document = json.loads(UI_BASELINE.read_text(encoding="utf-8"))
    baseline = {tuple(row) for row in baseline_document["entries"]}
    needs_bridge = [{"file": item[0], "call": item[1], "source": item[2], "line": line}
                    for item, line in sorted(unwrapped_ui_sources(repo).items()) if item not in baseline]
    form_baseline = {tuple(row) for row in baseline_document.get("formEntries", [])}
    for item in sorted(other_form_sources(repo)):
        if item not in form_baseline:
            needs_bridge.append({"file": item[0], "call": "Qt form: " + item[1], "source": item[2], "line": ""})
    added = 0
    for item, details in sorted(observed.items()):
        locations = details["locations"]
        message = existing.get(item)
        if message is None:
            context = contexts.get(item[0])
            if context is None:
                context = ET.SubElement(root, "context")
                ET.SubElement(context, "name").text = item[0]
                contexts[item[0]] = context
            message = ET.SubElement(context, "message")
            if details["numerus"]:
                message.set("numerus", "yes")
            for location in locations:
                # TS locations remain relative to the actual shipped TS path.
                filename = Path(os.path.relpath(repo / location["file"], (repo / CATALOG).parent)).as_posix()
                ET.SubElement(message, "location", filename=filename, line=str(location["line"]))
            ET.SubElement(message, "source").text = item[1]
            if item[2]:
                ET.SubElement(message, "comment").text = item[2]
            if details["extraComment"]:
                ET.SubElement(message, "extracomment").text = details["extraComment"]
            translation = ET.SubElement(message, "translation", type="unfinished")
            if details["numerus"]:
                ET.SubElement(translation, "numerusform")
            existing[item] = message
            added += 1
        if not completed(message):
            missing.append({"context": item[0], "source": item[1], "comment": item[2],
                            "numerus": details["numerus"], "extraComment": details["extraComment"],
                            "locations": sorted({(x["file"], str(x["line"])) for x in locations}),
                            "reason": "new source" if (message.find("translation") is None or message.find("translation").text is None) else "unfinished translation"})
    # Existing unfinished entries also block a release, including technical
    # identities that must be explicitly reviewed rather than auto-approved.
    pending = {(x["context"], x["source"], x["comment"]) for x in missing}
    for item, message in sorted(existing.items()):
        if not completed(message) and item not in pending:
            missing.append({"context": item[0], "source": item[1], "comment": item[2],
                            "numerus": message.get("numerus") == "yes", "extraComment": message.findtext("extracomment", ""),
                            "locations": [], "reason": "unfinished translation"})
    for entry in missing:
        entry["locations"] = [{"file": p, "line": line} for p, line in entry["locations"]]
    missing.sort(key=lambda x: (x["context"], x["source"], x["comment"]))
    report = {"passed": not missing and not needs_bridge, "catalog": CATALOG.as_posix(), "observedMessages": len(observed),
              "newMessages": sum(entry["reason"] == "new source" for entry in missing), "missingMessages": len(missing), "entries": missing,
              "needsTranslationBridge": needs_bridge,
              "coverage": "Native RvCommon C++ and Qt6 forms; shipped session/annotation forms; literal opted-in Mu menus and explicit script translation calls"}
    (output / "missing-translations.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    markdown = ["# OpenRV 中文人工补译清单", "", f"待补译：{len(missing)} 项；新静态界面文案需接入翻译：{len(needs_bridge)} 项。", "",
                "在源 TS 中填写译文，删除该条 translation 的 type=\"unfinished\"；保留格式参数、换行和缩进。",
                "菜单内部 ID、文件名和用户数据无需翻译。完成后重新运行同步和校验；不会调用在线翻译服务。", ""]
    for number, entry in enumerate(missing, 1):
        markdown += [f"## {number}. {entry['context']}", "", "英文：", "", "```text", entry["source"], "```", ""]
        if entry["comment"]:
            markdown += ["消歧备注：" + entry["comment"], ""]
        if entry["extraComment"]:
            markdown += ["源码备注：" + entry["extraComment"], ""]
        if entry["numerus"]:
            markdown += ["复数条目：在 TS 的 numerusform 中填写译文，并保留 %n。", ""]
        markdown += ["位置：" + (", ".join(x["file"] + ":" + str(x["line"]) for x in entry["locations"]) or "已有 TS 条目"), "", "中文：待填写", ""]
    if needs_bridge:
        markdown += ["## 新增界面文案接入检查", "", "这些调用使用了新的固定英文文案。请在对应代码中接入 tr / translate_ui / QCoreApplication.translate，再运行此工具生成 TS 条目。若为应保留英文的技术标识，审核后更新可信工具的基线；不要翻译用户数据。", ""]
        for entry in needs_bridge:
            markdown += [f"- {entry['file']}:{entry['line']} — {entry['call']}: {json.dumps(entry['source'], ensure_ascii=False)}"]
        markdown.append("")
    (output / "missing-translations.md").write_text("\n".join(markdown) + "\n", encoding="utf-8")
    write_xml(output / "candidate-i18n_zh.ts", copy.deepcopy(root))
    if apply and added:
        write_xml(repo / CATALOG, root)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--lupdate", help="Trusted Qt lupdate executable, never a candidate checkout script")
    parser.add_argument("--apply", action="store_true", help="Add new unfinished entries to the candidate catalog; preserve all existing translations")
    parser.add_argument("--accept-ui-baseline", action="store_true", help="Maintainer only: record reviewed unwrapped literal UI sinks in this trusted tool's baseline")
    args = parser.parse_args()
    try:
        if args.accept_ui_baseline:
            records = sorted(unwrapped_ui_sources(args.repo.resolve()))
            UI_BASELINE.write_text(json.dumps({"entries": records, "formEntries": sorted(other_form_sources(args.repo.resolve()))}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        report = synchronize(args.repo, args.output_dir, find_lupdate(args.lupdate), args.apply)
    except (RuntimeError, OSError, ET.ParseError, SyntaxError) as error:
        parser.exit(2, str(error) + "\n")
    print(json.dumps({k: v for k, v in report.items() if k != "entries"}, ensure_ascii=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
