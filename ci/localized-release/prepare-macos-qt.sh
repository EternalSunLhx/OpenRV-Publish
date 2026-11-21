#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Xcode 26 removed AGL. Use OpenRV's existing upstream Qt 6.5.3 patch:
# https://github.com/AcademySoftwareFoundation/OpenRV/blob/v4.0.2/qt_fix.patch
# https://github.com/qt/qtbase/commit/cdb33c3d5621ce035ad6950c8e2268fe94b73de5
# This script accepts the SDK leaf directory exported as QT_ROOT_DIR, not the
# parent-of-version QT_HOME expected by upstream apply_qt_fix.sh. Missing paths,
# incompatible patch contexts and incomplete fixes must stop the build.
set -euo pipefail

if [ "$#" -ne 2 ]; then
  echo 'Usage: prepare-macos-qt.sh QT_ROOT_DIR XCODE_VERSION' >&2
  exit 1
fi
qt_root=$(cd "$1" && pwd -P)
xcode_version=$2
case "$xcode_version" in
  16.4|26.3) ;;
  *) echo "Unsupported pinned Xcode version: $xcode_version" >&2; exit 1 ;;
esac

find_module="$qt_root/lib/cmake/Qt6/FindWrapOpenGL.cmake"
mac_conf="$qt_root/mkspecs/common/mac.conf"
qconfig="$qt_root/mkspecs/qconfig.pri"
for config in "$find_module" "$mac_conf" "$qconfig"; do
  if [ ! -f "$config" ]; then
    echo "Required Qt SDK file is missing: $config" >&2
    exit 1
  fi
done
if ! grep -Eq '^[[:space:]]*QT_VERSION[[:space:]]*=[[:space:]]*6[.]5[.]3[[:space:]]*$' "$qconfig"; then
  echo 'The localized build requires the Qt 6.5.3 SDK.' >&2
  exit 1
fi

if [ "$xcode_version" = 16.4 ]; then
  echo 'Qt 6.5.3 SDK verified; Xcode 16.4 retains its original configuration.'
  exit 0
fi

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
patch_file="$script_dir/../../qt_fix.patch"
if [ ! -f "$patch_file" ]; then
  echo "Official OpenRV Qt patch is missing: $patch_file" >&2
  exit 1
fi

verify_fixed() {
  ! grep -Eq 'AGL|__opengl_agl' "$find_module" "$mac_conf" &&
    grep -Fq 'target_link_libraries(WrapOpenGL::WrapOpenGL INTERFACE ${__opengl_fw_path})' "$find_module" &&
    grep -Eq '^QMAKE_LIBS_OPENGL[[:space:]]*=[[:space:]]*-framework[[:space:]]+OpenGL[[:space:]]*$' "$mac_conf"
}

if verify_fixed; then
  echo 'Qt 6.5.3 Xcode 26 AGL fix is already applied and verified.'
  exit 0
fi

# Check the entire official patch before writing either file. Never reverse an
# already applied hunk or accept fuzzy/incomplete matches.
patch -p1 -d "$qt_root" --dry-run --batch --forward -F 0 -i "$patch_file"
patch -p1 -d "$qt_root" --batch --forward -F 0 -i "$patch_file"
if ! verify_fixed; then
  echo 'Qt AGL fix failed its postcondition; refusing to configure any Qt target.' >&2
  exit 1
fi
echo 'Qt 6.5.3 Xcode 26 AGL fix applied and verified.'
