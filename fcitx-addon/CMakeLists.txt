# SPDX-FileCopyrightText: 2026 HarryLoong
# SPDX-License-Identifier: MIT

cmake_minimum_required(VERSION 3.16)
project(voxkey_fcitx VERSION 0.2.0 LANGUAGES CXX)

include(GNUInstallDirs)
find_package(PkgConfig REQUIRED)
pkg_check_modules(FCITX5 REQUIRED IMPORTED_TARGET Fcitx5Core Fcitx5Utils)

add_library(voxkeyinput MODULE voxkeyinput.cpp)
target_compile_features(voxkeyinput PRIVATE cxx_std_20)
target_link_libraries(voxkeyinput PRIVATE PkgConfig::FCITX5)
set_target_properties(voxkeyinput PROPERTIES
    PREFIX "lib"
    OUTPUT_NAME "voxkeyinput"
)

install(TARGETS voxkeyinput
    LIBRARY DESTINATION "${CMAKE_INSTALL_LIBDIR}/fcitx5"
)
install(FILES voxkeyinput.conf
    DESTINATION "${CMAKE_INSTALL_DATADIR}/fcitx5/addon"
)
