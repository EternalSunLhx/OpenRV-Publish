# Published-release update checks

When an RV session window is visible, the client checks the latest stable release from `EternalSunLhx/OpenRV-Publish` once per local calendar day. The last automatic attempt is saved per user across restarts. A session left open checks again the next day. A verified newer installer for the current platform opens the update dialog. Selecting **Later** dismisses the reminder for the day. Background errors stay silent.

**Help → Check for Updates** is available at any time and can display the result of an in-flight automatic request. Both paths use the same asynchronous HTTPS request, 15-second timeout, 2 MiB limit and installer URL validation. Downloads open in the browser when selected. Set `RV_DISABLE_AUTOMATIC_UPDATE_CHECK=1` to suppress background checks in a scripted GUI session.

Official releases use `vX.Y.Z` with an internal positive build counter. New publications use twelve clean filenames without `-build.N`, with platform build manifests retained in Actions artifacts. One single-line hidden `openrv-publication` JSON comment binds the exact tag, positive uint64 build-number string and all twelve SHA-256 digests. The parser requires the exact asset set, uploaded state, positive sizes, matching API digests and canonical repository/tag URLs before offering the compiled platform's installer. Missing companions, mixed cohorts, duplicate JSON members/assets and invalid markers are rejected; an invalid marker never falls back to the older layout.

When the marker is absent, the existing `-build.N` format remains supported: the highest complete uploaded installer, ZIP, `.build.json` and `.sha256.txt` group is selected. The original v4.1.0 build35 used that layout. Already distributed filename-only update checkers need a manual download of their first package using the new marker protocol, including a same-tag rebuild. Array length remains bounded at 4096 and the response at 2 MiB; no extra request or executable download is introduced.

The compiled identity separates the official version, native CMake version and `RV_LOCALIZED_BUILD_NUMBER`. Configuration uses `ORV_RELEASE_TAG=v4.0.2` and `ORV_BUILD_NUMBER=17`; the runtime displays `4.0.2+build.17`, while an official source tree may still declare native `4.0.0`.

Legacy `vX.Y.Z-zh.N` parsing remains for migration to the new binary. An already installed binary with the old parser needs one manual installation of a new-mode package. A development `4.1.0` build is not automatically downgraded to an official `4.0.2` release.

Build and run the offline native Qt fixture:

```sh
cmake -S tests/localization/update_checker -B work/update-checker \
  -DCMAKE_PREFIX_PATH=/path/to/Qt
cmake --build work/update-checker --config Release
ctest --test-dir work/update-checker -C Release --output-on-failure
```

Use separate build directories for the legacy fixture (`-DRV_TEST_RELEASE_TAG=v4.1.0-zh.2`) and official fixture (`-DRV_TEST_RELEASE_TAG=v4.0.2 -DRV_TEST_BUILD_NUMBER=17 -DRV_TEST_NATIVE_VERSION=4.0.0`). The injected fake network manager never accesses GitHub, downloads an executable or launches a browser. Cases cover version/build comparison, platform groups, partial uploads, spoofed metadata, bounds, cancellation and dialog lifetimes.

Metadata Git/CMake fixtures are exercised with:

```sh
python -B -m unittest discover -s tests/localization/update_checker -p test_build_metadata.py -v
```

Primary references: [GitHub stable release API](https://docs.github.com/en/rest/releases/releases#get-the-latest-release), [Qt asynchronous replies](https://doc.qt.io/qt-6/qnetworkreply.html).
