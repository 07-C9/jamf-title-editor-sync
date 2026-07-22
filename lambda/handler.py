# ABOUTME: Syncs latest vendor app versions to Jamf Title Editor via API.
# ABOUTME: Runs as a scheduled AWS Lambda, config-driven for multiple apps.

import base64
import copy
import http.cookiejar
import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

TRANSIENT_HTTP_STATUSES = (500, 502, 503, 504)
RETRY_BACKOFF_SECONDS = 2


def _is_timeout(exc):
    if isinstance(exc, TimeoutError):
        return True
    return isinstance(exc, urllib.error.URLError) and isinstance(
        getattr(exc, "reason", None), TimeoutError
    )


def _is_transient(exc):
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in TRANSIENT_HTTP_STATUSES
    return isinstance(exc, (TimeoutError, urllib.error.URLError))


def _with_retries(label, fn, attempts=2, retry_timeouts=True):
    """Runs fn, retrying transient failures (5xx, timeouts, connection errors)
    so a single blip does not fail the run. Every failure re-raises tagged with
    the request label so alerts name the exact call that broke. Only use on
    idempotent requests. retry_timeouts=False exempts timeouts from retry for
    calls whose timeout budget is too large to spend twice."""
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as e:
            retryable = _is_transient(e) and (retry_timeouts or not _is_timeout(e))
            if attempt < attempts and retryable:
                logger.warning(f"{label}: {e} - retrying")
                time.sleep(RETRY_BACKOFF_SECONDS)
                continue
            suffix = f" (failed {attempt} attempts)" if attempt > 1 else ""
            raise RuntimeError(f"{label}: {e}{suffix}") from e

APPS = json.loads((Path(__file__).parent / "apps.json").read_text())

_ssm_client = None
_cached_creds = None


def _get_ssm_client():
    global _ssm_client
    if _ssm_client is None:
        _ssm_client = boto3.client("ssm")
    return _ssm_client


def _get_credentials():
    global _cached_creds
    if _cached_creds is not None:
        return _cached_creds

    ssm = _get_ssm_client()
    username = ssm.get_parameter(
        Name=os.environ["SSM_USERNAME_PATH"], WithDecryption=True
    )["Parameter"]["Value"]
    password = ssm.get_parameter(
        Name=os.environ["SSM_PASSWORD_PATH"], WithDecryption=True
    )["Parameter"]["Value"]
    _cached_creds = (username, password)
    return _cached_creds


def _get_title_editor_token(base_url, username, password):
    auth_string = base64.b64encode(f"{username}:{password}".encode()).decode()

    def fetch():
        req = urllib.request.Request(
            f"{base_url}/v2/auth/tokens",
            method="POST",
            headers={
                "Authorization": f"Basic {auth_string}",
                "Content-Type": "application/json",
            },
            data=b"{}",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())["token"]

    return _with_retries(f"Title Editor auth POST {base_url}/v2/auth/tokens", fetch)


def _fetch_version_json(url, app_name):
    def fetch():
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())

    data = _with_retries(f"version source GET {url}", fetch)
    releases = data.get("releases", [])
    if not releases:
        raise ValueError(f"No releases found for {app_name}")
    return releases[0]["version"]


def _fetch_version_html(url, regex, app_name):
    def fetch():
        cj = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
        req = urllib.request.Request(url)
        with opener.open(req, timeout=15) as resp:
            return resp.read().decode()

    text = _with_retries(f"version source GET {url}", fetch)
    match = re.search(regex, text)
    if not match:
        raise ValueError(f"No version found for {app_name}")
    return match.group(1)


def _fetch_version_github(url, version_parts, app_name):
    def fetch():
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())

    data = _with_retries(f"version source GET {url}", fetch)
    tag = data.get("tag_name")
    if not tag:
        raise ValueError(f"No tag_name found for {app_name}")
    version = tag.lstrip("v")
    if version_parts:
        version = ".".join(version.split(".")[:version_parts])
    return version


