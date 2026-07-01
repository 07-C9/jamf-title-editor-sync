#!/bin/zsh
# Extension Attribute: utiluti Version
# Reports the installed version of the utiluti command-line tool.
# utiluti is a standalone binary with no app bundle, so the version comes from
# the tool itself; Jamf inventory cannot see it via recon.
# For use in Jamf Pro or Title Editor patch definitions.
#
binary="/usr/local/bin/utiluti"
if [ -x "$binary" ]; then
    result=$("$binary" --version 2>/dev/null)
    echo "<result>$result</result>"
else
    echo "<result></result>"
fi
