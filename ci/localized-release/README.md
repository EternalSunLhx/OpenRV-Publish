# Native bilingual official releases

This lightweight repository contains only community translations, owned code, explicit patches, tests and release tooling. It has independent history. Full ASWF OpenRV sources and submodules are fetched only into disposable build workspaces; they are never pushed into this repository.

| Package | Native runner | Toolchain |
| --- | --- | --- |
| Windows x64 Setup + portable ZIP | `windows-2022` | MSVC 14.40.33807 |
| macOS Intel app ZIP + DMG | `macos-15-intel` | Xcode 16.4 / x86_64 |
| macOS Apple Silicon app ZIP + DMG | `macos-15` | Xcode 26.3 / arm64 |

Builds select CY2025, Qt 6.5.3, CMake 3.31.6, Python 3.11 and Rust 1.100.0. Dependency hashes and external Action commits are pinned. Hosted images and package repositories may change, so bit-for-bit reproducibility is not claimed.

## Source composition

`ci/upstream-release-watch/localization-base.json` is a schema-2 manifest of reviewed source patches and owned-file mappings. All patch and resource bytes are read from the immutable maintenance Git snapshot and checked against their declared SHA-256 and file modes. The core patch contains only our changes to upstream files. The optional annotateBeta patch is omitted only when the entire upstream component is absent from the selected official tag; an ordinary failed hunk remains a hard error.

The source base is fetched from ASWF only when needed to reconstruct a patch. It is not a parent of this repository's main history. Owned resources include `localization/i18n_zh.ts`, the native files under `overlay/src/`, and selected CI/tools/tests. Unexpected collisions with upstream source are rejected; replacement mappings are explicit. Upstream version numbers and submodule pointers are preserved.

The separate `openrv-preferences-lifetime.patch` protects the document-owned preferences cache with `QPointer` and prevents shutdown from lazily creating auxiliary windows. Its versioned condition checks the complete three-file patch against the composed source: a complete reverse-apply check means the reviewed fix is already present and is skipped; otherwise the complete forward application must succeed. Partial fixes, conflicting changes and unfamiliar alternative fixes stop for source review. This does not infer a fix from an issue's closed state, a version number or a keyword. The source plan records and each platform rechecks the decision and input/output trees. Other patches retain their normal conflict checks. Once an upstream solution is reviewed, this patch and its condition can be retired together.

Preflight produces a schema-4 `official-tag-patch-workspace` plan with the exact official Release ID, original object/commit/tree, maintained snapshot, publication anchor, patch/resource input digest, generated localization delta and final source tree. Each platform must reconstruct and verify that same tree. The ordinary HEAD stays at the official source commit inside the temporary build checkout. Official refs live under `refs/openrv-upstream/tags/`.

## Publication tags and credentials

A release named `v4.1.0` uses an annotated tag whose object is a commit from this lightweight repository. Its canonical annotation records the original official release identity as text. A same-version rebuild keeps that publication anchor and records its newer maintenance snapshot separately. No official source object is pushed to this repository; GitHub's automatic source archive contains the release tools only.

Tag creation and publication use job-scoped `GITHUB_TOKEN` with Contents write permission. The monitor uses Actions write only to dispatch the main packaging workflow. `OPENRV_TAG_MIRROR_TOKEN` is no longer referenced or required. An existing annotation with conflicting official provenance or an unrelated maintenance anchor stops the run.

## Running and publishing

Open **官方版本中英文打包与发布 (Auto) (Manual)** in Actions, select `main`, and enter an explicit official stable `official_tag`. This input is required and cannot be empty. Same-tag rebuilds are supported. The hourly monitor selects the latest official stable release and supplies its exact tag automatically. It runs at minute23 and deduplicates builds from this lightweight main history.

The optional `replace_legacy_v410` input defaults to false. It permits only the reviewed v4.1.0 build35 migration, with its exact Release ID and fifteen asset IDs, names, sizes and hashes pinned in `legacy-v4.1.0-build35-assets.json`. The new product must include the compatible updater. All three native builds and all twelve uploaded public files must pass validation before the old files are removed. Preflight requires the intact legacy cohort; an interrupted migration must resume publication from the same original build artifacts with the same explicit input, rather than starting another native build. Other historical-format releases remain protected.

