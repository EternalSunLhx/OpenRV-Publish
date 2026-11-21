"""Extract display-only opt-in paths from literal, shipped Mu menu definitions."""
import argparse
import ast
import json
from pathlib import Path
import re

roots = {"File", "Edit", "Control", "Tools", "Audio", "Image", "Color", "View", "Help",
         "Annotation", "Window", "Layout", "Source", "Stack", "Switch", "Sequence", "Retime", "Maya", "Nuke"}


def checkout_path(repo, path):
    if not path.resolve().is_relative_to(repo.resolve()):
        raise RuntimeError("Checkout source/target resolves outside --repo")
    return path


def report_path(repo, path):
    absolute = path.absolute()
    if (absolute.is_relative_to(repo.resolve()) and not path.resolve().is_relative_to(repo.resolve())) or path.is_symlink():
        raise RuntimeError("Candidate report target resolves outside --repo or is a symbolic link")
    return path


def mask_comments(text):
    """Mask C++/Mu comments without changing strings, offsets or line numbers."""
    result = list(text)
    index, quote = 0, None
    while index < len(text):
        char = text[index]
        if quote:
            if char == "\\":
                index += 2
                continue
            if char == quote:
                quote = None
        elif char in {'"', "'"}:
            quote = char
        elif text.startswith("//", index) or text.startswith("/*", index):
            if text.startswith("//", index):
                end = text.find("\n", index)
                end = len(text) if end < 0 else end
            else:
                end = text.find("*/", index + 2)
                end = len(text) if end < 0 else end + 2
            for offset in range(index, end):
                if result[offset] != "\n":
                    result[offset] = " "
            index = end
            continue
        index += 1
    return "".join(result)

def args_at(text, opening):
    depth, start, quote = 0, opening + 1, None
    result, i = [], opening + 1
    while i < len(text):
        char = text[i]
        if quote:
            if char == "\\":
                i += 2
                continue
            if char == quote:
                quote = None
        elif char in ('"', "'"):
            quote = char
        elif text.startswith("//", i):
            i = text.find("\n", i)
            if i < 0:
                return [], len(text)
            continue
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = len(text) if end < 0 else end + 2
            continue
        elif char in "([{":
            depth += 1
        elif char in ")]}":
            if char == ")" and depth == 0:
                result.append((start, i))
                return result, i + 1
            depth -= 1
        elif char == "," and depth == 0:
            result.append((start, i))
            start = i + 1
        i += 1
    return [], len(text)

def balanced_end(text, opening):
    """Return one balanced Mu expression's end without inspecting string data."""
    pairs = {"(": ")", "[": "]", "{": "}"}
    stack, quote = [], None
    index = opening
    while index < len(text):
        char = text[index]
        if quote:
            if char == "\\":
                index += 2
                continue
            if char == quote:
                quote = None
        elif char in {'"', "'"}:
            quote = char
        elif char in pairs:
            stack.append(pairs[char])
        elif char in ")]}":
            if not stack or stack.pop() != char:
                return None
            if not stack:
                return index + 1
        index += 1
    return None


def literal_menu_range(text, start):
    match = re.match(r"\s*(?:newMenu\s*\(|(?:MenuItem\s*\[\s*\]|Menu)\s*\{|\[)", text[start:])
    if not match:
        return None
    opening = start + match.end() - 1
    end = balanced_end(text, opening)
    return (opening + 1, end - 1) if end else None


def brace_scopes(text):
    stack, result, quote = [], [], None
    index = 0
    while index < len(text):
        char = text[index]
        if quote:
            if char == "\\":
                index += 2
                continue
            if text.startswith(quote, index):
                index += len(quote)
                quote = None
                continue
        elif char in {'"', "'"}:
            quote = char * 3 if text.startswith(char * 3, index) else char
            index += len(quote)
            continue
        elif char == "{":
            stack.append(index)
        elif char == "}" and stack:
            result.append((stack.pop(), index + 1))
        index += 1
    return result


def menu_nodes(text):
    """Keep dynamic constructors as barriers, while recording only literal labels."""
    nodes = []
    for match in re.finditer(r"\b(subMenu|menuItem|menuText)\s*\(", text):
        arguments, end = args_at(text, text.index("(", match.start()))
        if not arguments:
            continue
        try:
            caption = ast.literal_eval(text[slice(*arguments[0])].strip())
        except (ValueError, SyntaxError):
            caption = None
        if not isinstance(caption, str):
            caption = None
        parents = [node for node in nodes if node["start"] < match.start() < node["end"]]
        nodes.append({"start": match.start(), "end": end, "kind": match[1],
                      "caption": caption, "arguments": arguments,
                      "parent": max(parents, key=lambda node: node["start"]) if parents else None})
    return nodes