def _fetch_version_redirect(url, regex, app_name):
    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    def fetch():
        opener = urllib.request.build_opener(_NoRedirect)
        try:
            with opener.open(urllib.request.Request(url), timeout=15) as resp:
                return resp.headers.get("Location", "")
        except urllib.error.HTTPError as e:
            # A redirect status IS the expected response here; only server
            # trouble should escape to the retry wrapper.
            if e.code in TRANSIENT_HTTP_STATUSES:
                raise
            return e.headers.get("Location", "")

    location = _with_retries(f"version source GET {url}", fetch)
    match = re.search(regex, location)
    if not match:
        raise ValueError(f"No version found in redirect Location for {app_name}")
    return match.group(1)


def _fetch_version_electron_feed(url, app_name):
    def fetch():
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.read().decode()

    text = _with_retries(f"version source GET {url}", fetch)
    match = re.search(r"(?m)^version:\s*['\"]?([0-9][0-9A-Za-z.+-]*)['\"]?\s*$", text)
    if not match:
        raise ValueError(f"No version found for {app_name}")
    return match.group(1)


def _fetch_minimum_accepted_version(config, app_name):
    """Reads the lowest version a vendor still accepts from their published
    feed, walking json_path down to the value for our platform."""
    def fetch():
        req = urllib.request.Request(config["url"], headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())

    data = _with_retries(f"minimum version source GET {config['url']}", fetch)
    for key in config["json_path"]:
        if not isinstance(data, dict) or key not in data:
            raise ValueError(
                f"No minimum accepted version at '{key}' for {app_name} "
                f"({config['url']})"
            )
        data = data[key]
    if not isinstance(data, str) or not re.fullmatch(r"\d+(\.\d+)*", data):
        raise ValueError(
            f"Minimum accepted version for {app_name} is not a version number: "
            f"{data!r} ({config['url']})"
        )
    return data


def _fetch_latest_version(app_config):
    source = app_config["version_source"]
    source_type = source["type"]
    name = app_config["name"]

    if source_type == "google_versionhistory":
        version = _fetch_version_json(source["url"], name)
    elif source_type == "html_scrape":
        version = _fetch_version_html(source["url"], source["regex"], name)
    elif source_type == "github_releases":
        version = _fetch_version_github(source["url"], source.get("version_parts"), name)
    elif source_type == "redirect_filename":
        version = _fetch_version_redirect(source["url"], source["regex"], name)
    elif source_type == "electron_updater_feed":
        version = _fetch_version_electron_feed(source["url"], name)
    else:
        raise ValueError(f"Unknown version source type: {source_type}")

    pattern = re.compile(app_config["version_pattern"])
    if not pattern.match(version):
        raise ValueError(
            f"Version '{version}' does not match pattern '{app_config['version_pattern']}'"
        )
    return version


def _title_editor_request(base_url, token, method, path, body=None):
    url = f"{base_url}{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(
        url,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        data=data,
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def _get_title_info(base_url, token, title_id):
    return _with_retries(
        f"Title Editor GET {base_url}/v2/softwaretitles/{title_id}",
        lambda: _title_editor_request(
            base_url, token, "GET", f"/v2/softwaretitles/{title_id}"
        ),
    )


