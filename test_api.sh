#!/bin/bash
# ABOUTME: Quick test script to verify Title Editor API auth and title setup.
# ABOUTME: Edit the variables below, then run: bash test_api.sh

TE_URL="https://YOURORG.appcatalog.jamfcloud.com"
TE_USERNAME="CHANGE_ME"
TE_PASSWORD="CHANGE_ME"
TITLE_ID="CHANGE_ME"
CURRENT_VERSION="CHANGE_ME"

echo "=== Step 1: Authenticate ==="
TOKEN=$(curl -sf -X POST "${TE_URL}/v2/auth/tokens" \
  -u "${TE_USERNAME}:${TE_PASSWORD}" \
  -H "Content-Type: application/json" \
  -d '{}' | python3 -c "import sys,json; print(json.load(sys.stdin)['token'])" 2>/dev/null)

if [[ -z "$TOKEN" ]]; then
    echo "FAILED - could not get token. Check username/password."
    exit 1
fi
echo "OK - got bearer token"

echo ""
echo "=== Step 2: Get Chrome title ==="
TITLE=$(curl -sf "${TE_URL}/v2/softwaretitles/${TITLE_ID}" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Accept: application/json")

if [[ -z "$TITLE" ]]; then
    echo "FAILED - could not fetch title ID ${TITLE_ID}"
    exit 1
fi

echo "$TITLE" | python3 -c "
import sys,json
d = json.load(sys.stdin)
print(f\"Name: {d.get('name','?')}\")
print(f\"Publisher: {d.get('publisher','?')}\")
print(f\"Current Version: {d.get('currentVersion','?')}\")
print(f\"Enabled: {d.get('enabled','?')}\")
print(f\"ID: {d.get('id','?')}\")
"

echo ""
echo "=== Step 3: Patches (from title response) ==="
echo "$TITLE" | python3 -c "
import sys,json
d = json.load(sys.stdin)
patches = d.get('patches', [])
print(f'Patch count: {len(patches)}')
for p in patches:
    v = p.get('version','?')
    e = p.get('enabled', '?')
    comps = len(p.get('components', []))
    caps = len(p.get('capabilities', []))
    kills = len(p.get('killApps', []))
    print(f'  {v} (enabled: {e}, components: {comps}, capabilities: {caps}, killApps: {kills})')
"

echo ""
echo "=== Step 4: Enable title if disabled ==="
ENABLED=$(echo "$TITLE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('enabled', False))")
if [[ "$ENABLED" != "True" ]]; then
    echo "Title is disabled, enabling..."
    ENABLE_RESULT=$(curl -sf -X PUT "${TE_URL}/v2/softwaretitles/${TITLE_ID}" \
      -H "Authorization: Bearer ${TOKEN}" \
      -H "Content-Type: application/json" \
      -d "{\"enabled\": true, \"currentVersion\": \"${CURRENT_VERSION}\"}")
    NEW_ENABLED=$(echo "$ENABLE_RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('enabled', 'FAILED'))" 2>/dev/null)
    echo "Result: enabled=$NEW_ENABLED"
else
    echo "Title is already enabled"
fi

echo ""
echo "=== Step 5: Full title JSON (for debugging) ==="
TITLE_FINAL=$(curl -sf "${TE_URL}/v2/softwaretitles/${TITLE_ID}" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Accept: application/json")
echo "$TITLE_FINAL" | python3 -m json.tool