def extract_file_menus(text):
    """Resolve literal submenu arrays, local Menu variables and zero-arg builders.

    This is deliberately a source parser, not a Mu interpreter. Dynamic labels,
    parameterized calls, unknown expressions and reassigned variables are not
    eligible. A literal child below a dynamic submenu is not promoted to its
    parent's path.
    """
    nodes = menu_nodes(text)
    scopes = brace_scopes(text)
    declarations = []
    for match in re.finditer(r"\b(?:rvtypes\.)?(?:Menu|MenuItem\s*\[\s*\])\s+(\w+)\s*=", text):
        expression = literal_menu_range(text, match.end())
        if not expression:
            continue
        parents = [(start, end) for start, end in scopes if start < match.start() < end]
        scope = max(parents, key=lambda item: item[0]) if parents else (0, len(text))
        declarations.append({"name": match[1], "start": match.start(), "range": expression, "scope": scope})
    builders = {}
    for match in re.finditer(r"(?:\\:|method:)\s*(\w+)\s*\(\s*(?:rvtypes\.)?Menu\s*;\s*\)\s*\{", text):
        opening = match.end() - 1
        end = balanced_end(text, opening)
        if end:
            builders.setdefault(match[1], []).append((opening + 1, end - 1))

    def variable_ranges(name, position):
        candidates = [item for item in declarations if item["name"] == name
                      and item["start"] < position < item["scope"][1]
                      and item["scope"][0] < position and item["range"][1] < position]
        if not candidates:
            return []
        declaration = max(candidates, key=lambda item: (item["scope"][0], item["start"]))
        between = text[declaration["range"][1] + 1:position]
        if (re.search(r"\b" + re.escape(name) + r"\s*(?:\[[^\]]*\]\s*)?=(?!=)", between)
                or re.search(r"\b" + re.escape(name) + r"\s*\.\s*(?!push_back\b)\w+\s*\(", between)):
            return []
        result = [declaration["range"]]
        # Fixed conditional additions (e.g. Maya support on macOS) are also
        # captions. Values and menu labels computed from data are never emitted.
        for addition in re.finditer(r"\b" + re.escape(name) + r"\.push_back\s*\(", between):
            opening = declaration["range"][1] + 1 + between.index("(", addition.start())
            arguments, unused = args_at(text, opening)
            if len(arguments) == 1:
                result.append(arguments[0])
        return result

    def ranges(expression, position, seen):
        start, end = expression
        value = text[start:end].strip()
        direct = literal_menu_range(text, start)
        if direct:
            return [direct]
        if re.fullmatch(r"[A-Za-z_]\w*", value):
            return variable_ranges(value, position)
        call = re.fullmatch(r"([A-Za-z_]\w*)\s*\(\s*\)", value)
        if not call or call[1] in seen or len(builders.get(call[1], [])) != 1:
            return []
        body_start, body_end = builders[call[1]][0]
        body = text[body_start:body_end]
        returned = re.search(r"(?:\breturn\s+)?([A-Za-z_]\w*)\s*;\s*$", body)
        if returned:
            return variable_ranges(returned[1], body_start + returned.start())
        returned = re.search(r"\breturn\s+(newMenu\s*\(|MenuItem\s*\[\s*\]\s*\{)", body)
        if returned:
            direct = literal_menu_range(text, body_start + returned.start(1))
            if direct and not text[direct[1] + 1:body_end].strip().strip(";"):
                return [direct]
        return []

    output = []

    def walk(region, prefix, seen):
        if region in seen:
            return
        seen = seen | {region}
        start, end = region
        direct = [node for node in nodes if start <= node["start"] < node["end"] <= end
                  and not (node["parent"] and start <= node["parent"]["start"] < node["parent"]["end"] <= end)]
        for node in direct:
            caption = node["caption"]
            if caption is None:
                continue
            path = prefix + [caption.strip()]
            output.append((path, caption, node["start"]))
            if node["kind"] != "subMenu" or len(node["arguments"]) < 2:
                continue
            argument = node["arguments"][1]
            value = text[slice(*argument)].strip()
            call = re.fullmatch(r"([A-Za-z_]\w*)\s*\(\s*\)", value)
            next_seen = seen | {call[1]} if call else seen
            for child_region in ranges(argument, node["start"], seen):
                walk(child_region, path, next_seen)

    for node in nodes:
        if node["parent"] is not None or node["caption"] is None or node["caption"].strip() not in roots:
            continue
        # Start at an actual literal root; standalone builder-local items only
        # gain a root through a resolved, literal subMenu mount.
        walk((node["start"], node["end"]), [], set())
    # A popup must explicitly opt into a stable translation scope. Bare user
    # popups and labels computed from data retain their original display text.
    for match in re.finditer(r"\bpopupMenuLocalized\s*\(", text):
        arguments, unused = args_at(text, text.index("(", match.start()))
        if len(arguments) != 3:
            continue
        try:
            scope = ast.literal_eval(text[slice(*arguments[2])].strip())
        except (ValueError, SyntaxError):
            continue
        if not isinstance(scope, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,96}", scope):
            continue
        prefix = ["@popup", scope]
        output.append((prefix, None, match.start()))  # Internal namespace, not UI text.
        for region in ranges(arguments[1], match.start(), set()):
            walk(region, prefix, set())
    return output


