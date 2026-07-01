#!/bin/zsh
# Extension Attribute: MacAdmins Python Version
# Reports the installed version of MacAdmins Python from the framework plist.
# For use in Jamf Pro or Title Editor patch definitions.
#
plist="/Library/ManagedFrameworks/Python/Python3.framework/Versions/Current/Resources/Info.plist"
if [ -f "$plist" ]; then
    result=$(/usr/libexec/PlistBuddy -c "print CFBundleVersion" "$plist" 2>/dev/null)
    echo "<result>$result</result>"
else
    echo "<result></result>"
fi
