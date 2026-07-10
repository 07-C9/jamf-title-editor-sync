# jamf-title-editor-sync

AWS Lambda that keeps Jamf Title Editor patch definitions current with vendor releases. Also handles apps Jamf has no built-in definition for.

## Why

Jamf's built-in patch definitions lag behind real releases. On June 4, 2026, Chrome 149.0.7827.54 had been out for two days while Jamf's built-in definition still showed 148.0.7778.216. Smart Groups scoping patch policies on that stale version target the wrong machines. Chrome ships a new stable release every week or two, and the definitions don't keep up.

Separately, Jamf has no built-in definition for many apps. If you deploy GMetrix or other niche software, there's no version tracking out of the box. As long as you can find a URL that exposes the current version number, this Lambda can create and maintain the patch definition.

## How it works

An AWS Lambda runs twice a day (6 AM and 6 PM Pacific by default). For each app in your config, it:

1. Checks the vendor's version API for the latest release
2. Compares that against what Title Editor currently has
3. If they differ, it adds the new version and sets it as current
4. Jamf Pro picks up the change within about 30 minutes

Each app has its own version source URL, version format regex, and patch template defined in `apps.json`. Adding a new app is a config change.

```
EventBridge Scheduler (twice daily)
  |
  v
Lambda (Python 3.13, ~320 lines)
  |
  +--- Vendor version API (per-app, configured in apps.json)
  +--- SSM Parameter Store (fetch Title Editor credentials)
  +--- POST Title Editor /v2/auth/tokens (authenticate)
  +--- GET  /v2/softwaretitles/{id} (current version)
  +--- If versions differ:
         POST /v2/softwaretitles/{id}/patches (add version)
         PUT  /v2/softwaretitles/{id} (set currentVersion)
  +--- After the app loop: download-URL canary GETs each constructed installer URL
```

Credentials are stored in AWS SSM Parameter Store (encrypted). Title Editor itself is the state store, so the Lambda is stateless.

## Google Chrome example

The included `apps.json` comes pre-configured for Google Chrome.

Google publishes Chrome release data through their VersionHistory API. The API returns releases at various rollout stages, so a version might only be available to 50% of users. The config filters for `fraction=1` (100% rollout) so machines don't get flagged as outdated before they can actually download the update. The tradeoff is that a new version shows up a few days late.

The version source URL:

```
https://versionhistory.googleapis.com/v1/chrome/platforms/mac/channels/stable/versions/all/releases?filter=fraction=1
```

The Lambda reads `releases[0].version` from the JSON response, which gives the latest Chrome stable version at full rollout.

The full `apps.json` config entry:

```json
{
  "name": "Google Chrome",
  "enabled": true,
  "title_id_env_var": "TITLE_EDITOR_TITLE_ID",
  "version_source": {
    "type": "google_versionhistory",
    "url": "https://versionhistory.googleapis.com/v1/chrome/platforms/mac/channels/stable/versions/all/releases?filter=fraction=1"
  },
  "version_pattern": "^\\d+\\.\\d+\\.\\d+\\.\\d+$",
  "patch_template": {
    "enabled": true,
    "standalone": true,
    "reboot": false,
    "minimumOperatingSystem": "12.0",
    "killApps": [],
    "components": [
      {
        "name": "Google Chrome",
        "version": "{version}",
        "criteria": [
          { "name": "Application Bundle ID", "operator": "is", "value": "com.google.Chrome", "type": "recon" },
          { "name": "Application Version", "operator": "is", "value": "{version}", "type": "recon" }
        ]
      }
    ],
    "capabilities": [
      { "name": "Operating System Version", "operator": "greater than", "value": "12.0", "type": "recon" }
    ]
  }
}
```

`{version}` placeholders get replaced at runtime with the version from Google's API. Everything else stays the same across versions.

## GMetrix SMSe example (no built-in Jamf definition)

GMetrix SMSe is a testing platform common in K-12 and higher ed. Jamf has no built-in patch definition for it, so there's no version tracking unless you build it yourself.

GMetrix is an Electron app that updates itself with electron-updater. The updater polls a YAML feed on the vendor's CDN, and the feed's top-level `version:` line is the current release:

```
https://releases.gmetrix.net/smse/latest/mac/latest-mac.yml
```