def extract_menus(repo):
    paths = set()
    provenance = {}
    captions = {}
    sources = list((repo / "src/lib/app/mu_rvui").glob("*.mu*"))
    sources += list((repo / "src/plugins/rv-packages").rglob("*.mu*"))
    for file in sources:
        if file.suffix == ".mu" and file.with_name(file.name + ".in").exists():
            continue
        text = mask_comments(checkout_path(repo, file).read_text(encoding="utf-8"))
        for path, caption, offset in extract_file_menus(text):
            key = "/".join(path)
            paths.add(key)
            line = text.count("\n", 0, offset) + 1
            if caption is not None:
                captions.setdefault(caption, []).append({"file": file.relative_to(repo).as_posix(), "line": str(line)})
            provenance.setdefault(key, []).append({"file": file.relative_to(repo).as_posix(), "line": line})

    # Package mode entries are assembled dynamically by mode_manager, but the
    # menu paths in these shipped manifests are fixed UI definitions. Installed
    # third-party packages and user/device/profile data are not read here.
    for file in sorted((repo / "src/plugins/rv-packages").glob("*/PACKAGE")):
        text = checkout_path(repo, file).read_text(encoding="utf-8")
        for match in re.finditer(r"^\s+menu:\s*([^\r\n]+)", text, re.M):
            value = match[1].strip()
            if value.startswith(('"', "'")):
                try:
                    value = ast.literal_eval(value)
                except (ValueError, SyntaxError):
                    continue
            if not isinstance(value, str):
                continue
            parts = [part.strip() for part in value.split("/")]
            if len(parts) < 2 or parts[0] not in roots or not all(parts) or any("${" in part for part in parts):
                continue
            line = text.count("\n", 0, match.start(1)) + 1
            for index in range(1, len(parts) + 1):
                key = "/".join(parts[:index])
                paths.add(key)
                captions.setdefault(parts[index - 1], []).append({"file": file.relative_to(repo).as_posix(), "line": str(line)})
                provenance.setdefault(key, []).append({"file": file.relative_to(repo).as_posix(), "line": line})

    # Stack_edit_mode chooses these two fixed root labels from node type. It is
    # not a user title; register only the four fixed actions from that definition.
    for root in ("Layout", "Stack"):
        paths.add(root)
        captions.setdefault(root, [])
        for caption in ("Align Start Frames", "Use Source Cut Info", "Automatically Retime Inputs", "Use Strict Frame Ranges"):
            paths.add(root + "/" + caption)
            captions.setdefault(caption, [])

    return {"paths": paths, "provenance": provenance, "captions": captions}


def render_header(paths):
    header = '''// SPDX-License-Identifier: Apache-2.0
// Generated opt-in paths from shipped literal Mu menus. Internal menu IDs stay
// English. No dynamic source/view/recent-file/device names are registered here.
#pragma once
#include <QSet>
#include <QString>
namespace Rv {
inline const QSet<QString>& translatedStaticMenuPaths()
{
    static const QSet<QString> paths = {
'''
    for path in sorted(paths):
        header += "        QStringLiteral(" + json.dumps(path, ensure_ascii=True) + "),\n"
    header += "    };\n    return paths;\n}\n}\n"
    return header


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--check", action="store_true")
    arguments = parser.parse_args()
    repo = arguments.repo.resolve()
    paths = extract_menus(repo)["paths"]
    header = render_header(paths)
    target = checkout_path(repo, repo / "src/lib/app/RvCommon/RvCommon/RvMenuPaths.h")
    if arguments.check:
        if target.read_text(encoding="utf-8") != header:
            raise SystemExit("Static menu paths are stale; run tools/localization/update_menu_paths.py")
    else:
        target.write_text(header, encoding="utf-8")
    print(json.dumps({"paths": len(paths), "topMenus": sorted(p for p in paths if "/" not in p)}))


if __name__ == "__main__":
    main()
