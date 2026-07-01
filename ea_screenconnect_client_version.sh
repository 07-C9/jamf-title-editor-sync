#!/bin/zsh
# Extension Attribute: ScreenConnect Client Version
# Reports the installed version of the ConnectWise ScreenConnect client.
# Uses a wildcard match because the app name includes an instance-specific hash.
#
app_path=$(find /Applications -maxdepth 1 -name "connectwisecontrol-*.app" -print -quit 2>/dev/null)
if [ -n "$app_path" ]; then
    result=$(/usr/libexec/PlistBuddy -c "print CFBundleVersion" "$app_path/Contents/Info.plist" 2>/dev/null)
    echo "<result>$result</result>"
else
    echo "<result></result>"
fi
