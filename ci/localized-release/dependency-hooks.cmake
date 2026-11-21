# Opt-in hooks for the Windows source dependency builds. Do not alter global tools.
# prepare-windows.py includes this at the end of cmake/dependencies/CMakeLists.txt.
# ExternalProject steps must be added in the same CMake directory as their targets.
set(OPENRV_RELEASE_FIX_SCRIPT "${CMAKE_CURRENT_LIST_DIR}/fix_dependency.py")
function(openrv_add_release_dependency_hooks)
  if(NOT WIN32)
    return()
  endif()
  if(NOT EXISTS "${OPENRV_BUILD_PYTHON}")
    message(FATAL_ERROR "OPENRV_BUILD_PYTHON must identify the native build Python interpreter")
  endif()
  if(TARGET RV_DEPS_FFMPEG)
    ExternalProject_Add_Step(RV_DEPS_FFMPEG localized_msvc_detection
      COMMAND "${OPENRV_BUILD_PYTHON}" "${OPENRV_RELEASE_FIX_SCRIPT}" ffmpeg <SOURCE_DIR>
      DEPENDEES patch DEPENDERS configure)
  endif()
  if(TARGET RV_DEPS_OPENSSL)
    ExternalProject_Add_Step(RV_DEPS_OPENSSL normalized_pkgconfig
      COMMAND "${OPENRV_BUILD_PYTHON}" "${OPENRV_RELEASE_FIX_SCRIPT}" openssl <SOURCE_DIR> <INSTALL_DIR>
      DEPENDEES install)
  endif()
  if(TARGET RV_DEPS_BOOST)
    ExternalProject_Add_Step(RV_DEPS_BOOST selected_msvc_toolset
      COMMAND "${OPENRV_BUILD_PYTHON}" "${OPENRV_RELEASE_FIX_SCRIPT}" boost <SOURCE_DIR>
      DEPENDEES configure DEPENDERS build)
  endif()
endfunction()
openrv_add_release_dependency_hooks()
