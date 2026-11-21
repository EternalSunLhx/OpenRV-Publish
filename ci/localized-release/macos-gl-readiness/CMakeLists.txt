cmake_minimum_required(VERSION 3.24)
project(OpenRvMacGraphicsDiagnostic LANGUAGES C CXX)
if(NOT APPLE)
  message(FATAL_ERROR "This diagnostic requires real Apple CGL and Cocoa")
endif()
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
find_package(Qt6 6.5.3 EXACT REQUIRED COMPONENTS Core Gui Widgets OpenGLWidgets)
get_filename_component(OPENRV_SOURCE_ROOT "${CMAKE_CURRENT_SOURCE_DIR}/../../.." ABSOLUTE)
set(OPENRV_GLEW_SOURCE "${OPENRV_SOURCE_ROOT}/src/pub/glew/glew.c")
file(SHA256 "${OPENRV_GLEW_SOURCE}" OPENRV_GLEW_SOURCE_SHA256)
set(OPENRV_CGL_POLICY "${OPENRV_SOURCE_ROOT}/src/lib/graphics/TwkGLFFBO/TwkGLFFBO/MacCGLContext.h")
file(SHA256 "${OPENRV_CGL_POLICY}" OPENRV_CGL_POLICY_SHA256)
find_library(OPENRV_OPENGL_FRAMEWORK OpenGL REQUIRED)
add_executable(openrv-macos-gl-readiness main.cpp "${OPENRV_GLEW_SOURCE}")
target_include_directories(openrv-macos-gl-readiness PRIVATE "${OPENRV_SOURCE_ROOT}/src/pub/glew"
  "${OPENRV_SOURCE_ROOT}/src/lib/graphics/TwkGLFFBO")
target_compile_definitions(openrv-macos-gl-readiness PRIVATE GLEW_STATIC
  "OPENRV_GLEW_SOURCE_SHA256=\"${OPENRV_GLEW_SOURCE_SHA256}\""
  "OPENRV_CGL_POLICY_SHA256=\"${OPENRV_CGL_POLICY_SHA256}\"")
target_link_libraries(openrv-macos-gl-readiness PRIVATE Qt6::Core Qt6::Gui Qt6::Widgets
  Qt6::OpenGLWidgets "${OPENRV_OPENGL_FRAMEWORK}")