The `electron_updater_feed` source type fetches that feed and reads the `version:` line; the config doesn't need a regex. Most apps built on electron-updater publish the same kind of feed (a `latest-mac.yml` next to the DMG), so the source type isn't GMetrix-specific.

This app was originally tracked with `html_scrape` against the vendor's download page. That broke in July 2026 when GMetrix relaunched the page as a React app: the DMG filename left the server-rendered HTML, and the version moved behind an API that rejects non-browser clients. The updater feed is the sturdier source. Installed copies of the app poll it, so the vendor can't break it without breaking their own auto-update.

```json
{
  "name": "GMetrix SMSe",
  "enabled": true,
  "title_id_env_var": "TITLE_EDITOR_GMETRIX_TITLE_ID",
  "version_source": {
    "type": "electron_updater_feed",
    "url": "https://releases.gmetrix.net/smse/latest/mac/latest-mac.yml"
  },
  "version_pattern": "^\\d+\\.\\d+\\.\\d+$",
  "patch_template": {
    "enabled": true,
    "standalone": true,
    "reboot": false,
    "minimumOperatingSystem": "12.0",
    "killApps": [],
    "components": [
      {
        "name": "GMetrix SMSe",
        "version": "{version}",
        "criteria": [
          { "name": "Application Bundle ID", "operator": "is", "value": "com.skills.management.system.app", "type": "recon" },
          { "name": "Application Version", "operator": "is", "value": "{version}", "type": "recon" }
        ]
      }
    ],
    "capabilities": [
      { "name": "Operating System Version", "operator": "greater than", "value": "12.0", "type": "recon" }
    ]
  }
}
```

To get the bundle ID for any app, run this on a machine that has it installed:

```bash
mdls -name kMDItemCFBundleIdentifier /Applications/YourApp.app
```

To get the Team ID (for verifying code signatures in Installomator or elsewhere):

```bash
codesign -dv /Applications/YourApp.app 2>&1 | grep TeamIdentifier
```

## MacAdmins Python example (framework with no bundle in /Applications)

[MacAdmins Python](https://github.com/macadmins/python) installs to `/Library/ManagedFrameworks/Python/Python3.framework`, not `/Applications`. Jamf doesn't scan that path by default, so `Application Bundle ID` and `Application Title` criteria won't find it. The bundle ID `org.python.python` is also shared with python.org's Python, so even adding a custom search path would be ambiguous.

Instead of relying on Jamf's inventory, the patch definition uses a Title Editor Extension Attribute. The EA script reads the version directly from the framework's Info.plist at its known path. Title Editor embeds the script in the patch definition JSON. When you enable the title in Jamf Pro and click "Accept" on the Extension Attributes tab, Jamf Pro creates the EA automatically. It runs on every managed computer during inventory and reports the installed version.

The version source is `github_releases`, which fetches the latest release tag from the GitHub API. MacAdmins Python tags look like `v3.14.5.80757`, where the last segment is a build number. The `version_parts` option strips it down to `3.14.5` to match what `CFBundleVersion` reports in the plist.

```json
{
  "name": "MacAdmins Python",
  "enabled": true,
  "title_id_env_var": "TITLE_EDITOR_MACADMINS_PYTHON_TITLE_ID",
  "version_source": {
    "type": "github_releases",
    "url": "https://api.github.com/repos/macadmins/python/releases/latest",
    "version_parts": 3
  },
  "version_pattern": "^\\d+\\.\\d+\\.\\d+$",
  "patch_template": {
    "enabled": true,
    "standalone": true,
    "reboot": false,
    "minimumOperatingSystem": "12.0",
    "killApps": [],
    "components": [
      {
        "name": "MacAdmins Python",
        "version": "{version}",
        "criteria": [
          { "name": "patch-macadmins-python", "operator": "is", "value": "{version}", "type": "extensionAttribute" }
        ]
      }
    ],
    "capabilities": [
      { "name": "Operating System Version", "operator": "greater than", "value": "12.0", "type": "recon" }
    ]
  }
}
```

The EA script embedded in the Title Editor definition:

```bash
#!/bin/zsh
plist="/Library/ManagedFrameworks/Python/Python3.framework/Versions/Current/Resources/Info.plist"
if [ -f "$plist" ]; then
    result=$(/usr/libexec/PlistBuddy -c "print CFBundleVersion" "$plist" 2>/dev/null)
    echo "<result>$result</result>"
else
    echo "<result></result>"
fi
```

Don't use `python3 --version` or `/usr/bin/python3` to check the version. On machines without Xcode CLI tools, calling the python binary triggers an install prompt dialog.

After enabling the title in Jamf Pro, go to the patch title settings, click the Extension Attributes tab, and click Accept. Machines will start reporting their MacAdmins Python version on next check-in.

The same `github_releases` + EA pattern covers other tools that live outside `/Applications`. If a project's release tag matches its `CFBundleVersion` exactly (Outset's `v4.3.0.22031`), track the full tag and skip `version_parts` so the GitHub version and the EA line up. If a tool ships as a bare command-line binary with no app bundle (utiluti at `/usr/local/bin/utiluti`), have the EA run the binary with `--version` instead of reading a plist. That's safe for a self-contained notarized binary, unlike `/usr/bin/python3`.

