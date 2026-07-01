#!/bin/zsh
# Extension Attribute: Outset Version
# Reports the installed version of Outset from the app bundle plist.
# Outset installs outside /Applications, so Jamf inventory cannot see it via recon.
# For use in Jamf Pro or Title Editor patch definitions.
#
plist="/usr/local/outset/Outset.app/Contents/Info.plist"
if [ -f "$plist" ]; then
    result=$(/usr/libexec/PlistBuddy -c "print CFBundleVersion" "$plist" 2>/dev/null)
    echo "<result>$result</result>"
else
    echo "<result></result>"
fi