All packages use Release configuration. The product number remains `28 + GITHUB_RUN_NUMBER` to preserve update ordering after the old repository's build28. Workflow run titles keep the raw Actions number. A retry of the same run keeps its product number.

Platform assets are retained as Actions artifacts for14 days. Every formal build requires the actual native GUI, saved-language, decode, installation or bundle checks. The original assembler validates all fifteen internal files. Publication then creates twelve public files: clean version/platform names, packages and checksums, plus source provenance/patch/checksums. The three build manifests remain in Actions artifacts. Package bytes and internal build identity do not change during projection. A new release stays a draft until remote size/digest/state/URL and source checks pass. A hidden structured release-note marker binds the internal build number to all twelve public hashes; compatible updater source is required before publishing this format.

Publisher jobs are serialized across tags. A manual older version cannot replace the newer Latest release, and an older same-version build cannot replace a newer complete cohort. Same-build resumes require matching bytes. A verified higher build may replace a previous clean-format cohort; the marker updates only after all new files are verified, so the new updater rejects mixed in-flight files. Historical-format releases require the narrowly scoped migration described above. Release notes show platform downloads, actual macOS minimums and installation instructions; detailed native build proofs stay in Actions artifacts. Already-installed legacy updaters require one manual download to upgrade to the new public format.

The generated localization patch is stored in a deterministic ZIP, including an empty delta, so no zero-byte release asset is necessary. The accompanying source checksum file covers both the ZIP and the full source plan.

## Decoder profile and actual runtime checks

`decoder-profile.cmake` uses upstream's existing `RV_FFMPEG_NON_FREE_DECODERS_TO_ENABLE` option. It selects 19 native software decoder names: AAC variants, AC-3/E-AC-3, Bink video/audio, DNxHD, DV, ProRes/ProRes RAW, QTRLE, VP9, HEVC, MPEG-2 and Sorenson1/3. VP9's parser follows the explicit decoder selection. Encoder defaults and hardware/OS/third-party SDK variants are unchanged.

The release gate loads the actual bundled `libavcodec`, records its hash/version and probes every expected decoder. The FFmpeg 6/7 compatibility path excludes the FFmpeg 8-only `prores_raw` entry. Availability alone does not establish complete application handling for every source pixel format; ProRes RAW color processing in particular should be checked with representative media.

The eighth native smoke case uses a small self-generated H264 + AAC stereo MP4. It requires an actual64×48 video frame and approximately one second of48kHz PCM stereo output with a non-silent440Hz signal. Missing decoders, silent output and the reported `Unsupported codec_id '86018'` failure stop packaging. Fixture provenance is recorded in `tests/media/fixture-provenance.json`; the tool used to generate it is not bundled.

The other runtime checks cover the native version, embedded rv/PySide/numpy/OCIO, PNG→EXR→PNG and real English/Chinese GUI/menu/HUD behavior. Processes use bounded deadlines and installed-runtime paths rather than compiler tool directories. Windows language persistence uses three private-INI launches; macOS uses three native menu/preference launches only on disposable hosted runners, then restores the app's language key and verifies it in a fresh process. All release gates are mandatory.

When changing a local FFmpeg decoder configuration, clean that dependency's old objects before rebuilding; conditional objects from an earlier disabled-codec configuration can otherwise remain stale. CI caches checked source downloads only and compiles dependencies afresh.

## Installers, bundles and signing

Windows uses the pinned official Inno Setup7.1.1 compiler and the checked-in `OpenRV.iss`. It defaults to the Windows UI language, offers English/简体中文, installs for all users and includes the canonical redistributable MSVC CRT. The private `OpenRV.Publish.Media` ProgID and capabilities add Open With candidates without changing UserChoice, existing defaults or ShotGrid RV's `Applications\\rv.exe` registration.

An early hosted-only small-program fixture validates the real registration template before compiling dependencies. The complete installer later receives its own install/run/hash/MP4+EXR Shell enumeration/uninstall test on a disposable hosted Windows runner. That test refuses ordinary local execution and leaves defaults, other candidates and unrelated registrations intact.