## ScreenConnect Client example (instance-specific version tracking)

ConnectWise ScreenConnect (ConnectWise Control) clients auto-update to match their server, but there's no built-in Jamf definition to track whether that's actually happening. The server exposes its version publicly through `Script.ashx` on the instance URL, no auth required.

The `version_source` uses `html_scrape` to pull `productVersion` from the response; the `regex` needs exactly one capture group containing the version string. The version tracks your server version, not the latest ConnectWise release, because clients should match whatever server they connect to. When you upgrade the server, the Lambda picks up the new version and flags any clients that haven't updated yet.

The app name includes an instance-specific hash (e.g., `connectwisecontrol-<instance-hash>.app`), so the EA uses a wildcard match with `find` instead of a hardcoded path. It reads `CFBundleVersion` for the full 4-part version since `CFBundleShortVersionString` only reports the major.minor.

```json
{
  "name": "ScreenConnect Client",
  "enabled": true,
  "title_id_env_var": "TITLE_EDITOR_SCREENCONNECT_TITLE_ID",
  "version_source": {
    "type": "html_scrape",
    "url": "https://yourorg.screenconnect.com/Script.ashx",
    "regex": "\"productVersion\":\"([0-9]+\\.[0-9]+\\.[0-9]+\\.[0-9]+)\""
  },
  "version_pattern": "^\\d+\\.\\d+\\.\\d+\\.\\d+$",
  "patch_template": {
    "enabled": true,
    "standalone": true,
    "reboot": false,
    "minimumOperatingSystem": "12.0",
    "killApps": [],
    "components": [
      {
        "name": "ScreenConnect Client",
        "version": "{version}",
        "criteria": [
          { "name": "patch-screenconnect-client", "operator": "is", "value": "{version}", "type": "extensionAttribute" }
        ]
      }
    ],
    "capabilities": [
      { "name": "Operating System Version", "operator": "greater than", "value": "12.0", "type": "recon" }
    ]
  }
}
```

The EA script:

```bash
#!/bin/zsh
app_path=$(find /Applications -maxdepth 1 -name "connectwisecontrol-*.app" -print -quit 2>/dev/null)
if [ -n "$app_path" ]; then
    result=$(/usr/libexec/PlistBuddy -c "print CFBundleVersion" "$app_path/Contents/Info.plist" 2>/dev/null)
    echo "<result>$result</result>"
else
    echo "<result></result>"
fi
```

## Washington Secure Browser example (version in a redirect filename)

