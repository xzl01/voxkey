# SPDX-FileCopyrightText: 2026 HarryLoong
# SPDX-License-Identifier: MIT

cmake_minimum_required(VERSION 3.16)
project(qwen_voice_input_fcitx VERSION 0.2.0 LANGUAGES CXX)

include(GNUInstallDirs)
find_package(PkgConfig REQUIRED)
pkg_check_modules(FCITX5 REQUIRED IMPORTED_TARGET Fcitx5Core Fcitx5Utils)

add_library(qwenvoiceinput MODULE qwenvoiceinput.cpp)
target_compile_features(qwenvoiceinput PRIVATE cxx_std_20)
target_link_libraries(qwenvoiceinput PRIVATE PkgConfig::FCITX5)
set_target_properties(qwenvoiceinput PROPERTIES
    PREFIX "lib"
    OUTPUT_NAME "qwenvoiceinput"
)

install(TARGETS qwenvoiceinput
    LIBRARY DESTINATION "${CMAKE_INSTALL_LIBDIR}/fcitx5"
)
install(FILES qwenvoiceinput.conf
    DESTINATION "${CMAKE_INSTALL_DATADIR}/fcitx5/addon"
)
