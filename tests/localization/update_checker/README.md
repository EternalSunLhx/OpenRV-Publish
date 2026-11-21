# Published-release update checks

The Help action sends one unauthenticated HTTPS request after an explicit click. It reads stable releases from `EternalSunLhx/OpenRV-Publish`; it is asynchronous, cancellable, limited to 15 seconds / 2 MiB, and reuses an existing dialog/request. It opens validated browser download URLs and does not install or execute the download.

Official releases use `vX.Y.Z` with an internal positive `build.N`. For the compiled Windows/macOS architecture, the parser selects the highest build with a complete uploaded installer, ZIP, `.build.json` and `.sha256.txt` group. Every asset must have a positive size and an exact matching repository/tag URL. Partial uploads, cross-tag files, malformed counters and older builds cannot be selected. Array length is additionally bounded at4096; the response-byte limit remains unchanged.

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
