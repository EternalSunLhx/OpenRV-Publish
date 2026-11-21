"""Apply the locally validated Windows fixes to downloaded dependency sources."""
from __future__ import annotations

import argparse
import os
import pathlib
import re


def fix_ffmpeg(source: pathlib.Path) -> None:
    path = source / "configure"
    text = path.read_text(encoding="utf-8")
    marker = "# OpenRV localized-release: recognize localized MSVC banners."
    if marker in text:
        return
    if "grep -q ^Microsoft" not in text:
        raise RuntimeError("Unexpected FFmpeg configure; review locale patch against the new version")
    text = text.replace("grep -q ^Microsoft", "grep -q Microsoft").replace(
        "| grep ^Microsoft |", "| grep Microsoft |")
    original = "if $_cc -nologo- 2>&1 | grep -q Linker; then"
    replacement = ('if $_cc -nologo- 2>&1 | grep -q Linker || '
                   '[ "$(basename "$_cc")" = "mslink" ] || '
                   '[ "$(basename "$_cc")" = "link.exe" ]; then')
    if original not in text:
        raise RuntimeError("Unexpected FFmpeg linker detection")
    text = text.replace(original, replacement).replace("probe_cc(){", marker + "\nprobe_cc(){")
    path.write_text(text, encoding="utf-8", newline="\n")


def fix_openssl(source: pathlib.Path, install: pathlib.Path) -> None:
    destination = install / "lib" / "pkgconfig"
    destination.mkdir(parents=True, exist_ok=True)
    for name in ("openssl", "libssl", "libcrypto"):
        # Source-root .pc files are build-only. exporters contains install metadata.
        path = source / "exporters" / f"{name}.pc"
        if not path.is_file():
            raise FileNotFoundError(f"OpenSSL install exporter is missing: {path}")
        text = re.sub(r"(?m)^prefix=.*$", "prefix=" + install.as_posix(),
                      path.read_text(encoding="utf-8"))
        (destination / path.name).write_text(text, encoding="utf-8", newline="\n")


def fix_boost(source: pathlib.Path) -> None:
    # B2 otherwise auto-selects the newest VS toolset even when vcvars selected 14.40.
    tools = pathlib.Path(os.environ["VCToolsInstallDir"])
    compiler = tools / "bin" / "Hostx64" / "x64" / "cl.exe"
    vcvars = pathlib.Path(os.environ["VSINSTALLDIR"]) / "VC" / "Auxiliary" / "Build" / "vcvarsall.bat"
    if not compiler.is_file() or not vcvars.is_file() or "14.40" not in str(tools):
        raise RuntimeError("Expected the explicitly selected MSVC 14.40 environment")
    wrapper = source / "openrv-vcvars-1440.cmd"
    wrapper.write_text('@echo off\ncall "' + str(vcvars) + '" x64 -vcvars_ver=14.40 >nul\n'
                       'if errorlevel 1 exit /b %errorlevel%\n'
                       'if not "%VCToolsVersion:~0,5%"=="14.40" exit /b 20\n'
                       'set VSLANG=1033\nexit /b 0\n', encoding="ascii", newline="\r\n")
    (source / "project-config.jam").write_text(
        'import option ;\nusing msvc : 14.3 : "' + compiler.as_posix() +
        '" : <setup>"' + wrapper.as_posix() + '" ;\noption.set keep-going : false ;\n',
        encoding="ascii")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["ffmpeg", "openssl", "boost"])
    parser.add_argument("source", type=pathlib.Path)
    parser.add_argument("install", type=pathlib.Path, nargs="?")
    args = parser.parse_args()
    if args.mode == "openssl":
        if not args.install:
            parser.error("openssl requires its install prefix")
        fix_openssl(args.source, args.install)
    elif args.mode == "ffmpeg":
        fix_ffmpeg(args.source)
    else:
        fix_boost(args.source)
