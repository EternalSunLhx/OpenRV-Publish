# SPDX-License-Identifier: Apache-2.0
# Edition metadata for the in-application updater. No cached banner hash is used.
FUNCTION(RV_CONFIGURE_LOCALIZED_BUILD REPOSITORY OUTPUT_DIRECTORY CORE_VERSION)
  SET(_rv_localized_build_number 0)
  IF(NOT CORE_VERSION MATCHES "^(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)$")
    MESSAGE(FATAL_ERROR "The localized core version must contain three numeric components")
  ENDIF()
  SET(RV_MAJOR_VERSION "${CMAKE_MATCH_1}")
  SET(RV_MINOR_VERSION "${CMAKE_MATCH_2}")
  SET(RV_REVISION_NUMBER "${CMAKE_MATCH_3}")
  FOREACH(_part RV_MAJOR_VERSION RV_MINOR_VERSION RV_REVISION_NUMBER)
    IF(${_part} GREATER 65535)
      MESSAGE(FATAL_ERROR "The localized core version exceeds the installer version range")
    ENDIF()
  ENDFOREACH()
  FIND_PROGRAM(_rv_update_git git NO_CACHE REQUIRED)
  EXECUTE_PROCESS(
    COMMAND "${_rv_update_git}" rev-parse HEAD
    WORKING_DIRECTORY "${REPOSITORY}"
    OUTPUT_VARIABLE _rv_update_head OUTPUT_STRIP_TRAILING_WHITESPACE COMMAND_ERROR_IS_FATAL ANY
  )
  STRING(LENGTH "${_rv_update_head}" _head_length)
  IF(NOT _head_length EQUAL 40 OR NOT _rv_update_head MATCHES "^[0-9a-f]+$")
    MESSAGE(FATAL_ERROR "The localized build requires a full Git commit SHA")
  ENDIF()
  STRING(SUBSTRING "${_rv_update_head}" 0 8 _rv_update_short_head)
  SET(_rv_localized_tag "$ENV{ORV_RELEASE_TAG}")
  SET(_rv_localized_version "${CORE_VERSION}-dev.${_rv_update_short_head}")
  IF(NOT _rv_localized_tag STREQUAL "")
    STRING(STRIP "${_rv_localized_tag}" _trimmed_tag)
    IF(NOT _rv_localized_tag STREQUAL _trimmed_tag
       OR NOT _rv_localized_tag MATCHES "^v([0-9]+)\\.([0-9]+)\\.([0-9]+)(-zh\\.([1-9][0-9]*))?$")
      MESSAGE(FATAL_ERROR "ORV_RELEASE_TAG must be a canonical official vX.Y.Z tag")
    ENDIF()
    SET(_tag_major "${CMAKE_MATCH_1}")
    SET(_tag_minor "${CMAKE_MATCH_2}")
    SET(_tag_patch "${CMAKE_MATCH_3}")
    SET(_edition "${CMAKE_MATCH_5}")
    SET(_official FALSE)
    IF(_edition STREQUAL "")
      SET(_official TRUE)
      SET(_rv_localized_build_number "$ENV{ORV_BUILD_NUMBER}")
      STRING(LENGTH "${_rv_localized_build_number}" _build_length)
      IF(NOT _rv_localized_build_number MATCHES "^[1-9][0-9]*$"
         OR _build_length GREATER 20
         OR (_build_length EQUAL 20 AND _rv_localized_build_number STRGREATER "18446744073709551615"))
        MESSAGE(FATAL_ERROR "An official release requires a positive uint64 ORV_BUILD_NUMBER")
      ENDIF()
    ENDIF()
    FOREACH(_part _tag_major _tag_minor _tag_patch)
      IF(NOT "${${_part}}" MATCHES "^(0|[1-9][0-9]*)$" OR ${_part} GREATER 65535)
        MESSAGE(FATAL_ERROR "ORV_RELEASE_TAG version is not canonical or exceeds the installer range")
      ENDIF()
    ENDFOREACH()
    SET(_release_core "${_tag_major}.${_tag_minor}.${_tag_patch}")
    IF(_official OR NOT _release_core STREQUAL CORE_VERSION)
      # Official 4.0.x tags can retain CMake 4.0.0. Accept that difference only
      # after reconstructing the exact official source + reviewed feature patch.
      FIND_PROGRAM(_rv_metadata_python NAMES python3 python NO_CACHE REQUIRED)
      EXECUTE_PROCESS(
        COMMAND "${CMAKE_COMMAND}" -E env --unset=GH_TOKEN --unset=GITHUB_TOKEN
          "${_rv_metadata_python}" -B "${REPOSITORY}/ci/localized-release/official_release.py"
          --repo "${REPOSITORY}" --tag "${_rv_localized_tag}" --native-version "${CORE_VERSION}" --version-only
        WORKING_DIRECTORY "${REPOSITORY}"
        RESULT_VARIABLE _identity_result OUTPUT_VARIABLE _identity_version ERROR_VARIABLE _identity_error
        OUTPUT_STRIP_TRAILING_WHITESPACE
      )
      IF(NOT _identity_result EQUAL 0 OR NOT _identity_version STREQUAL _release_core)
        MESSAGE(FATAL_ERROR "Release/core version difference lacks verified official provenance: ${_identity_error}")
      ENDIF()
    ENDIF()
    STRING(LENGTH "${_edition}" _edition_length)
    IF(_edition_length GREATER 20 OR (_edition_length EQUAL 20 AND _edition STRGREATER "18446744073709551615"))
      MESSAGE(FATAL_ERROR "ORV_RELEASE_TAG edition exceeds the updater's integer range")
    ENDIF()
    # A published vX.Y.Z tag belongs to the lightweight maintenance repository.
    # Only the private upstream reference identifies the source being compiled.
    SET(_rv_source_ref "refs/tags/${_rv_localized_tag}")
    IF(_official)
      SET(_rv_source_ref "refs/openrv-upstream/tags/${_rv_localized_tag}")
    ENDIF()
    EXECUTE_PROCESS(
      COMMAND "${_rv_update_git}" rev-list --max-count=1 "${_rv_source_ref}"
      WORKING_DIRECTORY "${REPOSITORY}"
      OUTPUT_VARIABLE _rv_update_tag_head OUTPUT_STRIP_TRAILING_WHITESPACE COMMAND_ERROR_IS_FATAL ANY
    )
    IF(NOT _rv_update_tag_head STREQUAL _rv_update_head)
      MESSAGE(FATAL_ERROR "ORV_RELEASE_TAG does not identify the exact checkout being built")
    ENDIF()
    STRING(SUBSTRING "${_rv_localized_tag}" 1 -1 _rv_localized_version)
    IF(_official)
      SET(_rv_localized_version "${_release_core}+build.${_rv_localized_build_number}")
    ENDIF()
    # Updater comparisons use the official release version. CORE_VERSION remains
    # the untouched source-native version and is recorded separately.
    SET(RV_MAJOR_VERSION "${_tag_major}")
    SET(RV_MINOR_VERSION "${_tag_minor}")
    SET(RV_REVISION_NUMBER "${_tag_patch}")
  ENDIF()
  FILE(MAKE_DIRECTORY "${OUTPUT_DIRECTORY}")
  CONFIGURE_FILE("${CMAKE_CURRENT_FUNCTION_LIST_DIR}/RvCommon/RvLocalizedBuild.h.in" "${OUTPUT_DIRECTORY}/RvLocalizedBuild.h" @ONLY)
ENDFUNCTION()
