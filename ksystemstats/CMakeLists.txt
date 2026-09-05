# SPDX-License-Identifier: BSD-2-Clause
cmake_minimum_required(VERSION 3.16)
project(junoplugin)

find_package(ECM REQUIRED NO_MODULE)
set(CMAKE_MODULE_PATH ${ECM_MODULE_PATH})
include(KDEInstallDirs)
include(KDECMakeSettings)
include(KDECompilerSettings NO_POLICY_SCOPE)

find_package(Qt6 REQUIRED COMPONENTS Core DBus Gui)
find_package(KF6 REQUIRED COMPONENTS CoreAddons I18n)
find_package(KSysGuard REQUIRED)

add_library(ksystemstats_plugin_juno MODULE junoplugin.cpp)
target_link_libraries(ksystemstats_plugin_juno Qt6::Core Qt6::DBus Qt6::Gui KF6::CoreAddons KF6::I18n KSysGuard::SystemStats)

# The daemon enumerates plugins from QLibraryInfo::PluginsPath/ksystemstats,
# which on Debian Qt6 is lib/<triplet>/qt6/plugins — ECM's QTPLUGINDIR variable
# resolves without the qt6 element here, so name the destination ourselves.
install(TARGETS ksystemstats_plugin_juno DESTINATION lib/${CMAKE_LIBRARY_ARCHITECTURE}/qt6/plugins/ksystemstats)