def _build_patch_body(version, app_config):
    template = copy.deepcopy(app_config["patch_template"])
    release_date = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def replace_version(obj):
        if isinstance(obj, str):
            return obj.replace("{version}", version)
        if isinstance(obj, dict):
            return {k: replace_version(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [replace_version(item) for item in obj]
        return obj

    patch = replace_version(template)
    patch["version"] = version
    patch["releaseDate"] = release_date
    return patch


def _update_title(base_url, token, title_id, version, app_config, patch_exists=False):
    if patch_exists:
        logger.info(f"Patch {version} already exists for {app_config['name']}, skipping POST")
    else:
        patch_body = _build_patch_body(version, app_config)
        try:
            _title_editor_request(
                base_url, token, "POST", f"/v2/softwaretitles/{title_id}/patches", patch_body
            )
            logger.info(f"Added patch version {version} for {app_config['name']}")
        except urllib.error.HTTPError as e:
            if e.code in (400, 409):
                body = e.read().decode() if hasattr(e, 'read') else ""
                if e.code == 409 or "DUPLICATE_RECORD" in body:
                    logger.info(f"Version {version} already exists for {app_config['name']}, skipping POST")
                else:
                    raise
            else:
                raise

    _title_editor_request(
        base_url, token, "PUT", f"/v2/softwaretitles/{title_id}",
        {"currentVersion": version, "enabled": True}
    )
    logger.info(f"Set currentVersion to {version} for {app_config['name']}")

    boto3.client("cloudwatch").put_metric_data(
        Namespace="JamfPatchSync",
        MetricData=[{
            "MetricName": "PatchVersionUpdated",
            "Dimensions": [{"Name": "AppName", "Value": app_config["name"]}],
            "Value": 1,
            "Unit": "Count",
        }],
    )


def _http_get_text(url, timeout=15):
    req = urllib.request.Request(url, headers={"Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode()


def _adobe_ccd_download_url(cc_arch):
    cfg = _http_get_text(
        f"https://ffc-static-cdn.oobesaas.adobe.com/features/v3/{cc_arch}/ccdConfig.xml"
    )
    m = re.search(r'greenline\.latest".*?"version":\s*"([\d.]+)"', cfg, re.S)
    if not m:
        raise ValueError(f"ccdConfig greenline.latest version not found ({cc_arch})")
    version = m.group(1)
    parts = version.split(".")
    triplet, build = ".".join(parts[:3]), ".".join(parts[3:])
    vU, bU = triplet.replace(".", "_"), build.replace(".", "_")
    url = (
        "https://ccmdls.adobe.com/AdobeProducts/StandaloneBuilds/ACCC/ESD/"
        f"{triplet}/{build}/{cc_arch}/ACCCx{vU}_{bU}.dmg"
    )
    return version, url


def _url_is_live(url, timeout=20):
    # 1-byte range request; confirm the object exists without pulling the ~311 MB body
    req = urllib.request.Request(url, method="GET", headers={"Range": "bytes=0-0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status in (200, 206)
    except urllib.error.HTTPError as e:
        return e.code in (200, 206)
    except Exception:
        return False


def _run_download_checks():
    """Returns human-readable failure strings for any dead Installomator download URL (empty = healthy)."""
    failures = []
    for cc_arch in ("macarm64", "osx10"):
        try:
            version, url = _adobe_ccd_download_url(cc_arch)
            if not _url_is_live(url):
                failures.append(f"Adobe CC {cc_arch}: download URL not live ({version}) {url}")
        except Exception as e:
            failures.append(f"Adobe CC {cc_arch}: check errored - {e}")
    return failures


_cached_jamf_pro_creds = None


def _get_jamf_pro_credentials():
    global _cached_jamf_pro_creds
    if _cached_jamf_pro_creds is not None:
        return _cached_jamf_pro_creds
    secret = boto3.client("secretsmanager").get_secret_value(
        SecretId=os.environ["JAMF_PRO_SECRET_ID"]
    )
    data = json.loads(secret["SecretString"])
    _cached_jamf_pro_creds = (data["client_id"], data["client_secret"])
    return _cached_jamf_pro_creds


def _get_jamf_pro_token(base_url, client_id, client_secret):
    body = urllib.parse.urlencode({
        "client_id": client_id,
        "grant_type": "client_credentials",
        "client_secret": client_secret,
    }).encode()
    req = urllib.request.Request(
        f"{base_url}/api/oauth/token",
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data=body,
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())["access_token"]


def _jamf_pro_get(base_url, token, path, accept, label=None, timeout=15, retry_timeouts=True):
    def fetch():
        req = urllib.request.Request(
            f"{base_url}{path}",
            headers={"Authorization": f"Bearer {token}", "Accept": accept},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()

    return _with_retries(
        label or f"GET {base_url}{path} ({timeout}s timeout)",
        fetch, retry_timeouts=retry_timeouts,
    )


def _run_jamf_pro_drift_check(te_state):
    """Compares Jamf Pro's latest ingested definition for each Title Editor
    title (matched by name_id == the TE title slug) against the version this
    run confirmed in Title Editor. Returns divergence strings (empty = current)."""
    base_url = os.environ["JAMF_PRO_URL"].rstrip("/")
    client_id, client_secret = _get_jamf_pro_credentials()
    token = _with_retries(
        f"OAuth token POST {base_url}/api/oauth/token",
        lambda: _get_jamf_pro_token(base_url, client_id, client_secret),
    )

    # This endpoint serializes every patch config on the instance and answers
    # in 13-17s here; give it a budget well above that, and never spend the
    # budget twice (a second stall would leave no Lambda time to send the
    # failure email).
    configs = json.loads(_jamf_pro_get(
        base_url, token, "/api/v2/patch-software-title-configurations", "application/json",
        timeout=45, retry_timeouts=False,
    ))
    drifted = []
    for config in configs:
        if config.get("jamfOfficial", True):
            continue
        detail_path = f"/JSSResource/patchsoftwaretitles/id/{config['id']}"
        xml_body = _jamf_pro_get(
            base_url, token, detail_path, "application/xml",
            label=f"patch config {config['id']} ({config.get('displayName')}) "
                  f"GET {base_url}{detail_path}",
        )
        # Jamf Pro classic API responses never carry a DTD; refuse any that do
        # rather than expose the parser to entity-expansion tricks.
        if b"<!DOCTYPE" in xml_body or b"<!ENTITY" in xml_body:
            raise ValueError(f"Unexpected DTD in Jamf Pro XML for config {config['id']}")
        root = ET.fromstring(xml_body)
        te_entry = te_state.get(root.findtext("name_id"))
        if te_entry is None:
            continue
        jamf_latest = root.findtext("versions/version/software_version")
        if jamf_latest != te_entry["version"]:
            drifted.append(
                f"{te_entry['app']} (Jamf Pro has {jamf_latest}, "
                f"Title Editor has {te_entry['version']})"
            )
    return drifted


def _run_minimum_version_checks():
    """Reports when a vendor's published minimum accepted version differs from
    the value recorded in apps.json. Patch reporting already shows who is behind
    the newest release; only this shows the moment being behind turns into being
    refused, because that number moves on the vendor's schedule and changes
    nothing on disk. Returns (changes, errors)."""
    changes, errors = [], []
    for app in APPS:
        config = app.get("minimum_accepted_version")
        if not config or not app.get("enabled", True):
            continue
        if "known" not in config:
            errors.append(
                f"{app['name']}: minimum_accepted_version has no 'known' value in apps.json"
            )
            continue
        try:
            live = _fetch_minimum_accepted_version(config, app["name"])
        except Exception as e:
            # One dead feed must not discard changes already found for other apps.
            errors.append(str(e))
            continue
        if live != config["known"]:
            changes.append(
                f"{app['name']}: vendor minimum accepted version is now {live} "
                f"(apps.json records {config['known']})"
            )
    return changes, errors


def _function_name():
    return os.environ.get("AWS_LAMBDA_FUNCTION_NAME", "jamf-title-editor-sync")


def _log_url():
    region = os.environ.get("AWS_REGION", "us-west-2")
    log_group = os.environ.get(
        "AWS_LAMBDA_LOG_GROUP_NAME", f"/aws/lambda/{_function_name()}"
    )
    return (
        f"https://{region}.console.aws.amazon.com/cloudwatch/home?region={region}"
        f"#logsV2:log-groups/log-group/{log_group.replace('/', '$252F')}"
    )


def _sns_subject(text):
    """SNS rejects subjects over 100 characters."""
    return text if len(text) <= 100 else text[:97] + "..."


def _build_update_notice(results, request_id):
    """Says exactly which versions this run published. The Lambda already knows
    what it pushed, so the notice names the app and both versions rather than
    leaving a metric threshold to be interpreted."""
    updated = [r for r in results if r.get("status") == "updated"]
    function_name = _function_name()
    noun = "version" if len(updated) == 1 else "versions"

    if len(updated) == 1:
        subject = f"{function_name} published: {updated[0]['app']} {updated[0]['to']}"
    else:
        subject = f"{function_name} published {len(updated)} new {noun}"

    lines = [
        f"{function_name} published {len(updated)} new {noun} to Title Editor.",
        f"Jamf Pro ingests {'it' if len(updated) == 1 else 'them'} on its own "
        f"schedule, usually within about 30 minutes.",
        "",
    ]
    for r in updated:
        lines.append(f"  - {r['app']}: {r['from']} -> {r['to']}")
    lines += ["", f"Request ID: {request_id}", f"Logs: {_log_url()}"]
    return _sns_subject(subject), "\n".join(lines)


def _publish_update_notice(results, request_id):
    topic_arn = os.environ.get("ALERT_TOPIC_ARN")
    if not topic_arn:
        return
    if not any(r.get("status") == "updated" for r in results):
        return
    subject, body = _build_update_notice(results, request_id)
    try:
        boto3.client("sns").publish(TopicArn=topic_arn, Subject=subject, Message=body)
    except Exception as e:
        # A successful sync must not be failed by a notification problem.
        logger.error(f"Could not publish update notice to SNS: {e}")


def _build_failure_alert(results, failures, download_failures, request_id):
    function_name = _function_name()

    subject = _sns_subject(
        f"{function_name} FAILED: {', '.join(failures) or 'download URL checks'}"
    )

    changed = [r for r in results if r.get("status") == "changed"]
    if changed:
        intro = (
            f"{function_name} run failed. Items under 'Failed' and 'Vendor minimum "
            f"version changed' both need attention."
        )
    else:
        intro = f"{function_name} run failed. Only the items under 'Failed' need attention."
    lines = [intro, ""]
    lines.append("Failed:")
    for r in results:
        if r.get("status") != "failed":
            continue
        if "app" in r:
            lines.append(f"  - {r['app']}: {r['error']}")
        else:
            lines.append(f"  - {r['detail']}")
            if r.get("check") == "jamfpro_drift":
                lines.append(
                    "    (post-sync check that Jamf Pro is ingesting Title Editor"
                    " definitions; not an app sync, the app sync results are below)"
                )
    if changed:
        lines.append("")
        lines.append("Vendor minimum version changed (nothing on disk changed; the bar moved):")
        for r in changed:
            lines.append(f"  - {r['detail']}")

    app_results = [r for r in results if "app" in r]
    ok = [r for r in app_results if r.get("status") in ("current", "updated")]
    if app_results:
        lines.append("")
        if len(ok) == len(app_results):
            lines.append(f"App syncs - all {len(ok)} OK:")
        else:
            lines.append(f"App syncs OK ({len(ok)} of {len(app_results)}):")
    for r in ok:
        if r["status"] == "current":
            lines.append(f"  - {r['app']}: current at {r['version']}")
        else:
            lines.append(f"  - {r['app']}: updated {r['from']} -> {r['to']}")
    lines += ["", f"Request ID: {request_id}", f"Logs: {_log_url()}"]
    return subject, "\n".join(lines)


def _publish_failure_alert(results, failures, download_failures, request_id):
    topic_arn = os.environ.get("ALERT_TOPIC_ARN")
    if not topic_arn:
        return
    subject, body = _build_failure_alert(results, failures, download_failures, request_id)
    try:
        boto3.client("sns").publish(TopicArn=topic_arn, Subject=subject, Message=body)
    except Exception as e:
        logger.error(f"Could not publish failure alert to SNS: {e}")


def lambda_handler(event, context):
    base_url = os.environ["TITLE_EDITOR_URL"].rstrip("/")
    try:
        username, password = _get_credentials()
        token = _get_title_editor_token(base_url, username, password)
    except Exception as e:
        # Nothing can sync without a Title Editor session; still send the
        # detail email before dying so the alert names the failing call.
        logger.error(f"Title Editor auth failed - {e}")
        _publish_failure_alert(
            [{"check": "auth", "status": "failed", "detail": str(e)}],
            ["Title Editor auth"], [],
            getattr(context, "aws_request_id", "unknown"),
        )
        raise

    results = []
    failures = []
    te_state = {}
    for app in APPS:
        if not app.get("enabled", True):
            logger.info(f"Skipping disabled app: {app['name']}")
            continue

        try:
            title_id = os.environ[app["title_id_env_var"]]
            latest = _fetch_latest_version(app)
            title_info = _get_title_info(base_url, token, title_id)
            current = title_info.get("currentVersion", "")

            if latest == current:
                logger.info(f"{app['name']}: up to date at {current}")
                results.append({"app": app["name"], "status": "current", "version": current})
            else:
                existing_versions = {p["version"] for p in title_info.get("patches", [])}
                logger.info(f"{app['name']}: updating {current} -> {latest}")
                _update_title(base_url, token, title_id, latest, app, latest in existing_versions)
                results.append({"app": app["name"], "status": "updated", "from": current, "to": latest})

            if title_info.get("id"):
                te_state[title_info["id"]] = {"app": app["name"], "version": latest}

        except Exception as e:
            logger.error(f"{app['name']}: failed - {e}")
            failures.append(app["name"])
            results.append({"app": app["name"], "status": "failed", "error": str(e)})

    download_failures = _run_download_checks()
    for failure in download_failures:
        logger.error(failure)
        results.append({"check": "download_url", "status": "failed", "detail": failure})

    boto3.client("cloudwatch").put_metric_data(
        Namespace="JamfPatchSync",
        MetricData=[{
            "MetricName": "DownloadUrlCheckFailures",
            "Value": len(download_failures),
            "Unit": "Count",
        }],
    )

    if os.environ.get("JAMF_PRO_URL") and os.environ.get("JAMF_PRO_SECRET_ID"):
        try:
            drifted = _run_jamf_pro_drift_check(te_state)
            for item in drifted:
                logger.warning(f"Jamf Pro definition lag: {item}")
                results.append({"check": "jamfpro_drift", "status": "lagging", "detail": item})
            boto3.client("cloudwatch").put_metric_data(
                Namespace="JamfPatchSync",
                MetricData=[{
                    "MetricName": "JamfProDefinitionLag",
                    "Value": len(drifted),
                    "Unit": "Count",
                }],
            )
        except Exception as e:
            logger.error(f"Jamf Pro drift check failed - {e}")
            failures.append("Jamf Pro drift check")
            results.append({
                "check": "jamfpro_drift", "status": "failed",
                "detail": f"Jamf Pro drift check: {e}",
            })

    try:
        changes, min_version_errors = _run_minimum_version_checks()
    except Exception as e:
        changes, min_version_errors = [], [str(e)]

    for item in changes:
        logger.warning(f"Vendor minimum version changed: {item}")
        results.append({"check": "minimum_version", "status": "changed", "detail": item})
    for error in min_version_errors:
        logger.error(f"Minimum version check failed - {error}")
        results.append({
            "check": "minimum_version", "status": "failed",
            "detail": f"Minimum version check: {error}",
        })
    if min_version_errors:
        failures.append("Minimum version check")
    else:
        # Only record a count the check actually established. Publishing 0 after
        # a failed check would clear a standing alarm with a number nobody measured.
        boto3.client("cloudwatch").put_metric_data(
            Namespace="JamfPatchSync",
            MetricData=[{
                "MetricName": "MinimumVersionChanged",
                "Value": len(changes),
                "Unit": "Count",
            }],
        )

    _publish_update_notice(results, getattr(context, "aws_request_id", "unknown"))

    if failures or download_failures:
        _publish_failure_alert(
            results, failures, download_failures,
            getattr(context, "aws_request_id", "unknown"),
        )
        raise RuntimeError(
            f"Version sync failed for: {', '.join(failures) or 'none'}; "
            f"download checks failed: {'; '.join(download_failures) or 'none'}"
        )

    return {"statusCode": 200, "results": results}
