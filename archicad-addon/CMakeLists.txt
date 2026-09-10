# Build for the Loriini Archicad add-on.
#
# Adapted from the dev kit's own AddOnCommandTest example, which is the
# authority for how an Archicad add-on links: the resource compiler step, the
# two exported ordinals, and the sweep over every module folder in the kit are
# all load-bearing and none of them are obvious.
#
# Two things differ from the example on purpose:
#
#   AC_API_DEVKIT_DIR has no default. The example points at its own parent
#   because it lives inside the kit; this add-on does not, and a wrong guess
#   there fails deep in a compile rather than here.
#
#   AC_ADDON_NAME carries the Archicad version. An .apx is compiled against
#   one major version and cannot be shared across them, so the version belongs
#   in the file name where a person copying it into an Add-Ons folder can see
#   it -- the same reason Tapir's own releases are named that way.
#
# Generated the way Tapir's CI generates its Archicad 26 build, which is the
# only combination proven to work for this kit:
#
#   cmake -B Build -G "Visual Studio 17 2022" -A x64 -T v142 \
#         -DAC_API_DEVKIT_DIR=<the folder containing Support> archicad-addon

cmake_minimum_required (VERSION 3.16)

function (SetCompilerOptions target)
	target_compile_features (${target} PUBLIC cxx_std_14)
	target_compile_options (${target} PUBLIC "$<$<CONFIG:Debug>:-DDEBUG>")
	if (WIN32)
		target_compile_options (${target} PUBLIC /W3 /WX /wd4996 /Zc:wchar_t-)
	endif ()
endfunction ()

function (SubDirList dirlist curdir)
	file (GLOB children RELATIVE "${curdir}" "${curdir}/*")
	set (result "")
	foreach (child ${children})
		if (IS_DIRECTORY "${curdir}/${child}")
			list (APPEND result ${child})
		endif ()
	endforeach ()
	set (${dirlist} ${result} PARENT_SCOPE)
endfunction ()

function (GetModuleNameFromModuleFolderName module folder)
	set (result "")
	if (${folder} MATCHES "^.*Lib$")
		string (REGEX REPLACE "^(.*)Lib$" "\\1" result ${folder})
	elseif (WIN32 AND ${folder} MATCHES "^Communication.*$")
		string (REGEX REPLACE "^Communication(.*)$" "Communication.\\1" result ${folder})
	else ()
		set (result ${folder})
	endif ()
	set (${module} ${result} PARENT_SCOPE)
endfunction ()

function (AddGSModule target folder)
	target_include_directories (${target} PUBLIC "${AC_API_DEVKIT_DIR}/Support/Modules/${folder}")
	set (module "")
	GetModuleNameFromModuleFolderName (module ${folder})
	set (ModuleLibPath "${AC_API_DEVKIT_DIR}/Support/Modules/${folder}/Win/${module}Imp.lib")
	if (EXISTS ${ModuleLibPath})
		target_link_libraries (${target} ${ModuleLibPath})
	endif ()
endfunction ()

set_property (GLOBAL PROPERTY USE_FOLDERS ON)

set (CMAKE_SUPPRESS_REGENERATION 1)
set (CMAKE_CONFIGURATION_TYPES Debug;Release;RelWithDebInfo)

set (AC_API_DEVKIT_DIR "" CACHE PATH "The folder holding the API Development Kit's Support directory.")
set (AC_VERSION 26 CACHE STRING "Archicad major version to build for.")
set (AC_ADDON_LANGUAGE "INT" CACHE STRING "Add-On language code.")
set (AC_ADDON_NAME "LoriiniAddOn_AC${AC_VERSION}_Win" CACHE STRING "Add-On name.")

if (NOT AC_API_DEVKIT_DIR)
	message (FATAL_ERROR "AC_API_DEVKIT_DIR is not set. Point it at the folder that contains the kit's Support directory.")
endif ()

# The kit states its own Archicad version, so the value passed in is checked
# against it rather than trusted. Building the 26 add-on against the 28 kit
# produces a file Archicad loads and then behaves strangely with, which is a
# far worse failure than a refusal here.
set (ACAPINC_FILE_LOCATION ${AC_API_DEVKIT_DIR}/Support/Inc/ACAPinc.h)
if (EXISTS ${ACAPINC_FILE_LOCATION})
	file (READ ${ACAPINC_FILE_LOCATION} ACAPIncContent)
	string (REGEX MATCHALL "#define[ \t]+ServerMainVers_([0-9][0-9])" VersionList ${ACAPIncContent})
	set (ARCHICAD_VERSION ${CMAKE_MATCH_1})
	message (STATUS "API DevKit at ${AC_API_DEVKIT_DIR} is for Archicad ${ARCHICAD_VERSION}")
	if (NOT ARCHICAD_VERSION STREQUAL AC_VERSION)
		message (FATAL_ERROR "AC_VERSION says ${AC_VERSION} but the kit is for Archicad ${ARCHICAD_VERSION}.")
	endif ()
