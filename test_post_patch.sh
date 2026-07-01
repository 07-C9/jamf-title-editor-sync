#!/bin/bash
# ABOUTME: Tests the POST endpoint for creating a new patch version in Title Editor.
# ABOUTME: Uses a real older Chrome version to validate the exact Lambda payload structure.

TE_URL="https://YOURORG.appcatalog.jamfcloud.com"
TE_USERNAME="CHANGE_ME"
TE_PASSWORD="CHANGE_ME"
TITLE_ID="1"
TEST_VERSION="148.0.7778.216"

echo "=== Authenticating ==="
TOKEN=$(/usr/bin/curl -sf -X POST "${TE_URL}/v2/auth/tokens" \
  -u "${TE_USERNAME}:${TE_PASSWORD}" \
  -H "Content-Type: application/json" \
  -d '{}' | python3 -c "import sys,json; print(json.load(sys.stdin)['token'])" 2>/dev/null)

if [[ -z "$TOKEN" ]]; then
    echo "FAILED - could not get token"
    exit 1
fi
echo "OK"

echo ""
echo "=== Current state before POST ==="
BEFORE=$(/usr/bin/curl -sf "${TE_URL}/v2/softwaretitles/${TITLE_ID}" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Accept: application/json")

PATCH_COUNT_BEFORE=$(echo "$BEFORE" | python3 -c "import sys,json; print(len(json.load(sys.stdin).get('patches',[])))")
CURRENT_VER=$(echo "$BEFORE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('currentVersion','?'))")
echo "Current version: $CURRENT_VER"
echo "Patch count: $PATCH_COUNT_BEFORE"

echo ""
echo "=== POSTing test patch version ${TEST_VERSION} ==="
echo "This is the EXACT payload structure the Lambda uses."

PATCH_BODY=$(python3 -c "
import json
body = {
    'version': '${TEST_VERSION}',
    'releaseDate': '2026-05-27T00:00:00Z',
    'enabled': True,
    'standalone': True,
    'reboot': False,
    'minimumOperatingSystem': '12.0',
    'killApps': [],
    'components': [{
        'name': 'Google Chrome',
        'version': '${TEST_VERSION}',
        'criteria': [
            {'name': 'Application Bundle ID', 'operator': 'is', 'value': 'com.google.Chrome', 'type': 'recon'},
            {'name': 'Application Version', 'operator': 'is', 'value': '${TEST_VERSION}', 'type': 'recon'}
        ]
    }],
    'capabilities': [{
        'name': 'Operating System Version',
        'operator': 'greater than',
        'value': '12.0',
        'type': 'recon'
    }]
}
print(json.dumps(body))
")

POST_RESULT=$(/usr/bin/curl -s -w "\n%{http_code}" -X POST \
  "${TE_URL}/v2/softwaretitles/${TITLE_ID}/patches" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  -d "${PATCH_BODY}")

HTTP_CODE=$(echo "$POST_RESULT" | tail -1)
RESPONSE_BODY=$(echo "$POST_RESULT" | sed '$d')

echo "HTTP status: ${HTTP_CODE}"

if [[ "$HTTP_CODE" == "201" || "$HTTP_CODE" == "200" ]]; then
    echo "POST SUCCEEDED"
    echo "$RESPONSE_BODY" | python3 -m json.tool 2>/dev/null || echo "$RESPONSE_BODY"
elif [[ "$HTTP_CODE" == "409" ]]; then
    echo "Version already exists (409 Conflict) - idempotency working"
elif [[ "$HTTP_CODE" == "400" ]] && echo "$RESPONSE_BODY" | grep -q "DUPLICATE_RECORD"; then
    echo "Version already exists (400 DUPLICATE_RECORD) - idempotency working"
else
    echo "POST FAILED"
    echo "$RESPONSE_BODY"
    exit 1
fi

echo ""
echo "=== State after POST ==="
AFTER=$(/usr/bin/curl -sf "${TE_URL}/v2/softwaretitles/${TITLE_ID}" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Accept: application/json")

PATCH_COUNT_AFTER=$(echo "$AFTER" | python3 -c "import sys,json; print(len(json.load(sys.stdin).get('patches',[])))")
CURRENT_VER_AFTER=$(echo "$AFTER" | python3 -c "import sys,json; print(json.load(sys.stdin).get('currentVersion','?'))")
echo "Current version: $CURRENT_VER_AFTER (should still be ${CURRENT_VER} - POST does not change this)"
echo "Patch count: $PATCH_COUNT_AFTER (was $PATCH_COUNT_BEFORE)"

echo ""
echo "=== All patch versions ==="
echo "$AFTER" | python3 -c "
import sys,json
d = json.load(sys.stdin)
for p in d.get('patches',[]):
    print(f\"  {p['version']} (enabled: {p.get('enabled')}, order: {p.get('absoluteOrderId')})\")
"

echo ""
if [[ "$CURRENT_VER_AFTER" == "$CURRENT_VER" ]]; then
    echo "SAFE: currentVersion unchanged. POST did not affect the active version."
else
    echo "WARNING: currentVersion changed unexpectedly!"
fi
