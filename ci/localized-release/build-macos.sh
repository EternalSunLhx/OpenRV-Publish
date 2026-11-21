#!/usr/bin/env bash
set -euo pipefail
test "${GITHUB_ACTIONS:-}" = true
test "$(uname -m)" = "$ORV_ARCH"
export RV_DISABLE_COMPILER_CACHE=1
export NUMBER_OF_PROCESSORS="${ORV_JOBS:-2}"
export QT_HOME="$QT_ROOT_DIR"
xcodebuild -version
cmake --version
python3 --version
df -h .
cmake -S . -B _build_release -G Ninja -DCMAKE_BUILD_TYPE=Release \
  -DRV_VFX_PLATFORM=CY2025 -C ci/localized-release/decoder-profile.cmake "-DRV_DEPS_QT_LOCATION=$QT_ROOT_DIR" \
  "-DCMAKE_OSX_ARCHITECTURES=$ORV_ARCH"
cmake --build _build_release --config Release --target dependencies --parallel 2
cmake --build _build_release --config Release --target main_executable --parallel "${ORV_JOBS:-2}"
cmake --install _build_release --prefix "$PWD/_install_release" --config Release
df -h .