The Qt runtime profile installs `qtquick3d` for the SpatialAudio framework shipped by Qt Multimedia. On macOS it omits only the optional PostgreSQL and ODBC Qt SQL driver files named in `qt_runtime_profile.py`; their vendor binaries reference external database client SDKs. On Windows it omits only the PostgreSQL driver whose external LIBPQ client is absent; ODBC and SQLite remain available. The early SDK scan uses the same exclusion list and rejects any remaining library that depends on an excluded driver. Packaging covers the two explicit physical plugin roots on each platform: the main Qt copy and the standalone PySide6 copy. It removes only the named regular files from those app-owned roots and records each relative path and original SHA-256 in the manifest. Unexpected alternate PySide6 layouts and redirected parents are rejected rather than searched or deleted broadly. The vendor SDK, QtSql, SQLite and NMEA stay intact. Final dependency checks remain mandatory.

Mac packaging bundles the non-system dependency closure with pinned Delocate0.13.0, preserves framework helpers/resources and license files, rejects architecture/path collisions, and checks every final Mach-O dependency. The highest minimum macOS version among packaged binaries is recorded and applied to Info.plist as required; the build target alone is not advertised as the minimum runtime OS.

The distributed Mac application is named `OpenRV.app`, with `OpenRV` as its bundle and display name; its internal executable and bundle identifier remain compatible. Final Mac code is signed inside-out with an ad-hoc signature and verified; `ditto` preserves bundle metadata and symlinks, and `hdiutil` creates/verifies the DMG. These builds are not Apple-notarized. Renaming or rebuilding alone does not remove Gatekeeper verification warnings. Windows installers are not publisher-signed. The runtime includes OpenRV/dependency license notices.

## Maintenance checks

### Optional post-package functional tests

The separate **已发布安装包功能测试 (Manual)** workflow downloads an existing stable release's portable Windows ZIP or macOS app ZIP and verifies its identity and SHA-256 before running it on disposable hosted runners. Select a tag, platform, language and case. `rotation-playback` opens the synthetic MP4, invokes the actual 180-degree rotation menu, observes changing playback frames, and closes normally. `preferences-owner-close` opens preferences owned by session A, creates B, closes A, verifies child destruction, then closes B. A crash, timeout or missing evidence fails the case.

This workflow is manually dispatched only. It never compiles, publishes or changes the packaging workflow's result. Its artifact contains package identity, case reports and bounded process logs, not the temporary preference directory. The small local Qt ownership regression under `tests/localization/pref_dialog_lifetime/` is also independent; its passing result is not evidence of full OpenRV or macOS execution.

Windows hosted tests reuse the scoped software OpenGL launcher with the reviewed Qt DLL size and hash, restored process settings and a provider outside the extracted runtime. Only that test archive is downloaded; the product is unchanged. This verifies functional behavior in the CI graphics environment, not a user's GPU or playback performance.

### Source and manifest checks

The preflight suite checks every formal macOS RV GUI script for direct successful Qt event-loop exits. The snapshot probe, both language smoke checks and all chooser-persistence launches must close their owned RV session normally. Callback tests cover close rejection and premature destruction; native packaging still requires clean process exits, and publication checks both languages' shutdown evidence. A passing Python assertion or success marker never overrides a native crash or timeout.

Edit translations in `localization/i18n_zh.ts`, owned native code under `overlay/src/`, and reviewed upstream changes under `patches/`. Refresh the schema-2 resource manifest after changing mapped files and before committing. The refresh tool reads Git-index blobs so a Windows checkout's line endings cannot change the recorded source identity.

```sh
git add <changed-owned-files>
python -B tools/refresh_patch_manifest.py
git add ci/upstream-release-watch/localization-base.json
python -B tools/refresh_patch_manifest.py --check
python -B -m unittest discover -s ci/localized-release/tests -p 'test_*.py' -v
python -B -m unittest discover -s ci/upstream-release-watch/tests -p test_dispatch.py -v
```

The independent installer registration and native language-dialog diagnostic workflows remain available. Historical graphics/mixed-Qt experiment workflows are retired. Their owned fixtures and the formal native release gates remain covered; the old bundler fixture is stored as a hash-checked owned file rather than retrieved from removed Git history.

Source extraction, patch application and translation checks are also required against the actual selected official tag. Unit checks alone do not establish that a native release succeeded.
