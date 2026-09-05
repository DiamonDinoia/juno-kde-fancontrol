# SPDX-License-Identifier: BSD-2-Clause
cmake_minimum_required(VERSION 3.16)
project(junoprobe)

find_package(ECM REQUIRED NO_MODULE)
set(CMAKE_MODULE_PATH ${ECM_MODULE_PATH})
include(KDEInstallDirs)
include(KDECMakeSettings)
include(KDECompilerSettings NO_POLICY_SCOPE)

find_package(Qt6 REQUIRED COMPONENTS Core DBus Gui)
find_package(KF6 REQUIRED COMPONENTS CoreAddons I18n)
find_package(KSysGuard REQUIRED)

add_executable(junoprobe probe.cpp)
target_link_libraries(junoprobe Qt6::Core Qt6::DBus Qt6::Gui KF6::CoreAddons KF6::I18n KSysGuard::SystemStats)