The Washington Secure Browser (Cambium Assessment's SBAC / Smarter Balanced testing browser) has no built-in Jamf definition, and its version isn't on a web page either. The vendor's download endpoint is a redirector, and the version lives in the filename of the `Location` header it returns:

```
GET https://sb.portal.cambiumast.com/geturls?clientName=washington&operatingSystem=macOS
  -> HTTP 301
  Location: https://.../WASecureBrowser18.0-2025-05-22-universal-signed.dmg?<presigned>
```

The `redirect_filename` source type handles this. Unlike `html_scrape`, which follows redirects and reads the body, `redirect_filename` issues the request with redirects disabled and pulls the version out of the `Location` header's filename. It stops at the 301 and never downloads the 130 MB DMG. The `regex` needs one capture group for the version.

This app installs to `/Applications/WASecureBrowser.app` with a stable bundle ID, so it uses standard `recon` criteria (no Extension Attribute needed). `minimumOperatingSystem` is `10.15.0`, the browser's actual floor.

```json
{
  "name": "Washington Secure Browser",
  "enabled": true,
  "title_id_env_var": "TITLE_EDITOR_WASECUREBROWSER_TITLE_ID",
  "version_source": {
    "type": "redirect_filename",
    "url": "https://sb.portal.cambiumast.com/geturls?clientName=washington&operatingSystem=macOS",
    "regex": "WASecureBrowser([0-9]+\\.[0-9]+)-[0-9-]+-universal-signed\\.dmg"
  },
  "version_pattern": "^\\d+\\.\\d+$",
  "patch_template": {
    "enabled": true,
    "standalone": true,
    "reboot": false,
    "minimumOperatingSystem": "10.15.0",
    "killApps": [],
    "components": [
      {
        "name": "Washington Secure Browser",
        "version": "{version}",
        "criteria": [
          { "name": "Application Bundle ID", "operator": "is", "value": "com.cambiumassessment.securebrowser", "type": "recon" },
          { "name": "Application Version", "operator": "is", "value": "{version}", "type": "recon" }
        ]
      }
    ],
    "capabilities": [
      { "name": "Operating System Version", "operator": "greater than", "value": "10.15.0", "type": "recon" }
    ]
  }
}
```

The `clientName=washington` query parameter is state-specific; other states serve a different signed build. Never pin the S3 URL the redirect points at - it's a short-lived presigned URL regenerated per request. Always hit `geturls`.

## Download-URL canary

Separate from patch-version sync, the Lambda runs a download-URL health check on every invocation. This guards against a vendor silently changing or pulling the installer that an Installomator label (or any constructed-URL deploy) depends on.

On June 12, 2026, Adobe shipped Creative Cloud Desktop under a new build path and pulled the old object. Every Mac's auto-update push then 404'd, and the first signal was failed Jamf policies. A twice-daily GET on that URL would have caught it the same day.

The check replicates the exact URL the `adobecreativeclouddesktop` Installomator label builds: it reads Adobe's `ccdConfig.xml` (`greenline.latest`) for both `macarm64` and `osx10`, constructs the CC DMG URL, and issues a 1-byte range request. A `200`/`206` is healthy; anything else (a `404`) is a failure. It downloads zero bytes of the ~311 MB DMG.

This is deliberately a health check, not a patch title. Adobe's ESD build number (`6.10.0.252.3`) never matches the version Jamf inventories on disk (`6.10.0.253`), so a patch title synced from `ccdConfig` would always show "out of date" even when the installed app is current. The Installomator label handles the install and the version comparison. The canary only checks that the download is still there.

A failure emits the `DownloadUrlCheckFailures` metric and raises at the end of the run, so both the `Errors` alarm and the dedicated download-URL alarm email you (see [Monitoring](#monitoring)).

## Cost

Under $1/month. The Lambda runs for a few seconds twice a day. Everything falls within AWS free tier at this scale except the handful of custom CloudWatch metrics it publishes (a few cents a month).

## Prerequisites

- AWS account with CLI access
- [Terraform](https://developer.hashicorp.com/terraform/install) >= 1.9
- Jamf Pro instance with Title Editor enabled
- Familiarity with Jamf Title Editor (you'll need to create a software title manually before the Lambda can manage it)

## Setup

### Part 1: Create the software title in Title Editor

The Lambda updates an existing Title Editor software title. You need to create it first. The walkthrough below uses Chrome as the example.

**1. Create the title**

Log into your Title Editor instance at `https://yourorg.appcatalog.jamfcloud.com`. Click New > Create.

| Field | Value |
|-------|-------|
| Name | Google Chrome |
| Publisher | Google |
| Current Version | Whatever Chrome's current stable version is |
| ID | googlechrome |

Save.

**2. Add a requirement**

Go to the Requirements tab and add:

| Criteria | Operator | Value |
|----------|----------|-------|
| Application Bundle ID | is | com.google.Chrome |

This tells Jamf which machines have Chrome installed.

**3. Create the first patch**

Go to the Patches tab, click New > Create. Fill in the sub-tabs:

Patch tab:
- Version: (same as current version above)
- Minimum Operating System: 12.0
- Standalone: Yes
- Reboot: No

Components tab (click New, then fill in both the Component and Criteria sub-tabs):
- Component Name: Google Chrome
- Component Version: (same version)
- Criteria: Application Bundle ID is com.google.Chrome AND Application Version is (same version)

Capabilities tab:
- Operating System Version greater than 12.0

Kill Apps tab: Leave empty (not needed if you're handling Chrome patching with your own scripts/Installomator).

Save the patch. Enable it when prompted. The Publish dialog will ask to set it as current version and publish the title. Do both.

**4. Note the title ID**

The numeric ID is in the URL after saving: `/softwaretitles/{ID}`. You'll need this for the Lambda config.

**5. Create a service account**

In Title Editor, go to Settings > User Accounts. Create a new user:
- Account Type: API Only
- Account Status: Enabled
- Privileges: Patch Definitions read + write (leave Users and Preferences unchecked)

Save the username and password somewhere secure.

**6. Add the title to Jamf Pro Patch Management**

In Jamf Pro, go to Computers > Patch Management > Software Titles. Under the Title Editor section, click the + next to your Chrome title to add it.

Note: If the Title Editor section shows "No Software Titles Available," check Settings > Computer Management > Patch Management > Title Editor and uncheck "Validate Software Title Definitions" (this requires code-signed definitions, which Title Editor doesn't set up by default).

### Part 2: Deploy the Lambda

**1. Clone and configure**

```bash
git clone https://github.com/YOUR_ORG/jamf-title-editor-sync.git
cd jamf-title-editor-sync/terraform
cp terraform.tfvars.example terraform.tfvars
```

Edit `terraform.tfvars`:

```hcl
title_editor_url      = "https://yourorg.appcatalog.jamfcloud.com"
title_editor_title_id = "1"
alert_email           = "you@example.com"
```

**2. Deploy**

```bash
terraform init
terraform plan    # review what gets created
terraform apply
```

**3. Set credentials**

Terraform creates SSM parameters with placeholder values. Set the real ones:

```bash
aws ssm put-parameter \
  --name "/jamf-title-editor-sync/te-username" \
  --value "YOUR_SERVICE_ACCOUNT_USERNAME" \
  --type SecureString --overwrite

aws ssm put-parameter \
  --name "/jamf-title-editor-sync/te-password" \
  --value "YOUR_SERVICE_ACCOUNT_PASSWORD" \
  --type SecureString --overwrite
```

**4. Confirm SNS subscription**

Check your email for the AWS SNS confirmation link and click it.

**5. Test**

```bash
aws lambda invoke \
  --function-name jamf-title-editor-sync \
  /tmp/response.json && cat /tmp/response.json
```

Check logs:

```bash
aws logs tail /aws/lambda/jamf-title-editor-sync --follow
```

Verify in Title Editor that the version looks right, then check Jamf Pro > Computers > Patch Management after about 30 minutes.

## Adding another app

1. Add a new entry to `lambda/apps.json`. The examples above cover the common cases - a standard `/Applications` app with recon criteria, HTML scraping, GitHub releases with an Extension Attribute, instance-specific tracking, and a version that only appears in a redirect's filename. Copy whichever one fits.

2. Set the `version_source.type`:
   - `google_versionhistory` for JSON APIs that return a `releases` array (the Lambda reads `releases[0].version`)
   - `html_scrape` for web pages where the version appears in a link or filename. Set `regex` to a pattern with one capture group for the version.
   - `github_releases` for apps that publish to GitHub. Reads `tag_name` from the latest release. Set `version_parts` to truncate if the tag includes a build number.
   - `redirect_filename` for endpoints that 301-redirect to a versioned filename. Reads the `Location` header without following the redirect. Set `regex` to a pattern with one capture group.

3. Fill in the `patch_template` with criteria. For apps in `/Applications`, use `Application Bundle ID` with `"type": "recon"`. For apps outside `/Applications` or with ambiguous bundle IDs, use a Title Editor Extension Attribute with `"type": "extensionAttribute"`. `{version}` placeholders get replaced at runtime.

4. Add a Terraform variable for the new title ID in `variables.tf` and the env var to the Lambda in `main.tf`.

5. Create the software title in Title Editor (UI or API).

6. Redeploy:

```bash
terraform apply
```

### Version source types

| Type | How it works | Example |
|------|-------------|---------|
| `google_versionhistory` | Fetches JSON, reads `releases[0].version` | Chrome VersionHistory API |
| `html_scrape` | Fetches HTML, applies `regex` capture group to extract version | ScreenConnect productVersion page |
| `github_releases` | Fetches latest GitHub release, reads `tag_name`, strips `v` prefix. Optional `version_parts` truncates to N segments. | MacAdmins Python |
| `redirect_filename` | Issues the request with redirects disabled, reads the version from the `Location` header's filename via `regex`. Never downloads the body. | Washington Secure Browser geturls redirect |
| `electron_updater_feed` | Fetches an electron-updater YAML feed, reads the top-level `version:` line | GMetrix latest-mac.yml |

## Monitoring

Four CloudWatch alarms, all emailing via SNS:

- Errors: Lambda threw an exception (auth failure, API error, code bug)
- Duration: Execution approaching the 30-second timeout
- No invocations (24h): Scheduler stopped firing (deleted, misconfigured)
- Download-URL failure: the canary found a dead installer URL (e.g. an Adobe CC build-path change), reported via the `DownloadUrlCheckFailures` metric

When a run fails, the Lambda also emails the detail to the same topic before it raises: which apps failed and why, what succeeded, and a link to the logs. The alarm email only says the run broke; the detail email says which app and what error.

The Errors and Duration alarms ignore missing data because the function only runs twice a day. Once one fires it stays in ALARM until a later run posts a clean datapoint, so an OK email means a later run came back clean. Async retries are off; retrying a broken version source a minute later fails the same way, so a failed run just waits for the next scheduled one.

An optional fifth alarm watches whether Jamf Pro is actually ingesting what the Lambda pushes. When `jamf_pro_url` and `jamf_pro_secret_name` are set in `terraform.tfvars` (a read-only Jamf Pro API client with the patch read privileges, stored in Secrets Manager as JSON keys `client_id` and `client_secret`), each run compares Jamf Pro's latest ingested definition per title against Title Editor and emits a `JamfProDefinitionLag` metric. Lag right after a push is normal, so the alarm fires only when a title stays diverged across two consecutive runs. That pattern is what a dropped Title Editor connection looks like, and since Jamf Pro 11.28 the connection cannot re-establish itself; the fix is re-saving the external patch source in Jamf Pro settings.

### When you get an alert

| Alert | Likely cause | What to do |
|-------|-------------|------------|
| Lambda error | One app's version source broke, auth failure, API change | Read the failure email for the app and error; CloudWatch Logs for the stack trace |
| No invocations | EventBridge schedule gone | `aws scheduler get-schedule --name jamf-title-editor-sync-schedule` |
| Duration | Slow API response | Consider bumping the Lambda timeout |
| Download-URL failure | Vendor changed or pulled an installer URL | Check CloudWatch Logs for the failing arch/URL, fix the Installomator label, re-verify |
| Definition lag | Jamf Pro stopped pulling from Title Editor | Re-save the external patch source in Jamf Pro settings, then confirm the next runs post JamfProDefinitionLag=0 |

## Security

- Lambda has no function URL, no API Gateway, no public endpoint. Only EventBridge can invoke it.
- Credentials stored in SSM Parameter Store as SecureStrings, encrypted with the AWS-managed KMS key.
- Terraform creates SSM params with placeholders. Real values are set via CLI after deploy. `terraform.tfvars` is gitignored.
- The Lambda IAM role can read its two SSM parameters, write to its own log group, and nothing else.
- Version strings are validated against a regex before any Title Editor API call.
- The handler never logs credentials or bearer tokens.

## Credential rotation

Title Editor doesn't support automated rotation, so this is manual:

1. Generate a new password
2. Update it in the Title Editor UI
3. Update SSM: `aws ssm put-parameter --name "/jamf-title-editor-sync/te-password" --value "NEW_PASSWORD" --type SecureString --overwrite`
4. Test: `aws lambda invoke --function-name jamf-title-editor-sync /tmp/test.json`
5. Log the rotation date

There's no downtime, since the Lambda gets a fresh token each run.

## Running tests

```bash
cd tests && python3 -m unittest test_handler -v
```

No external dependencies. Tests mock boto3 and all HTTP calls.

## License

MIT
