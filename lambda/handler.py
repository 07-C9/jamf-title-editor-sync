# ABOUTME: Syncs latest vendor app versions to Jamf Title Editor via API.
# ABOUTME: Runs as a scheduled AWS Lambda, config-driven for multiple apps.

import base64
import copy
import http.cookiejar
import json
import logging
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

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


def _fetch_version_json(url, app_name):
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read())
    releases = data.get("releases", [])
    if not releases:
        raise ValueError(f"No releases found for {app_name}")
    return releases[0]["version"]


def _fetch_version_html(url, regex, app_name):
    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    req = urllib.request.Request(url)
    with opener.open(req, timeout=15) as resp:
        text = resp.read().decode()
    match = re.search(regex, text)
    if not match:
        raise ValueError(f"No version found for {app_name}")
    return match.group(1)


def _fetch_version_github(url, version_parts, app_name):
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read())
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

    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(urllib.request.Request(url), timeout=15) as resp:
            location = resp.headers.get("Location", "")
    except urllib.error.HTTPError as e:
        location = e.headers.get("Location", "")
    match = re.search(regex, location)
    if not match:
        raise ValueError(f"No version found in redirect Location for {app_name}")
    return match.group(1)


def _fetch_version_electron_feed(url, app_name):
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=15) as resp:
        text = resp.read().decode()
    match = re.search(r"(?m)^version:\s*['\"]?([0-9][0-9A-Za-z.+-]*)['\"]?\s*$", text)
    if not match:
        raise ValueError(f"No version found for {app_name}")
    return match.group(1)


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
    return _title_editor_request(
        base_url, token, "GET", f"/v2/softwaretitles/{title_id}"
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


def _build_failure_alert(results, failures, download_failures, request_id):
    function_name = os.environ.get("AWS_LAMBDA_FUNCTION_NAME", "jamf-title-editor-sync")
    region = os.environ.get("AWS_REGION", "us-west-2")
    log_group = os.environ.get("AWS_LAMBDA_LOG_GROUP_NAME", f"/aws/lambda/{function_name}")

    subject = f"{function_name} FAILED: {', '.join(failures) or 'download URL checks'}"
    if len(subject) > 100:
        subject = subject[:97] + "..."

    lines = [f"{function_name} run failed. Only the items under 'Failed' need attention.", ""]
    lines.append("Failed:")
    for r in results:
        if r.get("status") != "failed":
            continue
        if "app" in r:
            lines.append(f"  - {r['app']}: {r['error']}")
        else:
            lines.append(f"  - {r['detail']}")
    ok = [r for r in results if r.get("status") in ("current", "updated")]
    lines.append("")
    lines.append(f"OK this run ({len(ok)}):")
    for r in ok:
        if r["status"] == "current":
            lines.append(f"  - {r['app']}: current at {r['version']}")
        else:
            lines.append(f"  - {r['app']}: updated {r['from']} -> {r['to']}")
    log_url = (
        f"https://{region}.console.aws.amazon.com/cloudwatch/home?region={region}"
        f"#logsV2:log-groups/log-group/{log_group.replace('/', '$252F')}"
    )
    lines += ["", f"Request ID: {request_id}", f"Logs: {log_url}"]
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
    username, password = _get_credentials()
    token = _get_title_editor_token(base_url, username, password)

    results = []
    failures = []
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