else ()
	message (FATAL_ERROR "No ACAPinc.h under ${AC_API_DEVKIT_DIR}/Support/Inc; AC_API_DEVKIT_DIR is wrong.")
endif ()

add_definitions (-DUNICODE -D_UNICODE)
add_definitions (-DACExtension)

project (${AC_ADDON_NAME})

set (AddOnSourcesFolder ./Src)
set (AddOnResourcesFolder .)
set (ResourceObjectsDir ${CMAKE_BINARY_DIR}/ResourceObjects)

file (GLOB AddOnResourceFiles
	${AddOnResourcesFolder}/R${AC_ADDON_LANGUAGE}/*.grc
	${AddOnResourcesFolder}/RFIX/*.grc
	${AddOnResourcesFolder}/RFIX.win/*.rc2
)

source_group ("Resources" FILES ${AddOnResourceFiles})
add_custom_target (
	AddOnResources ALL
	DEPENDS "${ResourceObjectsDir}/AddOnResources.stamp"
	SOURCES ${AddOnResourceFiles}
)

get_filename_component (AddOnSourcesFolderAbsolute "${CMAKE_CURRENT_LIST_DIR}/${AddOnSourcesFolder}" ABSOLUTE)
get_filename_component (AddOnResourcesFolderAbsolute "${CMAKE_CURRENT_LIST_DIR}/${AddOnResourcesFolder}" ABSOLUTE)
get_filename_component (APIDevKitToolsFolderAbsolute "${AC_API_DEVKIT_DIR}/Support/Tools" ABSOLUTE)

add_custom_command (
	OUTPUT "${ResourceObjectsDir}/AddOnResources.stamp"
	DEPENDS ${AddOnResourceFiles}
	COMMENT "Compiling resources..."
	COMMAND ${CMAKE_COMMAND} -E make_directory "${ResourceObjectsDir}"
	COMMAND python "${APIDevKitToolsFolderAbsolute}/CompileResources.py" "${AC_ADDON_LANGUAGE}" "${AC_API_DEVKIT_DIR}" "${AddOnSourcesFolderAbsolute}" "${AddOnResourcesFolderAbsolute}" "${ResourceObjectsDir}" "${ResourceObjectsDir}/${AC_ADDON_NAME}.res"
	COMMAND ${CMAKE_COMMAND} -E touch "${ResourceObjectsDir}/AddOnResources.stamp"
)

file (GLOB AddOnHeaderFiles ${AddOnSourcesFolder}/*.h ${AddOnSourcesFolder}/*.hpp)
file (GLOB AddOnSourceFiles ${AddOnSourcesFolder}/*.c ${AddOnSourcesFolder}/*.cpp)
set (AddOnFiles ${AddOnHeaderFiles} ${AddOnSourceFiles})
source_group ("Sources" FILES ${AddOnFiles})

add_library (AddOn SHARED ${AddOnFiles})

set_target_properties (AddOn PROPERTIES OUTPUT_NAME ${AC_ADDON_NAME})
set_target_properties (AddOn PROPERTIES SUFFIX ".apx")

target_link_options (AddOn PUBLIC "${ResourceObjectsDir}/${AC_ADDON_NAME}.res")

# The two ordinals Archicad looks an add-on up by. Without them the .apx
# builds, loads, and is never called.
target_link_options (AddOn PUBLIC /export:GetExportedFuncAddrs,@1 /export:SetImportedFuncAddrs,@2)

target_include_directories (AddOn PUBLIC
	${AddOnSourcesFolder}
	${AC_API_DEVKIT_DIR}/Support/Inc
)

target_link_libraries (AddOn
	"$<$<CONFIG:Debug>:${AC_API_DEVKIT_DIR}/Support/Lib/Win/ACAP_STATD.lib>"
	"$<$<CONFIG:Release>:${AC_API_DEVKIT_DIR}/Support/Lib/Win/ACAP_STAT.lib>"
	"$<$<CONFIG:RelWithDebInfo>:${AC_API_DEVKIT_DIR}/Support/Lib/Win/ACAP_STAT.lib>"
)

SetCompilerOptions (AddOn)
add_dependencies (AddOn AddOnResources)

get_filename_component (APIDevKitModulesDir "${AC_API_DEVKIT_DIR}/Support/Modules" ABSOLUTE)
set (GSModules "")
SubDirList (GSModules "${APIDevKitModulesDir}")
foreach (gsModule ${GSModules})
	AddGSModule (AddOn ${gsModule})
endforeach ()
