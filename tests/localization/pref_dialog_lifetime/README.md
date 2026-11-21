# Preference dialog ownership regression

This optional standalone Qt test extracts the actual patched `prefDialog()` getter, cached member and shutdown helper from a materialized OpenRV source directory. It verifies that deleting document A destroys its preferences, clears the application cache, and lets document B create fresh preferences. It also checks that shutdown creates no unopened auxiliary windows and preserves the existing network shutdown/close ordering.

The widgets and parent destruction are real Qt. Only RV's Session context and unrelated auxiliary dialog classes are minimal fixture types; this is not a full OpenRV playback or macOS product test. It does not add a mandatory packaging step.

```sh
cmake -S tests/localization/pref_dialog_lifetime -B work/pref-lifetime \
  -DCMAKE_PREFIX_PATH=/path/to/Qt -DOPENRV_LIFETIME_SOURCE=/path/to/patched/OpenRV
cmake --build work/pref-lifetime --config Release
ctest --test-dir work/pref-lifetime -C Release --output-on-failure
```

For a negative control, configure a separate build against the actual pre-fix source with `-DOPENRV_LIFETIME_LEGACY_NEGATIVE=ON`. Its ownership test must fail at the stale-cache assertion; it deliberately never dereferences the dangling pointer.
