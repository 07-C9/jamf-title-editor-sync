#!/bin/bash
# ABOUTME: Reads TE creds from SSM, prints each Title Editor title's live state.
# ABOUTME: Shows name/currentVersion/lastModified/enabled/patch count - never credentials.
set -euo pipefail
TE_URL="${TITLE_EDITOR_URL:?Set TITLE_EDITOR_URL, e.g. https://yourorg.appcatalog.jamfcloud.com}"
TITLE_IDS="${TITLE_IDS:?Set TITLE_IDS to a space-separated list of software title IDs, e.g. \"1 2 3\"}"
TE_USERNAME=$(aws ssm get-parameter --name /jamf-title-editor-sync/te-username --with-decryption --query Parameter.Value --output text)
TE_PASSWORD=$(aws ssm get-parameter --name /jamf-title-editor-sync/te-password --with-decryption --query Parameter.Value --output text)
TOKEN=$(curl -sf -X POST "${TE_URL}/v2/auth/tokens" -u "${TE_USERNAME}:${TE_PASSWORD}" -H "Content-Type: application/json" -d '{}' | python3 -c "import sys,json; print(json.load(sys.stdin)['token'])")
for ID in ${TITLE_IDS}; do
  curl -sf "${TE_URL}/v2/softwaretitles/${ID}" -H "Authorization: Bearer ${TOKEN}" -H "Accept: application/json" | python3 -c "
import sys, json
d = json.load(sys.stdin)
patches = d.get('patches', [])
newest = patches[0].get('version','?') if patches else 'none'
print(f\"title {d.get('softwareTitleId','?')}: {d.get('name','?')} | currentVersion={d.get('currentVersion','?')} | lastModified={d.get('lastModified','?')} | enabled={d.get('enabled','?')} | newestPatch={newest} | patchCount={len(patches)}\")
"
done
