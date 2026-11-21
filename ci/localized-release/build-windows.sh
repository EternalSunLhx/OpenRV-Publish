#!/usr/bin/env bash
set -euo pipefail
test "${GITHUB_ACTIONS:-}" = true
# Keep PySide6/MSVC generated object paths below MAX_PATH without moving verified sources.
test "${OPENRV_BUILD_DIR:-}" = D:/b
build_dir="$OPENRV_BUILD_DIR"
# Use native Python/CMake/MSVC before MSYS tools; never persist PATH into a user profile.
export PATH="$(dirname "$(cygpath -u "$OPENRV_BUILD_PYTHON")"):$(dirname "$(cygpath -u "$OPENRV_CMAKE")"):$PATH:/c/Strawberry/perl/bin:/c/ProgramData/Chocolatey/lib/jom/tools"
export ACLOCAL_PATH=/usr/share/aclocal
export VSLANG=1033 LANG=C LC_ALL=C
export NUMBER_OF_PROCESSORS="${ORV_JOBS:-2}"
export MSBUILDDISABLENODEREUSE=1
export RV_DISABLE_COMPILER_CACHE=1
# Reuse the pinned MSVC environment; setuptools re-running vcvars can exceed cmd PATH limits.
export DISTUTILS_USE_SDK=1 MSSdk=1
cmake_bin="$(cygpath -u "$OPENRV_CMAKE")"
python_bin="$(cygpath -u "$OPENRV_BUILD_PYTHON")"
qt_location="$(cygpath -m "$QT_ROOT_DIR")"
df -h .
"$python_bin" -c 'import pathlib, shutil, sys; p=pathlib.Path(sys.executable); q=p.with_name("python3.exe"); q.exists() or shutil.copy2(p,q)'
# Exercise the actual MSYS Git/native CMake boundary before configuring any cold dependencies.
export OPENRV_TEST_MSYS_GIT="$(cygpath -m /usr/bin/git.exe)"
export OPENRV_TEST_CMAKE="$(cygpath -m "$cmake_bin")"
export OPENRV_REQUIRE_MSYS_GIT_TESTS=1
"$python_bin" -B -m unittest discover -s ci/localized-release/tests -p test_msys_git_metadata.py -v
"$python_bin" ci/localized-release/prepare-windows.py
"$cmake_bin" -S . -B "$build_dir" -G 'Visual Studio 17 2022' -A x64 -T 'v143,version=14.40' \
  -DCMAKE_BUILD_TYPE=Release -DRV_VFX_PLATFORM=CY2025 -C ci/localized-release/decoder-profile.cmake \
  -DRV_DEPS_WIN_PERL_ROOT=C:/Strawberry/perl/bin \
  "-DRV_DEPS_QT_LOCATION=$qt_location" \
  "-DOPENRV_BUILD_PYTHON=$(cygpath -m "$OPENRV_BUILD_PYTHON")"
"$cmake_bin" --build "$build_dir" --config Release --target dependencies --parallel 2
"$cmake_bin" --build "$build_dir" --config Release --target main_executable --parallel "${ORV_JOBS:-2}"
"$cmake_bin" --install "$build_dir" --prefix "$(pwd -W)/_install_release" --config Release
df -h .
