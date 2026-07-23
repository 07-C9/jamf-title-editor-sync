# ABOUTME: Unit tests for the jamf-patch-sync Lambda handler.
# ABOUTME: Covers version check, update flow, idempotency, auth, and error paths.

import io
import json
import os
import sys
import types
import unittest
import urllib.error
from unittest.mock import MagicMock, patch, call

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lambda"))

os.environ.setdefault("SSM_USERNAME_PATH", "/test/username")
os.environ.setdefault("SSM_PASSWORD_PATH", "/test/password")
os.environ.setdefault("TITLE_EDITOR_URL", "https://test.appcatalog.jamfcloud.com")
os.environ.setdefault("TITLE_EDITOR_TITLE_ID", "42")
os.environ.setdefault("TITLE_EDITOR_GMETRIX_TITLE_ID", "99")
os.environ.setdefault("TITLE_EDITOR_MACADMINS_PYTHON_TITLE_ID", "100")
os.environ.setdefault("TITLE_EDITOR_SCREENCONNECT_TITLE_ID", "101")
os.environ.setdefault("TITLE_EDITOR_PROMETHEAN_SCREEN_SHARE_TITLE_ID", "102")

if "boto3" not in sys.modules:
    sys.modules["boto3"] = MagicMock()


def _make_response(data, status=200, raw=False):
    body = data.encode() if raw else json.dumps(data).encode()
    resp = MagicMock()
    resp.read.return_value = body
    resp.status = status
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def _reload_handler():
    import importlib
    import handler
    importlib.reload(handler)
    handler._cached_creds = None
    handler._ssm_client = None
    return handler


def _disable_download_checks(test_case):
    # The canary is a separate pass; isolate the version-sync handler tests from it.
    p = patch("handler._run_download_checks", return_value=[])
    p.start()
    test_case.addCleanup(p.stop)


class TestFetchLatestVersion(unittest.TestCase):

    def test_valid_chrome_version(self):
        handler = _reload_handler()
        app_config = handler.APPS[0]
        response_data = {
            "releases": [{"version": "149.0.7827.54", "fraction": 1}]
        }
        with patch("handler.urllib.request.urlopen", return_value=_make_response(response_data)):
            version = handler._fetch_latest_version(app_config)
            self.assertEqual(version, "149.0.7827.54")

    def test_empty_releases_raises(self):
        handler = _reload_handler()
        app_config = handler.APPS[0]
        with patch("handler.urllib.request.urlopen", return_value=_make_response({"releases": []})):
            with self.assertRaises(ValueError) as ctx:
                handler._fetch_latest_version(app_config)
            self.assertIn("No releases", str(ctx.exception))

    def test_malformed_version_raises(self):
        handler = _reload_handler()
        app_config = handler.APPS[0]
        response_data = {"releases": [{"version": "149.0.7827"}]}
        with patch("handler.urllib.request.urlopen", return_value=_make_response(response_data)):
            with self.assertRaises(ValueError) as ctx:
                handler._fetch_latest_version(app_config)
            self.assertIn("does not match", str(ctx.exception))

    def test_injection_attempt_rejected(self):
        handler = _reload_handler()
        app_config = handler.APPS[0]
        response_data = {"releases": [{"version": "149.0.7827.54; DROP TABLE"}]}
        with patch("handler.urllib.request.urlopen", return_value=_make_response(response_data)):
            with self.assertRaises(ValueError):
                handler._fetch_latest_version(app_config)


class TestBuildPatchBody(unittest.TestCase):

    def test_version_substitution(self):
        handler = _reload_handler()
        app_config = handler.APPS[0]
        body = handler._build_patch_body("149.0.7827.54", app_config)
        self.assertEqual(body["version"], "149.0.7827.54")
        self.assertEqual(body["components"][0]["version"], "149.0.7827.54")
        criteria_values = [c["value"] for c in body["components"][0]["criteria"]]
        self.assertIn("149.0.7827.54", criteria_values)
        self.assertIn("com.google.Chrome", criteria_values)
        self.assertIn("releaseDate", body)

    def test_template_not_mutated(self):
        handler = _reload_handler()
        app_config = handler.APPS[0]
        original = json.dumps(app_config["patch_template"])
        handler._build_patch_body("149.0.7827.54", app_config)
        self.assertEqual(json.dumps(app_config["patch_template"]), original)


class TestLambdaHandler(unittest.TestCase):

    def setUp(self):
        handler = _reload_handler()
        self._all_apps = handler.APPS[:]

    def tearDown(self):
        handler = _reload_handler()
        handler.APPS = self._all_apps

    def _setup_mocks(self, handler, current_version, latest_version="149.0.7827.54"):
        handler.APPS = [a for a in handler.APPS if a["name"] == "Google Chrome"]
        mock_ssm = MagicMock()
        mock_ssm.get_parameter.side_effect = [
            {"Parameter": {"Value": "testuser"}},
            {"Parameter": {"Value": "testpass"}},
        ]

        token_resp = _make_response({"token": "test-bearer-token"})
        version_api_resp = _make_response({
            "releases": [{"version": latest_version, "fraction": 1}]
        })
        title_get_resp = _make_response({
            "currentVersion": current_version,
            "enabled": True,
            "patches": [{"version": current_version, "enabled": True}] if current_version else []
        })
        post_resp = _make_response({"patchId": 1})
        put_resp = _make_response({"currentVersion": latest_version, "enabled": True})

        responses = [token_resp, version_api_resp, title_get_resp]
        if current_version != latest_version:
            responses.extend([post_resp, put_resp])

        mock_urlopen = patch("handler.urllib.request.urlopen", side_effect=responses)
        mock_boto_client = patch("handler.boto3.client")

        urlopen_mock = mock_urlopen.start()
        boto_mock = mock_boto_client.start()
        boto_mock.side_effect = lambda svc: mock_ssm if svc == "ssm" else MagicMock()

        self.addCleanup(mock_urlopen.stop)
        self.addCleanup(mock_boto_client.stop)

        dl = patch("handler._run_download_checks", return_value=[])
        dl.start()
        self.addCleanup(dl.stop)

        return urlopen_mock, mock_ssm, boto_mock

    def test_versions_match_no_update(self):
        handler = _reload_handler()
        urlopen_mock, _, _ = self._setup_mocks(handler, "149.0.7827.54")

        result = handler.lambda_handler({}, None)

        self.assertEqual(result["statusCode"], 200)
        self.assertEqual(result["results"][0]["status"], "current")
        self.assertEqual(urlopen_mock.call_count, 3)

    def test_new_version_triggers_update(self):
        handler = _reload_handler()
        urlopen_mock, _, _ = self._setup_mocks(handler, "148.0.7778.216")

        result = handler.lambda_handler({}, None)

        self.assertEqual(result["statusCode"], 200)
        self.assertEqual(result["results"][0]["status"], "updated")
        self.assertEqual(result["results"][0]["from"], "148.0.7778.216")
        self.assertEqual(result["results"][0]["to"], "149.0.7827.54")
        self.assertEqual(urlopen_mock.call_count, 5)

    def test_post_409_skips_to_put(self):
        handler = _reload_handler()
        _disable_download_checks(self)
        handler.APPS = [a for a in handler.APPS if a["name"] == "Google Chrome"]

        mock_ssm = MagicMock()
        mock_ssm.get_parameter.side_effect = [
            {"Parameter": {"Value": "testuser"}},
            {"Parameter": {"Value": "testpass"}},
        ]

        import urllib.error
        http_409 = urllib.error.HTTPError(
            url="https://test/v2/softwaretitles/42/patches",
            code=409, msg="Conflict", hdrs={}, fp=io.BytesIO(b"")
        )

        token_resp = _make_response({"token": "test-bearer-token"})
        version_api_resp = _make_response({
            "releases": [{"version": "149.0.7827.54", "fraction": 1}]
        })
        title_get_resp = _make_response({
            "currentVersion": "148.0.7778.216",
            "enabled": True,
            "patches": [{"version": "148.0.7778.216", "enabled": True}]
        })
        put_resp = _make_response({"currentVersion": "149.0.7827.54", "enabled": True})

        responses = [token_resp, version_api_resp, title_get_resp, http_409, put_resp]

        with patch("handler.urllib.request.urlopen", side_effect=responses) as urlopen_mock:
            with patch("handler.boto3.client") as boto_mock:
                boto_mock.side_effect = lambda svc: mock_ssm if svc == "ssm" else MagicMock()
                result = handler.lambda_handler({}, None)

        self.assertEqual(result["results"][0]["status"], "updated")
        self.assertEqual(urlopen_mock.call_count, 5)

    def test_post_500_raises(self):
        handler = _reload_handler()
        _disable_download_checks(self)
        handler.APPS = [a for a in handler.APPS if a["name"] == "Google Chrome"]

        mock_ssm = MagicMock()
        mock_ssm.get_parameter.side_effect = [
            {"Parameter": {"Value": "testuser"}},
            {"Parameter": {"Value": "testpass"}},
        ]

        import urllib.error
        http_500 = urllib.error.HTTPError(
            url="https://test/v2/softwaretitles/42/patches",
            code=500, msg="Server Error", hdrs={}, fp=io.BytesIO(b"")
        )

        token_resp = _make_response({"token": "test-bearer-token"})
        version_api_resp = _make_response({
            "releases": [{"version": "149.0.7827.54", "fraction": 1}]
        })
        title_get_resp = _make_response({
            "currentVersion": "148.0.7778.216",
            "enabled": True,
            "patches": [{"version": "148.0.7778.216", "enabled": True}]
        })

        with patch("handler.urllib.request.urlopen", side_effect=[token_resp, version_api_resp, title_get_resp, http_500]):
            with patch("handler.boto3.client") as boto_mock:
                boto_mock.side_effect = lambda svc: mock_ssm if svc == "ssm" else MagicMock()
                with self.assertRaises(RuntimeError) as ctx:
                    handler.lambda_handler({}, None)

        self.assertIn("Google Chrome", str(ctx.exception))

    def test_disabled_app_skipped(self):
        handler = _reload_handler()
        _disable_download_checks(self)
        original_apps = handler.APPS
        handler.APPS = [dict(original_apps[0], enabled=False)]

        mock_ssm = MagicMock()
        mock_ssm.get_parameter.side_effect = [
            {"Parameter": {"Value": "testuser"}},
            {"Parameter": {"Value": "testpass"}},
        ]
        token_resp = _make_response({"token": "test-bearer-token"})

        with patch("handler.urllib.request.urlopen", side_effect=[token_resp]) as urlopen_mock:
            with patch("handler.boto3.client") as boto_mock:
                boto_mock.side_effect = lambda svc: mock_ssm if svc == "ssm" else MagicMock()
                result = handler.lambda_handler({}, None)

        self.assertEqual(result["results"], [])
        self.assertEqual(urlopen_mock.call_count, 1)
        handler.APPS = original_apps

    def test_credentials_cached_across_calls(self):
        handler = _reload_handler()

        mock_ssm = MagicMock()
        mock_ssm.get_parameter.side_effect = [
            {"Parameter": {"Value": "testuser"}},
            {"Parameter": {"Value": "testpass"}},
        ]

        with patch("handler.boto3.client", return_value=mock_ssm):
            handler._get_credentials()
            handler._get_credentials()

        self.assertEqual(mock_ssm.get_parameter.call_count, 2)


class TestFetchVersionHtmlScrape(unittest.TestCase):

    def _make_html_app_config(self):
        return {
            "name": "GMetrix SMSe",
            "version_source": {
                "type": "html_scrape",
                "url": "https://www.gmetrix.net/GetGMetrixSMS.aspx",
                "regex": "GMetrixSMSe-([0-9]+\\.[0-9]+\\.[0-9]+)-universal\\.dmg",
            },
            "version_pattern": "^\\d+\\.\\d+\\.\\d+$",
        }

    def test_valid_html_scrape_version(self):
        handler = _reload_handler()
        app_config = self._make_html_app_config()
        html = '<a href="https://releases.gmetrix.net/smse/latest/mac/GMetrixSMSe-7.1.7-universal.dmg">Download</a>'
        mock_opener = MagicMock()
        mock_opener.open.return_value = _make_response(html, raw=True)
        with patch("handler.urllib.request.build_opener", return_value=mock_opener):
            version = handler._fetch_latest_version(app_config)
            self.assertEqual(version, "7.1.7")

    def test_html_scrape_no_match_raises(self):
        handler = _reload_handler()
        app_config = self._make_html_app_config()
        html = '<html><body>No download link here</body></html>'
        mock_opener = MagicMock()
        mock_opener.open.return_value = _make_response(html, raw=True)
        with patch("handler.urllib.request.build_opener", return_value=mock_opener):
            with self.assertRaises(ValueError) as ctx:
                handler._fetch_latest_version(app_config)
            self.assertIn("No version found", str(ctx.exception))

    def test_html_scrape_version_pattern_mismatch_raises(self):
        handler = _reload_handler()
        app_config = self._make_html_app_config()
        app_config["version_pattern"] = "^\\d+\\.\\d+\\.\\d+\\.\\d+$"
        html = '<a href="GMetrixSMSe-7.1.7-universal.dmg">Download</a>'
        mock_opener = MagicMock()
        mock_opener.open.return_value = _make_response(html, raw=True)
        with patch("handler.urllib.request.build_opener", return_value=mock_opener):
            with self.assertRaises(ValueError) as ctx:
                handler._fetch_latest_version(app_config)
            self.assertIn("does not match", str(ctx.exception))

    def test_unknown_version_source_type_raises(self):
        handler = _reload_handler()
        app_config = {
            "name": "FakeApp",
            "version_source": {"type": "magic", "url": "https://example.com"},
            "version_pattern": "^\\d+$",
        }
        with self.assertRaises(ValueError) as ctx:
            handler._fetch_latest_version(app_config)
        self.assertIn("Unknown version source type", str(ctx.exception))


class TestFetchVersionElectronUpdaterFeed(unittest.TestCase):

    def _make_feed_app_config(self):
        return {
            "name": "GMetrix SMSe",
            "version_source": {
                "type": "electron_updater_feed",
                "url": "https://releases.gmetrix.net/smse/latest/mac/latest-mac.yml",
            },
            "version_pattern": "^\\d+\\.\\d+\\.\\d+$",
        }

    def test_valid_feed_version(self):
        handler = _reload_handler()
        app_config = self._make_feed_app_config()
        feed = (
            "version: 7.1.17\n"
            "files:\n"
            "  - url: GMetrixSMSe-7.1.17-universal.dmg\n"
            "    version: 0.0.0-decoy\n"
            "releaseDate: '2026-07-09T14:02:49.811Z'\n"
        )
        with patch("handler.urllib.request.urlopen", return_value=_make_response(feed, raw=True)):
            version = handler._fetch_latest_version(app_config)
            self.assertEqual(version, "7.1.17")

    def test_feed_quoted_version(self):
        handler = _reload_handler()
        app_config = self._make_feed_app_config()
        feed = "version: '7.1.17'\nreleaseDate: '2026-07-09T14:02:49.811Z'\n"
        with patch("handler.urllib.request.urlopen", return_value=_make_response(feed, raw=True)):
            self.assertEqual(handler._fetch_latest_version(app_config), "7.1.17")

    def test_feed_no_version_raises(self):
        handler = _reload_handler()
        app_config = self._make_feed_app_config()
        feed = "files:\n  - url: GMetrixSMSe.dmg\n"
        with patch("handler.urllib.request.urlopen", return_value=_make_response(feed, raw=True)):
            with self.assertRaises(ValueError) as ctx:
                handler._fetch_latest_version(app_config)
            self.assertIn("No version found", str(ctx.exception))


# A synthetic second app for exercising multi-app handling and html_scrape.
# It is NOT the shipped GMetrix config, which tracks an electron-updater feed;
# TestAppsJsonIntegrity is what pins the real entries.
GMETRIX_APP_CONFIG = {
    "name": "GMetrix SMSe",
    "enabled": True,
    "title_id_env_var": "TITLE_EDITOR_GMETRIX_TITLE_ID",
    "version_source": {
        "type": "html_scrape",
        "url": "https://www.gmetrix.net/GetGMetrixSMS.aspx",
        "regex": "GMetrixSMSe-([0-9]+\\.[0-9]+\\.[0-9]+)-universal\\.dmg",
    },
    "version_pattern": "^\\d+\\.\\d+\\.\\d+$",
    "patch_template": {
        "enabled": True,
        "standalone": True,
        "reboot": False,
        "minimumOperatingSystem": "12.0",
        "killApps": [],
        "components": [
            {
                "name": "GMetrix SMSe",
                "version": "{version}",
                "criteria": [
                    {
                        "name": "Application Bundle ID",
                        "operator": "is",
                        "value": "com.skills.management.system.app",
                        "type": "recon",
                    },
                    {
                        "name": "Application Version",
                        "operator": "is",
                        "value": "{version}",
                        "type": "recon",
                    },
                ],
            }
        ],
        "capabilities": [
            {
                "name": "Operating System Version",
                "operator": "greater than",
                "value": "12.0",
                "type": "recon",
            }
        ],
    },
}


class TestGMetrixPatchBody(unittest.TestCase):

    def test_gmetrix_version_substitution(self):
        handler = _reload_handler()
        body = handler._build_patch_body("7.1.7", GMETRIX_APP_CONFIG)
        self.assertEqual(body["version"], "7.1.7")
        self.assertEqual(body["components"][0]["version"], "7.1.7")
        criteria_values = [c["value"] for c in body["components"][0]["criteria"]]
        self.assertIn("7.1.7", criteria_values)
        self.assertIn("com.skills.management.system.app", criteria_values)
        self.assertIn("releaseDate", body)

    def test_gmetrix_template_not_mutated(self):
        handler = _reload_handler()
        original = json.dumps(GMETRIX_APP_CONFIG["patch_template"])
        handler._build_patch_body("7.1.7", GMETRIX_APP_CONFIG)
        self.assertEqual(json.dumps(GMETRIX_APP_CONFIG["patch_template"]), original)


class TestMultiAppHandler(unittest.TestCase):

    def test_multi_app_both_updated(self):
        handler = _reload_handler()
        _disable_download_checks(self)
        os.environ["TITLE_EDITOR_GMETRIX_TITLE_ID"] = "99"

        chrome_config = handler.APPS[0]
        handler.APPS = [chrome_config, GMETRIX_APP_CONFIG]

        mock_ssm = MagicMock()
        mock_ssm.get_parameter.side_effect = [
            {"Parameter": {"Value": "testuser"}},
            {"Parameter": {"Value": "testpass"}},
        ]

        gmetrix_html = '<a href="GMetrixSMSe-7.1.7-universal.dmg">Download</a>'
        mock_opener = MagicMock()
        mock_opener.open.return_value = _make_response(gmetrix_html, raw=True)

        token_resp = _make_response({"token": "test-bearer-token"})
        chrome_version_resp = _make_response({"releases": [{"version": "149.0.7827.54", "fraction": 1}]})
        chrome_title_resp = _make_response({"currentVersion": "148.0.7778.216", "enabled": True, "patches": []})
        chrome_post_resp = _make_response({"patchId": 1})
        chrome_put_resp = _make_response({"currentVersion": "149.0.7827.54", "enabled": True})
        gmetrix_title_resp = _make_response({"currentVersion": "7.1.6", "enabled": True, "patches": []})
        gmetrix_post_resp = _make_response({"patchId": 2})
        gmetrix_put_resp = _make_response({"currentVersion": "7.1.7", "enabled": True})

        urlopen_responses = [
            token_resp,
            chrome_version_resp, chrome_title_resp, chrome_post_resp, chrome_put_resp,
            gmetrix_title_resp, gmetrix_post_resp, gmetrix_put_resp,
        ]

        with patch("handler.urllib.request.urlopen", side_effect=urlopen_responses):
            with patch("handler.urllib.request.build_opener", return_value=mock_opener):
                with patch("handler.boto3.client") as boto_mock:
                    boto_mock.side_effect = lambda svc: mock_ssm if svc == "ssm" else MagicMock()
                    result = handler.lambda_handler({}, None)

        self.assertEqual(result["statusCode"], 200)
        self.assertEqual(len(result["results"]), 2)
        self.assertEqual(result["results"][0]["app"], "Google Chrome")
        self.assertEqual(result["results"][0]["status"], "updated")
        self.assertEqual(result["results"][0]["to"], "149.0.7827.54")
        self.assertEqual(result["results"][1]["app"], "GMetrix SMSe")
        self.assertEqual(result["results"][1]["status"], "updated")
        self.assertEqual(result["results"][1]["to"], "7.1.7")

        handler.APPS = [chrome_config]

    def test_multi_app_gmetrix_current_skips(self):
        handler = _reload_handler()
        _disable_download_checks(self)
        os.environ["TITLE_EDITOR_GMETRIX_TITLE_ID"] = "99"

        handler.APPS = [GMETRIX_APP_CONFIG]

        mock_ssm = MagicMock()
        mock_ssm.get_parameter.side_effect = [
            {"Parameter": {"Value": "testuser"}},
            {"Parameter": {"Value": "testpass"}},
        ]

        gmetrix_html = '<a href="GMetrixSMSe-7.1.7-universal.dmg">Download</a>'
        mock_opener = MagicMock()
        mock_opener.open.return_value = _make_response(gmetrix_html, raw=True)

        token_resp = _make_response({"token": "test-bearer-token"})
        gmetrix_title_resp = _make_response({"currentVersion": "7.1.7", "enabled": True, "patches": []})

        with patch("handler.urllib.request.urlopen", side_effect=[token_resp, gmetrix_title_resp]):
            with patch("handler.urllib.request.build_opener", return_value=mock_opener):
                with patch("handler.boto3.client") as boto_mock:
                    boto_mock.side_effect = lambda svc: mock_ssm if svc == "ssm" else MagicMock()
                    result = handler.lambda_handler({}, None)

        self.assertEqual(result["results"][0]["status"], "current")
        self.assertEqual(result["results"][0]["version"], "7.1.7")


class TestContinueOnError(unittest.TestCase):

    def test_failing_app_does_not_starve_later_apps(self):
        handler = _reload_handler()
        _disable_download_checks(self)
        os.environ["TITLE_EDITOR_GMETRIX_TITLE_ID"] = "99"

        chrome_config = handler.APPS[0]
        handler.APPS = [chrome_config, GMETRIX_APP_CONFIG]

        mock_ssm = MagicMock()
        mock_ssm.get_parameter.side_effect = [
            {"Parameter": {"Value": "testuser"}},
            {"Parameter": {"Value": "testpass"}},
        ]

        # GMetrix (html_scrape) is reachable and already current at 7.1.7
        gmetrix_html = '<a href="GMetrixSMSe-7.1.7-universal.dmg">Download</a>'
        mock_opener = MagicMock()
        mock_opener.open.return_value = _make_response(gmetrix_html, raw=True)

        token_resp = _make_response({"token": "test-bearer-token"})
        # Chrome version fetch returns a malformed (3-part) version -> raises in _fetch_latest_version
        chrome_bad_version = _make_response({"releases": [{"version": "149.0.7827"}]})
        gmetrix_title_resp = _make_response({"currentVersion": "7.1.7", "enabled": True, "patches": []})

        with patch("handler.urllib.request.urlopen",
                   side_effect=[token_resp, chrome_bad_version, gmetrix_title_resp]) as urlopen_mock:
            with patch("handler.urllib.request.build_opener", return_value=mock_opener):
                with patch("handler.boto3.client") as boto_mock:
                    boto_mock.side_effect = lambda svc: mock_ssm if svc == "ssm" else MagicMock()
                    with self.assertRaises(RuntimeError) as ctx:
                        handler.lambda_handler({}, None)

        # Chrome failed, but GMetrix was still processed: its title GET ran (3rd urlopen call).
        # Old fail-fast behavior would abort after Chrome with only 2 urlopen calls.
        self.assertEqual(urlopen_mock.call_count, 3)
        self.assertIn("Google Chrome", str(ctx.exception))

        handler.APPS = [chrome_config]

    def test_missing_title_id_env_var_is_isolated(self):
        handler = _reload_handler()
        _disable_download_checks(self)
        app = dict(GMETRIX_APP_CONFIG, title_id_env_var="TITLE_EDITOR_DOES_NOT_EXIST")
        handler.APPS = [app]
        os.environ.pop("TITLE_EDITOR_DOES_NOT_EXIST", None)

        mock_ssm = MagicMock()
        mock_ssm.get_parameter.side_effect = [
            {"Parameter": {"Value": "testuser"}},
            {"Parameter": {"Value": "testpass"}},
        ]
        token_resp = _make_response({"token": "test-bearer-token"})

        with patch("handler.urllib.request.urlopen", side_effect=[token_resp]):
            with patch("handler.boto3.client") as boto_mock:
                boto_mock.side_effect = lambda svc: mock_ssm if svc == "ssm" else MagicMock()
                with self.assertRaises(RuntimeError) as ctx:
                    handler.lambda_handler({}, None)

        self.assertIn("GMetrix", str(ctx.exception))


class TestFetchVersionGitHubReleases(unittest.TestCase):

    def _make_github_app_config(self):
        return {
            "name": "MacAdmins Python",
            "version_source": {
                "type": "github_releases",
                "url": "https://api.github.com/repos/macadmins/python/releases/latest",
                "version_parts": 3,
            },
            "version_pattern": "^\\d+\\.\\d+\\.\\d+$",
        }

    def test_valid_github_release_strips_v_and_build(self):
        handler = _reload_handler()
        app_config = self._make_github_app_config()
        api_response = {"tag_name": "v3.14.5.80757", "name": "Python 3.14.5.80757"}
        with patch("handler.urllib.request.urlopen", return_value=_make_response(api_response)):
            version = handler._fetch_latest_version(app_config)
            self.assertEqual(version, "3.14.5")

    def test_github_release_without_v_prefix(self):
        handler = _reload_handler()
        app_config = self._make_github_app_config()
        api_response = {"tag_name": "3.14.5.80757", "name": "Python 3.14.5.80757"}
        with patch("handler.urllib.request.urlopen", return_value=_make_response(api_response)):
            version = handler._fetch_latest_version(app_config)
            self.assertEqual(version, "3.14.5")

    def test_github_release_full_version_when_no_parts(self):
        handler = _reload_handler()
        app_config = self._make_github_app_config()
        app_config["version_source"].pop("version_parts")
        app_config["version_pattern"] = "^\\d+\\.\\d+\\.\\d+\\.\\d+$"
        api_response = {"tag_name": "v3.14.5.80757"}
        with patch("handler.urllib.request.urlopen", return_value=_make_response(api_response)):
            version = handler._fetch_latest_version(app_config)
            self.assertEqual(version, "3.14.5.80757")

    def test_github_release_no_tag_raises(self):
        handler = _reload_handler()
        app_config = self._make_github_app_config()
        api_response = {"name": "Some Release"}
        with patch("handler.urllib.request.urlopen", return_value=_make_response(api_response)):
            with self.assertRaises(ValueError) as ctx:
                handler._fetch_latest_version(app_config)
            self.assertIn("No tag_name", str(ctx.exception))

    def test_github_release_pattern_mismatch_raises(self):
        handler = _reload_handler()
        app_config = self._make_github_app_config()
        app_config["version_pattern"] = "^\\d+\\.\\d+$"
        api_response = {"tag_name": "v3.14.5.80757"}
        with patch("handler.urllib.request.urlopen", return_value=_make_response(api_response)):
            with self.assertRaises(ValueError) as ctx:
                handler._fetch_latest_version(app_config)
            self.assertIn("does not match", str(ctx.exception))


class TestFetchVersionRedirect(unittest.TestCase):

    def _make_redirect_app_config(self):
        return {
            "name": "Washington Secure Browser",
            "version_source": {
                "type": "redirect_filename",
                "url": "https://sb.portal.cambiumast.com/geturls?clientName=washington&operatingSystem=macOS",
                "regex": "WASecureBrowser([0-9]+\\.[0-9]+)-[0-9-]+-universal-signed\\.dmg",
            },
            "version_pattern": "^\\d+\\.\\d+$",
        }

    def _redirect_error(self, location):
        import email.message
        import urllib.error
        hdrs = email.message.Message()
        hdrs["Location"] = location
        return urllib.error.HTTPError(
            url="https://sb.portal.cambiumast.com/geturls",
            code=301, msg="Moved Permanently", hdrs=hdrs, fp=io.BytesIO(b"")
        )

    def test_valid_redirect_filename_version(self):
        handler = _reload_handler()
        app_config = self._make_redirect_app_config()
        location = ("https://cai-sb-prod.s3.amazonaws.com/washington/secureBrowsers/SB2022/"
                    "WASecureBrowser18.0-2025-05-22-universal-signed.dmg"
                    "?response-content-disposition=attachment&AWSAccessKeyId=x&Signature=y")
        mock_opener = MagicMock()
        mock_opener.open.side_effect = self._redirect_error(location)
        with patch("handler.urllib.request.build_opener", return_value=mock_opener):
            version = handler._fetch_latest_version(app_config)
            self.assertEqual(version, "18.0")

    def test_redirect_no_matching_filename_raises(self):
        handler = _reload_handler()
        app_config = self._make_redirect_app_config()
        mock_opener = MagicMock()
        mock_opener.open.side_effect = self._redirect_error("https://example.com/SomethingElse.dmg")
        with patch("handler.urllib.request.build_opener", return_value=mock_opener):
            with self.assertRaises(ValueError) as ctx:
                handler._fetch_latest_version(app_config)
            self.assertIn("No version found", str(ctx.exception))


class TestTransientRetries(unittest.TestCase):
    """A single transient failure (5xx, timeout) on an idempotent call must not
    fail the run; exhausted retries must name the exact request that failed."""

    def setUp(self):
        self.handler = _reload_handler()
        sleep_patch = patch("time.sleep")
        sleep_patch.start()
        self.addCleanup(sleep_patch.stop)

    def _github_config(self):
        return {
            "name": "Outset",
            "version_source": {
                "type": "github_releases",
                "url": "https://api.github.com/repos/macadmins/outset/releases/latest",
            },
            "version_pattern": "^\\d+\\.\\d+\\.\\d+\\.\\d+$",
        }

    def _http_error(self, code, msg, url="https://api.github.com/repos/macadmins/outset/releases/latest"):
        import urllib.error
        return urllib.error.HTTPError(url=url, code=code, msg=msg, hdrs={}, fp=io.BytesIO(b""))

    def test_version_fetch_retries_503_then_succeeds(self):
        ok = _make_response({"tag_name": "v4.3.0.22031"})
        responses = [self._http_error(503, "Service Unavailable"), ok]
        with patch("handler.urllib.request.urlopen", side_effect=responses) as urlopen_mock:
            version = self.handler._fetch_latest_version(self._github_config())
        self.assertEqual(version, "4.3.0.22031")
        self.assertEqual(urlopen_mock.call_count, 2)

    def test_version_fetch_exhausted_retries_name_the_url(self):
        responses = [
            self._http_error(503, "Service Unavailable"),
            self._http_error(503, "Service Unavailable"),
        ]
        with patch("handler.urllib.request.urlopen", side_effect=responses):
            with self.assertRaises(RuntimeError) as ctx:
                self.handler._fetch_latest_version(self._github_config())
        msg = str(ctx.exception)
        self.assertIn("https://api.github.com/repos/macadmins/outset/releases/latest", msg)
        self.assertIn("503", msg)
        self.assertIn("2 attempts", msg)

    def test_version_fetch_404_not_retried(self):
        with patch("handler.urllib.request.urlopen", side_effect=[self._http_error(404, "Not Found")]) as urlopen_mock:
            with self.assertRaises(RuntimeError) as ctx:
                self.handler._fetch_latest_version(self._github_config())
        self.assertEqual(urlopen_mock.call_count, 1)
        self.assertIn("404", str(ctx.exception))

    def test_read_timeout_retried_then_succeeds(self):
        ok = _make_response({"tag_name": "v4.3.0.22031"})
        responses = [TimeoutError("The read operation timed out"), ok]
        with patch("handler.urllib.request.urlopen", side_effect=responses) as urlopen_mock:
            version = self.handler._fetch_latest_version(self._github_config())
        self.assertEqual(version, "4.3.0.22031")
        self.assertEqual(urlopen_mock.call_count, 2)

    def test_title_editor_auth_retries_503_then_succeeds(self):
        ok = _make_response({"token": "te-token"})
        err = self._http_error(503, "Service Unavailable",
                               url="https://test.appcatalog.jamfcloud.com/v2/auth/tokens")
        with patch("handler.urllib.request.urlopen", side_effect=[err, ok]) as urlopen_mock:
            token = self.handler._get_title_editor_token(
                "https://test.appcatalog.jamfcloud.com", "u", "p"
            )
        self.assertEqual(token, "te-token")
        self.assertEqual(urlopen_mock.call_count, 2)

    def test_title_info_failure_names_title_editor(self):
        err_url = "https://test.appcatalog.jamfcloud.com/v2/softwaretitles/9"
        responses = [
            self._http_error(503, "Service Unavailable", url=err_url),
            self._http_error(503, "Service Unavailable", url=err_url),
        ]
        with patch("handler.urllib.request.urlopen", side_effect=responses):
            with self.assertRaises(RuntimeError) as ctx:
                self.handler._get_title_info(
                    "https://test.appcatalog.jamfcloud.com", "tok", "9"
                )
        msg = str(ctx.exception)
        self.assertIn("Title Editor", msg)
        self.assertIn("/v2/softwaretitles/9", msg)

    def test_redirect_fetch_retries_503_then_reads_location(self):
        import email.message
        import urllib.error
        hdrs = email.message.Message()
        hdrs["Location"] = ("https://cai-sb-prod.s3.amazonaws.com/washington/secureBrowsers/"
                            "SB2022/WASecureBrowser18.0-2025-05-22-universal-signed.dmg")
        redirect = urllib.error.HTTPError(
            url="https://sb.portal.cambiumast.com/geturls",
            code=301, msg="Moved Permanently", hdrs=hdrs, fp=io.BytesIO(b""),
        )
        app_config = {
            "name": "Washington Secure Browser",
            "version_source": {
                "type": "redirect_filename",
                "url": "https://sb.portal.cambiumast.com/geturls?clientName=washington&operatingSystem=macOS",
                "regex": "WASecureBrowser([0-9]+\\.[0-9]+)-[0-9-]+-universal-signed\\.dmg",
            },
            "version_pattern": "^\\d+\\.\\d+$",
        }
        mock_opener = MagicMock()
        mock_opener.open.side_effect = [
            self._http_error(503, "Service Unavailable", url="https://sb.portal.cambiumast.com/geturls"),
            redirect,
        ]
        with patch("handler.urllib.request.build_opener", return_value=mock_opener):
            version = self.handler._fetch_latest_version(app_config)
        self.assertEqual(version, "18.0")
        self.assertEqual(mock_opener.open.call_count, 2)


class TestMacAdminsPythonPatchBody(unittest.TestCase):

    def test_ea_based_criteria_version_substitution(self):
        handler = _reload_handler()
        app_config = {
            "name": "MacAdmins Python",
            "patch_template": {
                "enabled": True,
                "standalone": True,
                "reboot": False,
                "minimumOperatingSystem": "12.0",
                "killApps": [],
                "components": [
                    {
                        "name": "MacAdmins Python",
                        "version": "{version}",
                        "criteria": [
                            {
                                "name": "patch-macadmins-python",
                                "operator": "is",
                                "value": "{version}",
                                "type": "extensionAttribute",
                            }
                        ],
                    }
                ],
                "capabilities": [
                    {
                        "name": "Operating System Version",
                        "operator": "greater than",
                        "value": "12.0",
                        "type": "recon",
                    }
                ],
            },
        }
        body = handler._build_patch_body("3.14.5.80757", app_config)
        self.assertEqual(body["version"], "3.14.5.80757")
        self.assertEqual(body["components"][0]["version"], "3.14.5.80757")
        criteria = body["components"][0]["criteria"][0]
        self.assertEqual(criteria["value"], "3.14.5.80757")
        self.assertEqual(criteria["type"], "extensionAttribute")
        self.assertEqual(criteria["name"], "patch-macadmins-python")


class TestFetchVersionPrometheanScreenShare(unittest.TestCase):

    def _make_promethean_app_config(self):
        return {
            "name": "Promethean Screen Share",
            "version_source": {
                "type": "html_scrape",
                "url": "https://share.one.prometheanworld.com/config.js",
                "regex": "VERSION:\\s*'([0-9]+\\.[0-9]+\\.[0-9]+)",
            },
            "version_pattern": "^\\d+\\.\\d+\\.\\d+$",
        }

    def test_valid_config_js_version(self):
        handler = _reload_handler()
        app_config = self._make_promethean_app_config()
        config_js = "let CONFIG = {\n  VERSION: '4.4.0.234',\n  BRANCH: ''\n}"
        mock_opener = MagicMock()
        mock_opener.open.return_value = _make_response(config_js, raw=True)
        with patch("handler.urllib.request.build_opener", return_value=mock_opener):
            version = handler._fetch_latest_version(app_config)
            self.assertEqual(version, "4.4.0")

    def test_config_js_no_version_raises(self):
        handler = _reload_handler()
        app_config = self._make_promethean_app_config()
        config_js = "let CONFIG = {\n  BRANCH: ''\n}"
        mock_opener = MagicMock()
        mock_opener.open.return_value = _make_response(config_js, raw=True)
        with patch("handler.urllib.request.build_opener", return_value=mock_opener):
            with self.assertRaises(ValueError) as ctx:
                handler._fetch_latest_version(app_config)
            self.assertIn("No version found", str(ctx.exception))


PROMETHEAN_APP_CONFIG = {
    "name": "Promethean Screen Share",
    "enabled": True,
    "title_id_env_var": "TITLE_EDITOR_PROMETHEAN_SCREEN_SHARE_TITLE_ID",
    "version_source": {
        "type": "html_scrape",
        "url": "https://share.one.prometheanworld.com/config.js",
        "regex": "VERSION:\\s*'([0-9]+\\.[0-9]+\\.[0-9]+)",
    },
    "version_pattern": "^\\d+\\.\\d+\\.\\d+$",
    "patch_template": {
        "enabled": True,
        "standalone": True,
        "reboot": False,
        "minimumOperatingSystem": "12.0",
        "killApps": [],
        "components": [
            {
                "name": "Promethean Screen Share",
                "version": "{version}",
                "criteria": [
                    {
                        "name": "Application Bundle ID",
                        "operator": "is",
                        "value": "cn.com.nd.pmcast",
                        "type": "recon",
                    },
                    {
                        "name": "Application Version",
                        "operator": "is",
                        "value": "{version}",
                        "type": "recon",
                    },
                ],
            }
        ],
        "capabilities": [
            {
                "name": "Operating System Version",
                "operator": "greater than",
                "value": "12.0",
                "type": "recon",
            }
        ],
    },
}


class TestPrometheanPatchBody(unittest.TestCase):

    def test_promethean_version_substitution(self):
        handler = _reload_handler()
        body = handler._build_patch_body("4.4.0", PROMETHEAN_APP_CONFIG)
        self.assertEqual(body["version"], "4.4.0")
        self.assertEqual(body["components"][0]["version"], "4.4.0")
        criteria_values = [c["value"] for c in body["components"][0]["criteria"]]
        self.assertIn("4.4.0", criteria_values)
        self.assertIn("cn.com.nd.pmcast", criteria_values)
        self.assertIn("releaseDate", body)

    def test_promethean_template_not_mutated(self):
        handler = _reload_handler()
        original = json.dumps(PROMETHEAN_APP_CONFIG["patch_template"])
        handler._build_patch_body("4.4.0", PROMETHEAN_APP_CONFIG)
        self.assertEqual(json.dumps(PROMETHEAN_APP_CONFIG["patch_template"]), original)


class TestAdobeCanary(unittest.TestCase):

    def test_adobe_ccd_download_url_macarm64(self):
        handler = _reload_handler()
        ccdconfig = '{"feature": "greenline.latest", "version": "6.10.0.252.3"}'
        with patch("handler._http_get_text", return_value=ccdconfig):
            version, url = handler._adobe_ccd_download_url("macarm64")
        self.assertEqual(version, "6.10.0.252.3")
        self.assertEqual(
            url,
            "https://ccmdls.adobe.com/AdobeProducts/StandaloneBuilds/ACCC/ESD/"
            "6.10.0/252.3/macarm64/ACCCx6_10_0_252_3.dmg",
        )

    def test_adobe_ccd_download_url_osx10(self):
        handler = _reload_handler()
        ccdconfig = '"greenline.latest"\n    "version": "6.10.0.252.3"'
        with patch("handler._http_get_text", return_value=ccdconfig):
            version, url = handler._adobe_ccd_download_url("osx10")
        self.assertEqual(version, "6.10.0.252.3")
        self.assertIn("/osx10/ACCCx6_10_0_252_3.dmg", url)

    def test_adobe_ccd_config_missing_version_raises(self):
        handler = _reload_handler()
        with patch("handler._http_get_text", return_value="<xml>nothing useful</xml>"):
            with self.assertRaises(ValueError) as ctx:
                handler._adobe_ccd_download_url("macarm64")
            self.assertIn("greenline.latest", str(ctx.exception))

    def test_url_is_live_partial_content(self):
        handler = _reload_handler()
        with patch("handler.urllib.request.urlopen", return_value=_make_response({}, status=206)):
            self.assertTrue(handler._url_is_live("https://ccmdls.adobe.com/x.dmg"))

    def test_url_is_live_404(self):
        handler = _reload_handler()
        import urllib.error
        err = urllib.error.HTTPError("https://x", 404, "Not Found", {}, io.BytesIO(b""))
        with patch("handler.urllib.request.urlopen", side_effect=err):
            self.assertFalse(handler._url_is_live("https://ccmdls.adobe.com/x.dmg"))

    def test_url_is_live_network_error(self):
        handler = _reload_handler()
        import urllib.error
        with patch("time.sleep"):
            with patch("handler.urllib.request.urlopen",
                       side_effect=urllib.error.URLError("boom")):
                self.assertFalse(handler._url_is_live("https://ccmdls.adobe.com/x.dmg"))

    def test_run_download_checks_all_healthy(self):
        handler = _reload_handler()
        with patch("handler._adobe_ccd_download_url", return_value=("6.10.0.252.3", "https://x/y.dmg")):
            with patch("handler._url_is_live", return_value=True):
                self.assertEqual(handler._run_download_checks(), [])

    def test_run_download_checks_dead_url_reports_each_arch(self):
        handler = _reload_handler()
        with patch("handler._adobe_ccd_download_url", return_value=("6.10.0.252.3", "https://x/y.dmg")):
            with patch("handler._url_is_live", return_value=False):
                failures = handler._run_download_checks()
        self.assertEqual(len(failures), 2)
        self.assertTrue(any("macarm64" in f for f in failures))
        self.assertTrue(any("osx10" in f for f in failures))

    def test_run_download_checks_fetch_error_captured(self):
        handler = _reload_handler()
        with patch("handler._adobe_ccd_download_url", side_effect=ValueError("ccdConfig broke")):
            failures = handler._run_download_checks()
        self.assertEqual(len(failures), 2)
        self.assertTrue(all("ccdConfig broke" in f for f in failures))

    def test_lambda_raises_when_download_check_fails(self):
        # All apps healthy, but a dead download URL must still fail the run at the end.
        handler = _reload_handler()
        handler.APPS = [a for a in handler.APPS if a["name"] == "Google Chrome"]

        mock_ssm = MagicMock()
        mock_ssm.get_parameter.side_effect = [
            {"Parameter": {"Value": "testuser"}},
            {"Parameter": {"Value": "testpass"}},
        ]
        token_resp = _make_response({"token": "test-bearer-token"})
        version_resp = _make_response({"releases": [{"version": "149.0.7827.54", "fraction": 1}]})
        title_resp = _make_response({"currentVersion": "149.0.7827.54", "enabled": True, "patches": []})

        with patch("handler.urllib.request.urlopen", side_effect=[token_resp, version_resp, title_resp]):
            with patch("handler._run_download_checks", return_value=["Adobe CC macarm64: download URL not live"]):
                with patch("handler.boto3.client") as boto_mock:
                    boto_mock.side_effect = lambda svc: mock_ssm if svc == "ssm" else MagicMock()
                    with self.assertRaises(RuntimeError) as ctx:
                        handler.lambda_handler({}, None)
        self.assertIn("download", str(ctx.exception).lower())

    def test_lambda_emits_download_failure_metric_when_healthy(self):
        handler = _reload_handler()
        handler.APPS = [a for a in handler.APPS if a["name"] == "Google Chrome"]

        mock_ssm = MagicMock()
        mock_ssm.get_parameter.side_effect = [
            {"Parameter": {"Value": "testuser"}},
            {"Parameter": {"Value": "testpass"}},
        ]
        token_resp = _make_response({"token": "test-bearer-token"})
        version_resp = _make_response({"releases": [{"version": "149.0.7827.54", "fraction": 1}]})
        title_resp = _make_response({"currentVersion": "149.0.7827.54", "enabled": True, "patches": []})
        cloudwatch = MagicMock()

        with patch("handler.urllib.request.urlopen", side_effect=[token_resp, version_resp, title_resp]):
            with patch("handler._run_download_checks", return_value=[]):
                with patch("handler.boto3.client") as boto_mock:
                    boto_mock.side_effect = lambda svc: mock_ssm if svc == "ssm" else cloudwatch
                    result = handler.lambda_handler({}, None)

        self.assertEqual(result["statusCode"], 200)
        metric_calls = [
            c for c in cloudwatch.put_metric_data.call_args_list
            if c.kwargs["MetricData"][0]["MetricName"] == "DownloadUrlCheckFailures"
        ]
        self.assertEqual(len(metric_calls), 1)
        self.assertEqual(metric_calls[0].kwargs["MetricData"][0]["Value"], 0)


class TestFetchVersionOutset(unittest.TestCase):

    def _outset_config(self, handler):
        return next(a for a in handler.APPS if a["name"] == "Outset")

    def test_full_four_part_version_from_tag(self):
        handler = _reload_handler()
        app_config = self._outset_config(handler)
        api_response = {"tag_name": "v4.3.0.22031", "name": "Outset 4.3.0.22031"}
        with patch("handler.urllib.request.urlopen", return_value=_make_response(api_response)):
            version = handler._fetch_latest_version(app_config)
            self.assertEqual(version, "4.3.0.22031")

    def test_three_part_tag_rejected_by_pattern(self):
        handler = _reload_handler()
        app_config = self._outset_config(handler)
        api_response = {"tag_name": "v4.3.0"}
        with patch("handler.urllib.request.urlopen", return_value=_make_response(api_response)):
            with self.assertRaises(ValueError) as ctx:
                handler._fetch_latest_version(app_config)
            self.assertIn("does not match", str(ctx.exception))


class TestOutsetPatchBody(unittest.TestCase):

    def test_ea_criteria_version_substitution(self):
        handler = _reload_handler()
        app_config = next(a for a in handler.APPS if a["name"] == "Outset")
        body = handler._build_patch_body("4.3.0.22031", app_config)
        self.assertEqual(body["version"], "4.3.0.22031")
        self.assertEqual(body["components"][0]["version"], "4.3.0.22031")
        criteria = body["components"][0]["criteria"][0]
        self.assertEqual(criteria["value"], "4.3.0.22031")
        self.assertEqual(criteria["type"], "extensionAttribute")
        self.assertEqual(criteria["name"], "patch-outset")
        self.assertIn("releaseDate", body)


class TestFetchVersionUtiluti(unittest.TestCase):

    def _utiluti_config(self, handler):
        return next(a for a in handler.APPS if a["name"] == "utiluti")

    def test_two_part_version_from_tag(self):
        handler = _reload_handler()
        app_config = self._utiluti_config(handler)
        api_response = {"tag_name": "v1.5", "name": "utiluti 1.5"}
        with patch("handler.urllib.request.urlopen", return_value=_make_response(api_response)):
            version = handler._fetch_latest_version(app_config)
            self.assertEqual(version, "1.5")

    def test_malformed_tag_rejected_by_pattern(self):
        handler = _reload_handler()
        app_config = self._utiluti_config(handler)
        api_response = {"tag_name": "v1"}
        with patch("handler.urllib.request.urlopen", return_value=_make_response(api_response)):
            with self.assertRaises(ValueError) as ctx:
                handler._fetch_latest_version(app_config)
            self.assertIn("does not match", str(ctx.exception))


class TestUtilutiPatchBody(unittest.TestCase):

    def test_ea_criteria_version_substitution(self):
        handler = _reload_handler()
        app_config = next(a for a in handler.APPS if a["name"] == "utiluti")
        body = handler._build_patch_body("1.5", app_config)
        self.assertEqual(body["version"], "1.5")
        self.assertEqual(body["components"][0]["version"], "1.5")
        criteria = body["components"][0]["criteria"][0]
        self.assertEqual(criteria["value"], "1.5")
        self.assertEqual(criteria["type"], "extensionAttribute")
        self.assertEqual(criteria["name"], "patch-utiluti")
        self.assertIn("releaseDate", body)


class TestFetchVersionDymoConnect(unittest.TestCase):

    def _dymo_config(self, handler):
        return next(a for a in handler.APPS if a["name"] == "DYMO Connect")

    def test_valid_version_from_updates_xml(self):
        handler = _reload_handler()
        app_config = self._dymo_config(handler)
        updates_xml = (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            "<Updates>\n\t<DYMOConnect>\n"
            "\t\t<ProductName>DYMO Connect</ProductName>\n"
            "\t\t<UpdateName>DYMO Connect v1.6.0.41</UpdateName>\n"
            "\t\t<Version>1.6.0.41</Version>\n"
            "\t\t<DownloadURL>https://dymoreleasecontent.blob.core.windows.net/"
            "dymo-release/DCDMAC/DCDMac1.6.0.41.pkg</DownloadURL>\n"
            "\t</DYMOConnect>\n</Updates>"
        )
        mock_opener = MagicMock()
        mock_opener.open.return_value = _make_response(updates_xml, raw=True)
        with patch("handler.urllib.request.build_opener", return_value=mock_opener):
            version = handler._fetch_latest_version(app_config)
            self.assertEqual(version, "1.6.0.41")

    def test_updates_xml_no_version_raises(self):
        handler = _reload_handler()
        app_config = self._dymo_config(handler)
        updates_xml = (
            "<Updates>\n\t<DYMOConnect>\n"
            "\t\t<ProductName>DYMO Connect</ProductName>\n"
            "\t</DYMOConnect>\n</Updates>"
        )
        mock_opener = MagicMock()
        mock_opener.open.return_value = _make_response(updates_xml, raw=True)
        with patch("handler.urllib.request.build_opener", return_value=mock_opener):
            with self.assertRaises(ValueError) as ctx:
                handler._fetch_latest_version(app_config)
            self.assertIn("No version found", str(ctx.exception))


class TestDymoConnectPatchBody(unittest.TestCase):

    def _dymo_config(self, handler):
        return next(a for a in handler.APPS if a["name"] == "DYMO Connect")

    def test_dymo_version_substitution(self):
        handler = _reload_handler()
        app_config = self._dymo_config(handler)
        body = handler._build_patch_body("1.6.0.41", app_config)
        self.assertEqual(body["version"], "1.6.0.41")
        self.assertEqual(body["components"][0]["version"], "1.6.0.41")
        criteria = body["components"][0]["criteria"]
        criteria_values = [c["value"] for c in criteria]
        self.assertIn("1.6.0.41", criteria_values)
        self.assertIn("com.dymo.dymo-connect", criteria_values)
        self.assertEqual({c["type"] for c in criteria}, {"recon"})
        self.assertIn("releaseDate", body)

    def test_dymo_template_not_mutated(self):
        handler = _reload_handler()
        app_config = self._dymo_config(handler)
        original = json.dumps(app_config["patch_template"])
        handler._build_patch_body("1.6.0.41", app_config)
        self.assertEqual(json.dumps(app_config["patch_template"]), original)


class TestBuildFailureAlert(unittest.TestCase):

    def test_subject_and_body_carry_failure_detail(self):
        handler = _reload_handler()
        results = [
            {"app": "Google Chrome", "status": "current", "version": "150.0.7871.115"},
            {"app": "GMetrix SMSe", "status": "failed", "error": "No version found for GMetrix SMSe"},
            {"app": "Outset", "status": "updated", "from": "4.2.0", "to": "4.3.0.22031"},
        ]
        subject, body = handler._build_failure_alert(
            results, ["GMetrix SMSe"], [], "req-123"
        )
        self.assertIn("FAILED", subject)
        self.assertIn("GMetrix SMSe", subject)
        self.assertIn("GMetrix SMSe: No version found for GMetrix SMSe", body)
        self.assertIn("Google Chrome: current at 150.0.7871.115", body)
        self.assertIn("Outset: updated 4.2.0 -> 4.3.0.22031", body)
        self.assertIn("req-123", body)
        self.assertIn("$252Faws$252Flambda$252Fjamf-title-editor-sync", body)

    def test_subject_stays_under_sns_limit(self):
        handler = _reload_handler()
        names = [f"Application Number {i}" for i in range(12)]
        results = [{"app": n, "status": "failed", "error": "boom"} for n in names]
        subject, _ = handler._build_failure_alert(results, names, [], "req-123")
        self.assertLessEqual(len(subject), 100)

    def test_download_only_failure_named_in_subject(self):
        handler = _reload_handler()
        detail = "Adobe CC macarm64: download URL not live (6.10.0) https://x/y.dmg"
        results = [
            {"app": "Google Chrome", "status": "current", "version": "150.0.7871.115"},
            {"check": "download_url", "status": "failed", "detail": detail},
        ]
        subject, body = handler._build_failure_alert(results, [], [detail], "req-123")
        self.assertIn("download URL checks", subject)
        self.assertIn(detail, body)

    def test_drift_failure_names_call_and_separates_app_syncs(self):
        handler = _reload_handler()
        detail = ("Jamf Pro drift check: GET https://jss.example.com:8443/api/v2/"
                  "patch-software-title-configurations (45s timeout): The read operation timed out")
        results = [
            {"app": f"App {i}", "status": "current", "version": "1.0"} for i in range(8)
        ] + [{"check": "jamfpro_drift", "status": "failed", "detail": detail}]
        _, body = handler._build_failure_alert(results, ["Jamf Pro drift check"], [], "req-9")
        self.assertIn(detail, body)
        self.assertIn("App syncs - all 8 OK:", body)
        self.assertIn("not an app sync", body)

    def test_partial_app_failures_counted_in_ok_header(self):
        handler = _reload_handler()
        results = (
            [{"app": f"App {i}", "status": "current", "version": "1.0"} for i in range(5)]
            + [{"app": "Bad App", "status": "failed", "error": "HTTP Error 503: Service Unavailable"}]
            + [{"check": "jamfpro_drift", "status": "failed", "detail": "Jamf Pro drift check: boom"}]
        )
        _, body = handler._build_failure_alert(
            results, ["Bad App", "Jamf Pro drift check"], [], "req-9"
        )
        self.assertIn("App syncs OK (5 of 6):", body)

    def test_no_app_results_omits_app_sync_section(self):
        handler = _reload_handler()
        results = [{"check": "auth", "status": "failed", "detail": "Title Editor auth: HTTP Error 503"}]
        _, body = handler._build_failure_alert(results, ["Title Editor auth"], [], "req-9")
        self.assertIn("Title Editor auth", body)
        self.assertNotIn("App syncs", body)


class TestFailureAlertPublish(unittest.TestCase):

    TOPIC = "arn:aws:sns:us-west-2:111122223333:test-alerts"

    def setUp(self):
        self.handler = _reload_handler()
        _disable_download_checks(self)
        self._apps = self.handler.APPS
        self.handler.APPS = [a for a in self.handler.APPS if a["name"] == "Google Chrome"]
        os.environ["ALERT_TOPIC_ARN"] = self.TOPIC
        self.addCleanup(os.environ.pop, "ALERT_TOPIC_ARN", None)

        self.mock_ssm = MagicMock()
        self.mock_ssm.get_parameter.side_effect = [
            {"Parameter": {"Value": "testuser"}},
            {"Parameter": {"Value": "testpass"}},
        ]
        self.mock_sns = MagicMock()

    def tearDown(self):
        self.handler.APPS = self._apps

    def _boto_router(self):
        def route(svc):
            if svc == "ssm":
                return self.mock_ssm
            if svc == "sns":
                return self.mock_sns
            return MagicMock()
        return route

    def _failing_run_responses(self):
        token_resp = _make_response({"token": "test-bearer-token"})
        bad_version = _make_response({"releases": [{"version": "149.0.7827"}]})
        return [token_resp, bad_version]

    def test_failed_run_publishes_detail_and_still_raises(self):
        ctx_obj = types.SimpleNamespace(aws_request_id="req-e2e-42")
        with patch("handler.urllib.request.urlopen", side_effect=self._failing_run_responses()):
            with patch("handler.boto3.client", side_effect=self._boto_router()):
                with self.assertRaises(RuntimeError):
                    self.handler.lambda_handler({}, ctx_obj)

        self.mock_sns.publish.assert_called_once()
        kwargs = self.mock_sns.publish.call_args.kwargs
        self.assertEqual(kwargs["TopicArn"], self.TOPIC)
        self.assertIn("Google Chrome", kwargs["Subject"])
        self.assertIn("does not match", kwargs["Message"])
        self.assertIn("req-e2e-42", kwargs["Message"])

    def test_missing_topic_env_var_skips_publish_and_still_raises(self):
        os.environ.pop("ALERT_TOPIC_ARN", None)
        with patch("handler.urllib.request.urlopen", side_effect=self._failing_run_responses()):
            with patch("handler.boto3.client", side_effect=self._boto_router()) as boto_mock:
                with self.assertRaises(RuntimeError):
                    self.handler.lambda_handler({}, None)

        requested_services = [c.args[0] for c in boto_mock.call_args_list]
        self.assertNotIn("sns", requested_services)

    def test_publish_error_is_logged_and_does_not_mask_run_failure(self):
        self.mock_sns.publish.side_effect = Exception("sns is down")
        with patch("handler.urllib.request.urlopen", side_effect=self._failing_run_responses()):
            with patch("handler.boto3.client", side_effect=self._boto_router()):
                with self.assertLogs(level="ERROR") as logs:
                    with self.assertRaises(RuntimeError) as ctx:
                        self.handler.lambda_handler({}, None)

        self.assertIn("Google Chrome", str(ctx.exception))
        self.assertTrue(any("failure alert" in m for m in logs.output))

    def test_clean_run_does_not_publish(self):
        token_resp = _make_response({"token": "test-bearer-token"})
        version_resp = _make_response({"releases": [{"version": "149.0.7827.54", "fraction": 1}]})
        title_resp = _make_response({"currentVersion": "149.0.7827.54", "enabled": True, "patches": []})
        with patch("handler.urllib.request.urlopen", side_effect=[token_resp, version_resp, title_resp]):
            with patch("handler.boto3.client", side_effect=self._boto_router()) as boto_mock:
                result = self.handler.lambda_handler({}, None)

        self.assertEqual(result["statusCode"], 200)
        requested_services = [c.args[0] for c in boto_mock.call_args_list]
        self.assertNotIn("sns", requested_services)

    def test_te_auth_failure_publishes_detail_and_raises(self):
        import urllib.error
        def auth_503():
            return urllib.error.HTTPError(
                "https://test.appcatalog.jamfcloud.com/v2/auth/tokens", 503,
                "Service Unavailable", {}, io.BytesIO(b""),
            )
        ctx_obj = types.SimpleNamespace(aws_request_id="req-auth-7")
        with patch("handler.urllib.request.urlopen", side_effect=[auth_503(), auth_503()]):
            with patch("handler.boto3.client", side_effect=self._boto_router()):
                with patch("time.sleep"):
                    with self.assertRaises(Exception):
                        self.handler.lambda_handler({}, ctx_obj)

        self.mock_sns.publish.assert_called_once()
        kwargs = self.mock_sns.publish.call_args.kwargs
        self.assertIn("Title Editor auth", kwargs["Subject"])
        self.assertIn("/v2/auth/tokens", kwargs["Message"])
        self.assertIn("503", kwargs["Message"])
        self.assertIn("req-auth-7", kwargs["Message"])


JAMF_PRO_V2_CONFIGS = [
    {"id": "201", "displayName": "Google Chrome", "jamfOfficial": False, "softwareTitleId": "201"},
    {"id": "205", "displayName": "ScreenConnect Agent", "jamfOfficial": False, "softwareTitleId": "205"},
    {"id": "150", "displayName": "Zoom Client for Meetings", "jamfOfficial": True, "softwareTitleId": "0DF"},
]

JAMF_PRO_XML_CHROME = """<?xml version="1.0" encoding="UTF-8"?>
<patch_software_title>
  <id>201</id>
  <name>Google Chrome</name>
  <name_id>googlechrome</name_id>
  <source_id>2</source_id>
  <versions>
    <version><software_version>150.0.7871.115</software_version></version>
    <version><software_version>150.0.7871.101</software_version></version>
  </versions>
</patch_software_title>"""

JAMF_PRO_XML_SCREENCONNECT = """<?xml version="1.0" encoding="UTF-8"?>
<patch_software_title>
  <id>205</id>
  <name>ScreenConnect Agent</name>
  <name_id>screenconnectclient</name_id>
  <source_id>2</source_id>
  <versions>
    <version><software_version>26.1.24.9579</software_version></version>
  </versions>
</patch_software_title>"""


class TestJamfProDriftCheck(unittest.TestCase):

    def setUp(self):
        self.handler = _reload_handler()
        os.environ["JAMF_PRO_URL"] = "https://jamf.test:8443"
        os.environ["JAMF_PRO_SECRET_ID"] = "test/jamf-pro-readonly"
        self.addCleanup(os.environ.pop, "JAMF_PRO_URL", None)
        self.addCleanup(os.environ.pop, "JAMF_PRO_SECRET_ID", None)

        self.mock_secrets = MagicMock()
        self.mock_secrets.get_secret_value.return_value = {
            "SecretString": json.dumps({"client_id": "cid", "client_secret": "csec"})
        }

    def _drift_responses(self, xml_bodies):
        token_resp = _make_response({"access_token": "jp-token", "expires_in": 1200})
        v2_resp = _make_response(JAMF_PRO_V2_CONFIGS)
        return [token_resp, v2_resp] + [_make_response(x, raw=True) for x in xml_bodies]

    def test_synced_titles_report_no_drift(self):
        te_state = {
            "googlechrome": {"app": "Google Chrome", "version": "150.0.7871.115"},
            "screenconnectclient": {"app": "ScreenConnect Client", "version": "26.1.24.9579"},
        }
        responses = self._drift_responses([JAMF_PRO_XML_CHROME, JAMF_PRO_XML_SCREENCONNECT])
        with patch("handler.urllib.request.urlopen", side_effect=responses):
            with patch("handler.boto3.client", return_value=self.mock_secrets):
                drifted = self.handler._run_jamf_pro_drift_check(te_state)
        self.assertEqual(drifted, [])

    def test_diverged_title_reported_with_both_versions(self):
        te_state = {
            "googlechrome": {"app": "Google Chrome", "version": "150.0.7871.115"},
            "screenconnectclient": {"app": "ScreenConnect Client", "version": "26.3.11.9650"},
        }
        responses = self._drift_responses([JAMF_PRO_XML_CHROME, JAMF_PRO_XML_SCREENCONNECT])
        with patch("handler.urllib.request.urlopen", side_effect=responses):
            with patch("handler.boto3.client", return_value=self.mock_secrets):
                drifted = self.handler._run_jamf_pro_drift_check(te_state)
        self.assertEqual(len(drifted), 1)
        self.assertIn("ScreenConnect Client", drifted[0])
        self.assertIn("26.1.24.9579", drifted[0])
        self.assertIn("26.3.11.9650", drifted[0])

    def test_unknown_name_id_ignored(self):
        te_state = {"googlechrome": {"app": "Google Chrome", "version": "150.0.7871.115"}}
        responses = self._drift_responses([JAMF_PRO_XML_CHROME, JAMF_PRO_XML_SCREENCONNECT])
        with patch("handler.urllib.request.urlopen", side_effect=responses):
            with patch("handler.boto3.client", return_value=self.mock_secrets):
                drifted = self.handler._run_jamf_pro_drift_check(te_state)
        self.assertEqual(drifted, [])

    def test_xml_with_dtd_rejected(self):
        te_state = {"googlechrome": {"app": "Google Chrome", "version": "150.0.7871.115"}}
        evil = '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><patch_software_title/>'
        v2_one_config = [{"id": "201", "displayName": "Google Chrome", "jamfOfficial": False}]
        responses = [
            _make_response({"access_token": "jp-token"}),
            _make_response(v2_one_config),
            _make_response(evil, raw=True),
        ]
        with patch("handler.urllib.request.urlopen", side_effect=responses):
            with patch("handler.boto3.client", return_value=self.mock_secrets):
                with self.assertRaises(ValueError) as ctx:
                    self.handler._run_jamf_pro_drift_check(te_state)
        self.assertIn("DTD", str(ctx.exception))

    def test_oauth_posts_client_credentials_form(self):
        with patch("handler.urllib.request.urlopen", return_value=_make_response({"access_token": "jp-token"})) as urlopen_mock:
            token = self.handler._get_jamf_pro_token("https://jamf.test:8443", "cid", "csec")
        self.assertEqual(token, "jp-token")
        req = urlopen_mock.call_args[0][0]
        body = req.data.decode()
        self.assertIn("grant_type=client_credentials", body)
        self.assertIn("client_id=cid", body)
        self.assertIn("client_secret=csec", body)
        self.assertEqual(req.get_header("Content-type"), "application/x-www-form-urlencoded")

    def test_config_list_timeout_names_call_and_is_not_retried(self):
        te_state = {"googlechrome": {"app": "Google Chrome", "version": "150.0.7871.115"}}
        responses = [
            _make_response({"access_token": "jp-token"}),
            TimeoutError("The read operation timed out"),
        ]
        with patch("handler.urllib.request.urlopen", side_effect=responses) as urlopen_mock:
            with patch("handler.boto3.client", return_value=self.mock_secrets):
                with patch("time.sleep"):
                    with self.assertRaises(RuntimeError) as ctx:
                        self.handler._run_jamf_pro_drift_check(te_state)
        msg = str(ctx.exception)
        self.assertIn("/api/v2/patch-software-title-configurations", msg)
        self.assertIn("The read operation timed out", msg)
        self.assertEqual(urlopen_mock.call_count, 2)

    def test_config_list_gets_45s_timeout(self):
        responses = [_make_response({"access_token": "jp-token"}), _make_response([])]
        with patch("handler.urllib.request.urlopen", side_effect=responses) as urlopen_mock:
            with patch("handler.boto3.client", return_value=self.mock_secrets):
                self.handler._run_jamf_pro_drift_check({})
        self.assertEqual(urlopen_mock.call_args_list[1].kwargs.get("timeout"), 45)

    def test_oauth_token_503_retried_then_succeeds(self):
        import urllib.error
        err = urllib.error.HTTPError(
            "https://jamf.test:8443/api/oauth/token", 503,
            "Service Unavailable", {}, io.BytesIO(b""),
        )
        responses = [err, _make_response({"access_token": "jp-token"}), _make_response([])]
        with patch("handler.urllib.request.urlopen", side_effect=responses) as urlopen_mock:
            with patch("handler.boto3.client", return_value=self.mock_secrets):
                with patch("time.sleep"):
                    drifted = self.handler._run_jamf_pro_drift_check({})
        self.assertEqual(drifted, [])
        self.assertEqual(urlopen_mock.call_count, 3)

    def test_config_xml_timeout_retried_then_succeeds(self):
        te_state = {"googlechrome": {"app": "Google Chrome", "version": "150.0.7871.115"}}
        v2_one_config = [{"id": "201", "displayName": "Google Chrome", "jamfOfficial": False}]
        responses = [
            _make_response({"access_token": "jp-token"}),
            _make_response(v2_one_config),
            TimeoutError("The read operation timed out"),
            _make_response(JAMF_PRO_XML_CHROME, raw=True),
        ]
        with patch("handler.urllib.request.urlopen", side_effect=responses) as urlopen_mock:
            with patch("handler.boto3.client", return_value=self.mock_secrets):
                with patch("time.sleep"):
                    drifted = self.handler._run_jamf_pro_drift_check(te_state)
        self.assertEqual(drifted, [])
        self.assertEqual(urlopen_mock.call_count, 4)

    def test_config_xml_failure_names_title(self):
        te_state = {"googlechrome": {"app": "Google Chrome", "version": "150.0.7871.115"}}
        v2_one_config = [{"id": "201", "displayName": "Google Chrome", "jamfOfficial": False}]
        responses = [
            _make_response({"access_token": "jp-token"}),
            _make_response(v2_one_config),
            TimeoutError("The read operation timed out"),
            TimeoutError("The read operation timed out"),
        ]
        with patch("handler.urllib.request.urlopen", side_effect=responses):
            with patch("handler.boto3.client", return_value=self.mock_secrets):
                with patch("time.sleep"):
                    with self.assertRaises(RuntimeError) as ctx:
                        self.handler._run_jamf_pro_drift_check(te_state)
        msg = str(ctx.exception)
        self.assertIn("Google Chrome", msg)
        self.assertIn("/JSSResource/patchsoftwaretitles/id/201", msg)
        self.assertIn("2 attempts", msg)


class TestLambdaHandlerDriftIntegration(unittest.TestCase):

    def setUp(self):
        self.handler = _reload_handler()
        _disable_download_checks(self)
        self._apps = self.handler.APPS
        self.handler.APPS = [a for a in self.handler.APPS if a["name"] == "Google Chrome"]

        self.mock_ssm = MagicMock()
        self.mock_ssm.get_parameter.side_effect = [
            {"Parameter": {"Value": "testuser"}},
            {"Parameter": {"Value": "testpass"}},
        ]
        self.mock_secrets = MagicMock()
        self.mock_secrets.get_secret_value.return_value = {
            "SecretString": json.dumps({"client_id": "cid", "client_secret": "csec"})
        }
        self.cloudwatch = MagicMock()
        self.mock_sns = MagicMock()

    def tearDown(self):
        self.handler.APPS = self._apps

    def _boto_router(self):
        def route(svc):
            return {
                "ssm": self.mock_ssm,
                "secretsmanager": self.mock_secrets,
                "cloudwatch": self.cloudwatch,
                "sns": self.mock_sns,
            }.get(svc, MagicMock())
        return route

    def _sync_responses(self):
        token_resp = _make_response({"token": "te-token"})
        version_resp = _make_response({"releases": [{"version": "150.0.7871.115", "fraction": 1}]})
        title_resp = _make_response({
            "id": "googlechrome", "currentVersion": "150.0.7871.115",
            "enabled": True, "patches": [],
        })
        return [token_resp, version_resp, title_resp]

    def _lag_metric_values(self):
        return [
            c.kwargs["MetricData"][0]["Value"]
            for c in self.cloudwatch.put_metric_data.call_args_list
            if c.kwargs["MetricData"][0]["MetricName"] == "JamfProDefinitionLag"
        ]

    def test_drift_check_skipped_without_config(self):
        os.environ.pop("JAMF_PRO_URL", None)
        os.environ.pop("JAMF_PRO_SECRET_ID", None)
        with patch("handler.urllib.request.urlopen", side_effect=self._sync_responses()) as urlopen_mock:
            with patch("handler.boto3.client", side_effect=self._boto_router()):
                result = self.handler.lambda_handler({}, None)
        self.assertEqual(result["statusCode"], 200)
        self.assertEqual(urlopen_mock.call_count, 3)
        self.assertEqual(self._lag_metric_values(), [])

    def test_lagging_title_emits_metric_and_result_but_run_succeeds(self):
        os.environ["JAMF_PRO_URL"] = "https://jamf.test:8443"
        os.environ["JAMF_PRO_SECRET_ID"] = "test/jamf-pro-readonly"
        self.addCleanup(os.environ.pop, "JAMF_PRO_URL", None)
        self.addCleanup(os.environ.pop, "JAMF_PRO_SECRET_ID", None)

        stale_chrome_xml = JAMF_PRO_XML_CHROME.replace("150.0.7871.115", "150.0.7871.101")
        v2_one_config = [{"id": "201", "displayName": "Google Chrome", "jamfOfficial": False}]
        responses = self._sync_responses() + [
            _make_response({"access_token": "jp-token"}),
            _make_response(v2_one_config),
            _make_response(stale_chrome_xml, raw=True),
        ]
        with patch("handler.urllib.request.urlopen", side_effect=responses):
            with patch("handler.boto3.client", side_effect=self._boto_router()):
                result = self.handler.lambda_handler({}, None)

        self.assertEqual(result["statusCode"], 200)
        self.assertEqual(self._lag_metric_values(), [1])
        lag_entries = [r for r in result["results"] if r.get("status") == "lagging"]
        self.assertEqual(len(lag_entries), 1)
        self.assertIn("Google Chrome", lag_entries[0]["detail"])
        self.mock_sns.publish.assert_not_called()

    def test_synced_title_emits_zero_metric(self):
        os.environ["JAMF_PRO_URL"] = "https://jamf.test:8443"
        os.environ["JAMF_PRO_SECRET_ID"] = "test/jamf-pro-readonly"
        self.addCleanup(os.environ.pop, "JAMF_PRO_URL", None)
        self.addCleanup(os.environ.pop, "JAMF_PRO_SECRET_ID", None)

        v2_one_config = [{"id": "201", "displayName": "Google Chrome", "jamfOfficial": False}]
        responses = self._sync_responses() + [
            _make_response({"access_token": "jp-token"}),
            _make_response(v2_one_config),
            _make_response(JAMF_PRO_XML_CHROME, raw=True),
        ]
        with patch("handler.urllib.request.urlopen", side_effect=responses):
            with patch("handler.boto3.client", side_effect=self._boto_router()):
                result = self.handler.lambda_handler({}, None)

        self.assertEqual(result["statusCode"], 200)
        self.assertEqual(self._lag_metric_values(), [0])

    def test_drift_check_error_fails_run_with_detail_email(self):
        os.environ["JAMF_PRO_URL"] = "https://jamf.test:8443"
        os.environ["JAMF_PRO_SECRET_ID"] = "test/jamf-pro-readonly"
        os.environ["ALERT_TOPIC_ARN"] = "arn:aws:sns:us-west-2:111122223333:test-alerts"
        self.addCleanup(os.environ.pop, "JAMF_PRO_URL", None)
        self.addCleanup(os.environ.pop, "JAMF_PRO_SECRET_ID", None)
        self.addCleanup(os.environ.pop, "ALERT_TOPIC_ARN", None)

        import urllib.error
        def oauth_503():
            return urllib.error.HTTPError("https://jamf.test:8443/api/oauth/token", 503,
                                          "Service Unavailable", {}, io.BytesIO(b""))
        responses = self._sync_responses() + [oauth_503(), oauth_503()]
        with patch("handler.urllib.request.urlopen", side_effect=responses):
            with patch("handler.boto3.client", side_effect=self._boto_router()):
                with patch("time.sleep"):
                    with self.assertRaises(RuntimeError) as ctx:
                        self.handler.lambda_handler({}, None)

        self.assertIn("Jamf Pro drift check", str(ctx.exception))
        self.mock_sns.publish.assert_called_once()
        message = self.mock_sns.publish.call_args.kwargs["Message"]
        self.assertIn("Jamf Pro drift check", message)
        self.assertIn("503", message)
        self.assertIn("/api/oauth/token", message)


class TestTitleEditorAuth(unittest.TestCase):

    def test_uses_basic_auth_header(self):
        handler = _reload_handler()
        token_resp = _make_response({"token": "test-token"})

        with patch("handler.urllib.request.urlopen", return_value=token_resp) as mock_urlopen:
            token = handler._get_title_editor_token(
                "https://test.appcatalog.jamfcloud.com", "myuser", "mypass"
            )

        self.assertEqual(token, "test-token")
        req = mock_urlopen.call_args[0][0]
        auth_header = req.get_header("Authorization")
        self.assertTrue(auth_header.startswith("Basic "))
        import base64
        decoded = base64.b64decode(auth_header.split(" ")[1]).decode()
        self.assertEqual(decoded, "myuser:mypass")


class TestFetchVersionDrcInsight(unittest.TestCase):

    def _drc_config(self, handler):
        return next(a for a in handler.APPS if a["name"] == "DRC INSIGHT")

    def _lookup_body(self, version="17.0.0", minimum_os="15.7"):
        return (
            '{\n "resultCount":1,\n"results": [\n'
            '{"screenshotUrls":[], "artistViewUrl":"https://apps.apple.com/us/developer/x",'
            '"trackCensoredName":"DRC INSIGHT", "trackViewUrl":"https://apps.apple.com/us/app/x",'
            f'"minimumOsVersion":"{minimum_os}", "bundleId":"com.drc.wbte-ipad.drc",'
            '"currentVersionReleaseDate":"2026-06-25T17:53:38Z",'
            f'"version":"{version}", "sellerName":"Data Recognition Corporation",'
            '"trackName":"DRC INSIGHT", "kind":"software"}]\n}'
        )

    def test_valid_version_from_itunes_lookup(self):
        handler = _reload_handler()
        app_config = self._drc_config(handler)
        mock_opener = MagicMock()
        mock_opener.open.return_value = _make_response(self._lookup_body(), raw=True)
        with patch("handler.urllib.request.build_opener", return_value=mock_opener):
            version = handler._fetch_latest_version(app_config)
            self.assertEqual(version, "17.0.0")

    def test_three_part_minimum_os_version_is_not_matched(self):
        """The leading quote and lowercase v anchor the match to the version
        field; a three-part minimumOsVersion must not be picked up instead."""
        handler = _reload_handler()
        app_config = self._drc_config(handler)
        body = self._lookup_body(version="17.0.0", minimum_os="15.7.1")
        mock_opener = MagicMock()
        mock_opener.open.return_value = _make_response(body, raw=True)
        with patch("handler.urllib.request.build_opener", return_value=mock_opener):
            self.assertEqual(handler._fetch_latest_version(app_config), "17.0.0")

    def test_lookup_without_version_raises(self):
        handler = _reload_handler()
        app_config = self._drc_config(handler)
        body = '{"resultCount":0, "results": []}'
        mock_opener = MagicMock()
        mock_opener.open.return_value = _make_response(body, raw=True)
        with patch("handler.urllib.request.build_opener", return_value=mock_opener):
            with self.assertRaises(ValueError) as ctx:
                handler._fetch_latest_version(app_config)
            self.assertIn("No version found", str(ctx.exception))

    def test_four_part_version_refused_not_truncated(self):
        """DRC ships major.minor.patch. If they ever go four-part, the trailing
        quote in the regex means no match at all, so the run fails loudly
        instead of silently truncating 17.0.0.1 to 17.0.0 and publishing a
        patch for a version that was never released."""
        handler = _reload_handler()
        app_config = self._drc_config(handler)
        mock_opener = MagicMock()
        mock_opener.open.return_value = _make_response(
            self._lookup_body(version="17.0.0.1"), raw=True
        )
        with patch("handler.urllib.request.build_opener", return_value=mock_opener):
            with self.assertRaises(ValueError) as ctx:
                handler._fetch_latest_version(app_config)
            self.assertIn("No version found", str(ctx.exception))


class TestDrcInsightPatchBody(unittest.TestCase):

    def _drc_config(self, handler):
        return next(a for a in handler.APPS if a["name"] == "DRC INSIGHT")

    def test_version_substitution_uses_recon_criteria(self):
        handler = _reload_handler()
        app_config = self._drc_config(handler)
        body = handler._build_patch_body("17.0.0", app_config)
        self.assertEqual(body["version"], "17.0.0")
        self.assertEqual(body["components"][0]["version"], "17.0.0")
        criteria = body["components"][0]["criteria"]
        criteria_values = [c["value"] for c in criteria]
        self.assertIn("17.0.0", criteria_values)
        self.assertIn("com.datarecognitioncorp.drcinsight.mac.sqa", criteria_values)
        self.assertEqual({c["type"] for c in criteria}, {"recon"})
        self.assertIn("releaseDate", body)

    def test_kill_apps_stays_empty(self):
        """DRC INSIGHT is a locked-down exam browser; a patch policy that quits
        it could end a student's ACCESS test."""
        handler = _reload_handler()
        app_config = self._drc_config(handler)
        body = handler._build_patch_body("17.0.0", app_config)
        self.assertEqual(body["killApps"], [])

    def test_template_not_mutated(self):
        handler = _reload_handler()
        app_config = self._drc_config(handler)
        original = json.dumps(app_config["patch_template"])
        handler._build_patch_body("17.0.0", app_config)
        self.assertEqual(json.dumps(app_config["patch_template"]), original)


class TestFetchMinimumAcceptedVersion(unittest.TestCase):

    SB_VERSIONS = {
        "SecureClientVersion.Windows64": {"minimumVersion": "16.0.0", "updateFile": ""},
        "SecureClientVersion.Mac": {"minimumVersion": "16.0.0", "updateFile": ""},
        "SecureClientVersion.ChromeOS": {"minimumVersion": "16.0.0", "updateFile": ""},
    }

    def test_walks_json_path_to_the_platform_floor(self):
        handler = _reload_handler()
        config = {
            "url": "https://example.test/sb-versions",
            "json_path": ["SecureClientVersion.Mac", "minimumVersion"],
        }
        with patch("handler.urllib.request.urlopen",
                   return_value=_make_response(self.SB_VERSIONS)):
            self.assertEqual(
                handler._fetch_minimum_accepted_version(config, "DRC INSIGHT"), "16.0.0"
            )

    def test_unparseable_floor_names_the_app_and_the_value(self):
        """A vendor feed can hand back an empty or non-numeric version (this
        endpoint already returns "" for its sibling updateFile field). The
        failure has to name what broke, not surface a bare int() error."""
        handler = _reload_handler()
        config = {
            "url": "https://example.test/sb-versions",
            "json_path": ["SecureClientVersion.Mac", "minimumVersion"],
        }
        for bad in ("16.0.0-beta", "sixteen", ""):
            payload = {"SecureClientVersion.Mac": {"minimumVersion": bad}}
            with patch("handler.urllib.request.urlopen",
                       return_value=_make_response(payload)):
                with self.assertRaises(ValueError) as ctx:
                    handler._fetch_minimum_accepted_version(config, "DRC INSIGHT")
            message = str(ctx.exception)
            self.assertIn("DRC INSIGHT", message)
            self.assertIn(repr(bad), message)
            self.assertIn("sb-versions", message)

    def test_missing_platform_key_raises(self):
        handler = _reload_handler()
        config = {
            "url": "https://example.test/sb-versions",
            "json_path": ["SecureClientVersion.Solaris", "minimumVersion"],
        }
        with patch("handler.urllib.request.urlopen",
                   return_value=_make_response(self.SB_VERSIONS)):
            with self.assertRaises(ValueError) as ctx:
                handler._fetch_minimum_accepted_version(config, "DRC INSIGHT")
            self.assertIn("SecureClientVersion.Solaris", str(ctx.exception))


class TestRunMinimumVersionChecks(unittest.TestCase):

    def _sb_versions(self, mac_floor):
        return {"SecureClientVersion.Mac": {"minimumVersion": mac_floor, "updateFile": ""}}

    KNOWN = "16.0.0"

    def test_unchanged_floor_reports_nothing(self):
        handler = _reload_handler()
        with patch("handler.urllib.request.urlopen",
                   return_value=_make_response(self._sb_versions(self.KNOWN))):
            self.assertEqual(handler._run_minimum_version_checks(), ([], []))

    def test_changed_floor_names_both_values(self):
        """The whole point of the check: WIDA moving its number is the moment
        machines that were merely behind become refused."""
        handler = _reload_handler()
        with patch("handler.urllib.request.urlopen",
                   return_value=_make_response(self._sb_versions("17.0.0"))):
            changes, errors = handler._run_minimum_version_checks()
        self.assertEqual(errors, [])
        # Pinned whole: asserting both numbers appear separately let a message
        # that stated them backwards pass, which would send you to update
        # apps.json to the vendor's old floor.
        self.assertEqual(changes, [
            "DRC INSIGHT: vendor minimum accepted version is now 17.0.0 "
            "(apps.json records 16.0.0)"
        ])

    def test_a_lower_floor_is_still_a_change(self):
        """A vendor relaxing its floor matters too, and a comparison-based
        check would have missed it entirely."""
        handler = _reload_handler()
        with patch("handler.urllib.request.urlopen",
                   return_value=_make_response(self._sb_versions("15.0.0"))):
            changes, errors = handler._run_minimum_version_checks()
        self.assertEqual(len(changes), 1)
        self.assertIn("15.0.0", changes[0])

    def test_apps_without_a_declared_floor_are_skipped(self):
        handler = _reload_handler()
        handler.APPS = [a for a in handler.APPS if a["name"] == "Google Chrome"]
        with patch("handler.urllib.request.urlopen") as mock_urlopen:
            self.assertEqual(handler._run_minimum_version_checks(), ([], []))
        mock_urlopen.assert_not_called()

    def test_one_unreachable_feed_does_not_hide_another_app_change(self):
        """A dead feed for one app must not discard a real change already
        found for another."""
        handler = _reload_handler()
        drc = next(a for a in handler.APPS if a["name"] == "DRC INSIGHT")
        broken = json.loads(json.dumps(drc))
        broken["name"] = "Broken Vendor"
        broken["minimum_accepted_version"]["url"] = "https://broken.test/feed"
        handler.APPS = [broken, drc]

        def route(req, *a, **kw):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            if "broken.test" in url:
                raise urllib.error.URLError("connection refused")
            return _make_response(self._sb_versions("17.0.0"))

        with patch("time.sleep"):
            with patch("handler.urllib.request.urlopen", side_effect=route):
                changes, errors = handler._run_minimum_version_checks()
        self.assertEqual(len(errors), 1)
        self.assertIn("broken.test", errors[0])
        self.assertEqual(len(changes), 1)
        self.assertIn("DRC INSIGHT", changes[0])

    def test_missing_known_value_is_reported_as_an_error(self):
        handler = _reload_handler()
        drc = next(a for a in handler.APPS if a["name"] == "DRC INSIGHT")
        del drc["minimum_accepted_version"]["known"]
        handler.APPS = [drc]
        with patch("handler.urllib.request.urlopen") as mock_urlopen:
            changes, errors = handler._run_minimum_version_checks()
        self.assertEqual(changes, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("DRC INSIGHT", errors[0])
        mock_urlopen.assert_not_called()


class TestFailureAlertMinimumVersion(unittest.TestCase):

    def _results_with_change(self):
        return [
            {"app": "Google Chrome", "status": "failed", "error": "boom"},
            {"app": "DRC INSIGHT", "status": "current", "version": "17.0.0"},
            {"check": "minimum_version", "status": "changed",
             "detail": "DRC INSIGHT: vendor minimum accepted version is now 17.0.0 "
                       "(apps.json records 16.0.0)"},
        ]

    def test_change_appears_even_when_another_item_failed(self):
        """Without this the email lists DRC INSIGHT as current and says nothing
        else, hiding that the vendor just moved the bar."""
        handler = _reload_handler()
        _, body = handler._build_failure_alert(
            self._results_with_change(), ["Google Chrome"], [], "req-1"
        )
        self.assertIn("minimum accepted version is now 17.0.0", body)
        self.assertIn("apps.json records 16.0.0", body)

    def test_intro_does_not_claim_only_failures_matter(self):
        handler = _reload_handler()
        _, body = handler._build_failure_alert(
            self._results_with_change(), ["Google Chrome"], [], "req-1"
        )
        self.assertNotIn("Only the items under 'Failed' need attention", body)

    def test_intro_unchanged_when_nothing_changed(self):
        handler = _reload_handler()
        results = [{"app": "Google Chrome", "status": "failed", "error": "boom"}]
        _, body = handler._build_failure_alert(results, ["Google Chrome"], [], "req-1")
        self.assertIn("Only the items under 'Failed' need attention", body)


class TestLambdaHandlerMinimumVersionIntegration(unittest.TestCase):

    def setUp(self):
        self.handler = _reload_handler()
        _disable_download_checks(self)
        self._apps = self.handler.APPS
        self.handler.APPS = [a for a in self.handler.APPS if a["name"] == "DRC INSIGHT"]

        os.environ["TITLE_EDITOR_DRC_INSIGHT_TITLE_ID"] = "11"
        self.addCleanup(os.environ.pop, "TITLE_EDITOR_DRC_INSIGHT_TITLE_ID", None)
        os.environ.pop("JAMF_PRO_URL", None)
        os.environ.pop("JAMF_PRO_SECRET_ID", None)

        self.mock_ssm = MagicMock()
        self.mock_ssm.get_parameter.side_effect = [
            {"Parameter": {"Value": "testuser"}},
            {"Parameter": {"Value": "testpass"}},
        ]
        self.cloudwatch = MagicMock()
        self.mock_sns = MagicMock()

    def tearDown(self):
        self.handler.APPS = self._apps

    def _boto_router(self):
        def route(svc):
            return {
                "ssm": self.mock_ssm,
                "cloudwatch": self.cloudwatch,
                "sns": self.mock_sns,
            }.get(svc, MagicMock())
        return route

    def _itunes_opener(self):
        opener = MagicMock()
        opener.open.return_value = _make_response(
            '{"resultCount":1,"results":[{"version":"17.0.0",'
            '"bundleId":"com.drc.wbte-ipad.drc"}]}', raw=True
        )
        return opener

    def _changed_metric_values(self):
        return [
            c.kwargs["MetricData"][0]["Value"]
            for c in self.cloudwatch.put_metric_data.call_args_list
            if c.kwargs["MetricData"][0]["MetricName"] == "MinimumVersionChanged"
        ]

    def _run(self, sb_versions_resp, te_current="17.0.0", update_calls=0):
        title_resp = _make_response({
            "id": "drcinsight", "currentVersion": te_current,
            "enabled": True, "patches": [],
        })
        responses = [_make_response({"token": "te-token"}), title_resp]
        # Publishing a new version costs a patch POST and a title PUT.
        responses += [_make_response({}) for _ in range(update_calls)]
        if isinstance(sb_versions_resp, list):
            responses += sb_versions_resp
        else:
            responses.append(sb_versions_resp)
        with patch("handler.urllib.request.build_opener", return_value=self._itunes_opener()):
            with patch("handler.urllib.request.urlopen", side_effect=responses):
                with patch("handler.boto3.client", side_effect=self._boto_router()):
                    return self.handler.lambda_handler({}, None)

    KNOWN = "16.0.0"

    def test_unchanged_floor_emits_zero(self):
        result = self._run(_make_response(
            {"SecureClientVersion.Mac": {"minimumVersion": self.KNOWN, "updateFile": ""}}
        ))
        self.assertEqual(result["statusCode"], 200)
        self.assertEqual(self._changed_metric_values(), [0])
        self.assertEqual(
            [r for r in result["results"] if r.get("check") == "minimum_version"], []
        )

    def test_changed_floor_reported_without_failing_the_run(self):
        """A moved floor is news, not a pipeline fault, so the sync still
        reports success and the alarm carries the signal."""
        result = self._run(_make_response(
            {"SecureClientVersion.Mac": {"minimumVersion": "17.0.0", "updateFile": ""}}
        ))
        self.assertEqual(result["statusCode"], 200)
        self.assertEqual(self._changed_metric_values(), [1])
        entries = [r for r in result["results"] if r.get("check") == "minimum_version"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["status"], "changed")
        self.assertIn("17.0.0", entries[0]["detail"])

    def test_check_runs_even_when_the_app_sync_is_irrelevant_to_it(self):
        """The floor check reads only vendor config, so an app updating (rather
        than being current) must not change whether it runs."""
        result = self._run(
            _make_response(
                {"SecureClientVersion.Mac": {"minimumVersion": self.KNOWN, "updateFile": ""}}
            ),
            te_current="16.0.0",
            update_calls=2,
        )
        self.assertEqual(result["statusCode"], 200)
        self.assertEqual(self._changed_metric_values(), [0])
        self.assertEqual(
            [r for r in result["results"] if r.get("app") == "DRC INSIGHT"][0]["status"],
            "updated",
        )

    def test_unreachable_feed_fails_the_run_and_publishes_no_metric(self):
        """A check that could not answer must never be recorded as a clean 0,
        which would clear a standing alarm."""
        os.environ["ALERT_TOPIC_ARN"] = "arn:aws:sns:us-west-2:111122223333:test-alerts"
        self.addCleanup(os.environ.pop, "ALERT_TOPIC_ARN", None)
        err = urllib.error.URLError("connection refused")
        with patch("time.sleep"):
            with self.assertRaises(RuntimeError) as ctx:
                self._run([err, err])
        self.assertIn("Minimum version check", str(ctx.exception))
        self.mock_sns.publish.assert_called_once()
        body = self.mock_sns.publish.call_args.kwargs["Message"]
        self.assertIn("sb-versions", body)
        self.assertIn("connection refused", body)
        self.assertEqual(self._changed_metric_values(), [])

    def test_shortfall_only_run_sends_no_email(self):
        os.environ["ALERT_TOPIC_ARN"] = "arn:aws:sns:us-west-2:111122223333:test-alerts"
        self.addCleanup(os.environ.pop, "ALERT_TOPIC_ARN", None)
        self._run(_make_response(
            {"SecureClientVersion.Mac": {"minimumVersion": "17.0.0", "updateFile": ""}}
        ))
        self.mock_sns.publish.assert_not_called()


class TestTransientClassification(unittest.TestCase):
    """_with_retries documents that it retries connection errors. urllib only
    wraps failures from the connect phase in URLError; anything raised while
    reading the response arrives raw, so these have to be classified explicitly
    or a mid-response reset fails an app for the whole 12-hour cycle."""

    def _connection_level_errors(self):
        import http.client
        return [
            ConnectionResetError(54, "Connection reset by peer"),
            http.client.RemoteDisconnected("Remote end closed connection"),
            http.client.IncompleteRead(b"12345", 95),
            http.client.BadStatusLine("\r\n"),
        ]

    def test_connection_level_failures_are_transient(self):
        handler = _reload_handler()
        for exc in self._connection_level_errors():
            self.assertTrue(
                handler._is_transient(exc),
                f"{type(exc).__name__} should be retryable",
            )

    def test_connection_level_failures_are_actually_retried(self):
        handler = _reload_handler()
        for exc in self._connection_level_errors():
            calls = []

            def fn():
                calls.append(1)
                raise exc

            with patch("time.sleep"):
                with self.assertRaises(RuntimeError):
                    handler._with_retries("GET https://vendor.test/feed", fn)
            self.assertEqual(len(calls), 2, f"{type(exc).__name__} was not retried")

    def test_a_404_is_still_not_retried(self):
        handler = _reload_handler()
        calls = []

        def fn():
            calls.append(1)
            raise urllib.error.HTTPError("https://x", 404, "Not Found", {}, None)

        with self.assertRaises(RuntimeError):
            handler._with_retries("GET https://x", fn)
        self.assertEqual(len(calls), 1)


class TestRetryErrorMessage(unittest.TestCase):

    def test_blank_exception_text_still_names_the_failure(self):
        """A malformed status line stringifies to bare CRLF, which produced
        alert lines ending in 'GET <url>: ' with nothing after the colon."""
        import http.client
        handler = _reload_handler()

        def fn():
            raise http.client.BadStatusLine("\r\n")

        with patch("time.sleep"):
            with self.assertRaises(RuntimeError) as ctx:
                handler._with_retries("version source GET https://vendor.test/x", fn)
        message = str(ctx.exception)
        self.assertIn("version source GET https://vendor.test/x", message)
        self.assertIn("BadStatusLine", message)
        self.assertNotIn("\r", message)
        self.assertNotIn("\n", message)


class TestMetricFailuresDoNotMaskResults(unittest.TestCase):
    """The metric puts sit between the collected failures and the detail email.
    Unguarded, a CloudWatch throttle throws away every failure the run found."""

    def setUp(self):
        self.handler = _reload_handler()
        self._apps = self.handler.APPS
        self.handler.APPS = [a for a in self.handler.APPS if a["name"] == "Google Chrome"]
        self.addCleanup(setattr, self.handler, "APPS", self._apps)
        os.environ["ALERT_TOPIC_ARN"] = "arn:aws:sns:us-west-2:111122223333:test-alerts"
        self.addCleanup(os.environ.pop, "ALERT_TOPIC_ARN", None)
        os.environ.pop("JAMF_PRO_URL", None)
        os.environ.pop("JAMF_PRO_SECRET_ID", None)
        self.mock_sns = MagicMock()
        self.cloudwatch = MagicMock()
        self.cloudwatch.put_metric_data.side_effect = RuntimeError("ThrottlingException")
        self.mock_ssm = MagicMock()
        self.mock_ssm.get_parameter.side_effect = [
            {"Parameter": {"Value": "u"}}, {"Parameter": {"Value": "p"}},
        ]

    def _router(self):
        def route(svc):
            return {"ssm": self.mock_ssm, "cloudwatch": self.cloudwatch,
                    "sns": self.mock_sns}.get(svc, MagicMock())
        return route

    def test_throttled_metric_still_lets_the_failure_email_out(self):
        _disable_download_checks(self)
        responses = [
            _make_response({"token": "t"}),
            urllib.error.HTTPError("https://x", 500, "boom", {}, None),
        ]
        with patch("time.sleep"):
            with patch("handler.urllib.request.urlopen", side_effect=responses * 3):
                with patch("handler.boto3.client", side_effect=self._router()):
                    with self.assertRaises(RuntimeError):
                        self.handler.lambda_handler({}, None)
        self.mock_sns.publish.assert_called_once()
        self.assertIn("Google Chrome", self.mock_sns.publish.call_args.kwargs["Message"])


class TestUpdateMetricFailureDoesNotFailThePublish(unittest.TestCase):

    def test_publish_succeeds_even_if_the_metric_put_throws(self):
        """Both Title Editor writes have already landed by then; reporting the
        app as failed would raise a false alarm for a patch that published."""
        handler = _reload_handler()
        app_config = next(a for a in handler.APPS if a["name"] == "Google Chrome")
        cloudwatch = MagicMock()
        cloudwatch.put_metric_data.side_effect = RuntimeError("AccessDenied")
        with patch("handler._title_editor_request") as te:
            with patch("handler.boto3.client", return_value=cloudwatch):
                handler._update_title(
                    "https://te.test", "tok", "1", "150.0.0.1", app_config, False
                )
        methods = [c.args[2] for c in te.call_args_list]
        self.assertIn("POST", methods)
        self.assertIn("PUT", methods)


class TestTitleEditorWireFormat(unittest.TestCase):
    """Publishing a patch is the product. Nothing else asserted the method,
    path, headers or body that actually reach Title Editor, so a wrong path or
    a dropped field would have shipped green."""

    def _capture(self, fn):
        sent = []

        def urlopen(req, timeout=None):
            sent.append({
                "method": req.get_method(),
                "url": req.full_url,
                "auth": req.get_header("Authorization"),
                "content_type": req.get_header("Content-type"),
                "body": json.loads(req.data.decode()) if req.data else None,
            })
            return _make_response({})

        with patch("handler.urllib.request.urlopen", side_effect=urlopen):
            fn()
        return sent

    def test_update_posts_the_patch_then_sets_current_version(self):
        handler = _reload_handler()
        app_config = next(a for a in handler.APPS if a["name"] == "DRC INSIGHT")
        sent = self._capture(lambda: handler._update_title(
            "https://te.test", "tok-123", "11", "17.1.0", app_config, False
        ))
        self.assertEqual(len(sent), 2)

        post, put = sent
        self.assertEqual(post["method"], "POST")
        self.assertEqual(post["url"], "https://te.test/v2/softwaretitles/11/patches")
        self.assertEqual(post["body"]["version"], "17.1.0")
        self.assertEqual(post["body"]["components"][0]["version"], "17.1.0")

        self.assertEqual(put["method"], "PUT")
        self.assertEqual(put["url"], "https://te.test/v2/softwaretitles/11")
        self.assertEqual(put["body"], {"currentVersion": "17.1.0", "enabled": True})

    def test_existing_patch_version_is_not_posted_again(self):
        handler = _reload_handler()
        app_config = next(a for a in handler.APPS if a["name"] == "DRC INSIGHT")
        sent = self._capture(lambda: handler._update_title(
            "https://te.test", "tok-123", "11", "17.1.0", app_config, True
        ))
        self.assertEqual([s["method"] for s in sent], ["PUT"])
        self.assertEqual(sent[0]["body"], {"currentVersion": "17.1.0", "enabled": True})

    def test_requests_carry_the_bearer_token_and_json_content_type(self):
        handler = _reload_handler()
        sent = self._capture(lambda: handler._title_editor_request(
            "https://te.test", "tok-123", "POST", "/v2/softwaretitles/11/patches",
            {"version": "17.1.0"},
        ))
        self.assertEqual(sent[0]["auth"], "Bearer tok-123")
        self.assertEqual(sent[0]["content_type"], "application/json")
        self.assertEqual(sent[0]["body"], {"version": "17.1.0"})

    def test_title_info_reads_the_requested_title(self):
        handler = _reload_handler()
        sent = self._capture(
            lambda: handler._get_title_info("https://te.test", "tok-123", "11")
        )
        self.assertEqual(sent[0]["method"], "GET")
        self.assertEqual(sent[0]["url"], "https://te.test/v2/softwaretitles/11")


class TestVersionFeedOrdering(unittest.TestCase):

    def test_chrome_feed_takes_the_newest_release_not_the_oldest(self):
        """Google's feed is newest-first and carries the whole history. Reading
        the wrong end still yields a valid four-part string, so version_pattern
        would not catch it and a stale Chrome would publish silently."""
        handler = _reload_handler()
        app_config = next(a for a in handler.APPS if a["name"] == "Google Chrome")
        feed = {"releases": [
            {"version": "150.0.7871.190"},
            {"version": "150.0.7871.182"},
            {"version": "149.0.7827.54"},
        ]}
        with patch("handler.urllib.request.urlopen", return_value=_make_response(feed)):
            self.assertEqual(handler._fetch_latest_version(app_config), "150.0.7871.190")


class TestAppsJsonIntegrity(unittest.TestCase):
    """The shipped config is what reaches production. Several entries were only
    ever exercised through hand-written fixtures, one of which had already
    drifted from the real file."""

    EXPECTED_SOURCE_TYPES = {
        "Google Chrome": "google_versionhistory",
        "GMetrix SMSe": "electron_updater_feed",
        "MacAdmins Python": "github_releases",
        "ScreenConnect Client": "html_scrape",
        "Promethean Screen Share": "html_scrape",
        "Washington Secure Browser": "redirect_filename",
        "Outset": "github_releases",
        "utiluti": "github_releases",
        "DYMO Connect": "html_scrape",
        "DRC INSIGHT": "html_scrape",
    }

    def test_every_app_uses_the_expected_source_type(self):
        handler = _reload_handler()
        actual = {a["name"]: a["version_source"]["type"] for a in handler.APPS}
        self.assertEqual(actual, self.EXPECTED_SOURCE_TYPES)

    def test_every_source_type_is_one_the_handler_dispatches(self):
        handler = _reload_handler()
        known = {"google_versionhistory", "html_scrape", "github_releases",
                 "redirect_filename", "electron_updater_feed"}
        for app in handler.APPS:
            self.assertIn(app["version_source"]["type"], known, app["name"])

    def test_every_app_declares_enabled_and_a_title_id_env_var(self):
        """Both loops fall back to enabled=True when the key is absent, so an
        entry added without it would be silently included or skipped."""
        handler = _reload_handler()
        for app in handler.APPS:
            self.assertIn("enabled", app, app["name"])
            self.assertIn("title_id_env_var", app, app["name"])

    def test_every_version_pattern_is_anchored(self):
        handler = _reload_handler()
        for app in handler.APPS:
            pattern = app["version_pattern"]
            self.assertTrue(pattern.startswith("^"), app["name"])
            self.assertTrue(pattern.endswith("$"), app["name"])

    def test_every_criteria_operator_is_an_equality_match(self):
        """An operator flipped to 'is not' inverts what the patch matches."""
        handler = _reload_handler()
        for app in handler.APPS:
            for component in app["patch_template"]["components"]:
                for criterion in component["criteria"]:
                    self.assertEqual(criterion["operator"], "is", app["name"])

    def test_drc_insight_pins_its_operating_system_floor(self):
        handler = _reload_handler()
        drc = next(a for a in handler.APPS if a["name"] == "DRC INSIGHT")
        self.assertEqual(drc["patch_template"]["minimumOperatingSystem"], "14.6")
        self.assertEqual(drc["patch_template"]["capabilities"][0]["value"], "14.6")
        self.assertEqual(drc["version_pattern"], r"^\d+\.\d+\.\d+$")

    def test_drc_insight_records_the_wida_floor_we_have_acknowledged(self):
        """Pinned as a literal so the floor-change tests cannot pass by reading
        this value back out of the file they are meant to be checking."""
        handler = _reload_handler()
        drc = next(a for a in handler.APPS if a["name"] == "DRC INSIGHT")
        self.assertEqual(drc["minimum_accepted_version"]["known"], "16.0.0")


class TestTitleEditorRejectionReason(unittest.TestCase):
    """A 400 from the patch POST carried the real reason in its body, which was
    read for the duplicate check and then thrown away, so the alert only said
    'Bad Request'."""

    def _http_error(self, code, body):
        return urllib.error.HTTPError(
            "https://te/x", code, "msg", {}, io.BytesIO(body.encode())
        )

    def _drc(self, handler):
        return next(a for a in handler.APPS if a["name"] == "DRC INSIGHT")

    def test_duplicate_record_400_still_skips_to_the_put(self):
        handler = _reload_handler()
        methods = []

        def fake(base, tok, method, path, body=None):
            methods.append(method)
            if method == "POST":
                raise self._http_error(400, '{"errors":[{"code":"DUPLICATE_RECORD"}]}')
            return {}

        with patch("handler._title_editor_request", side_effect=fake):
            handler._update_title("https://te", "t", "11", "17.0.0", self._drc(handler), False)
        self.assertEqual(methods, ["POST", "PUT"])

    def test_non_duplicate_400_surfaces_the_server_reason(self):
        handler = _reload_handler()

        def fake(base, tok, method, path, body=None):
            if method == "POST":
                raise self._http_error(400, '{"errors":[{"code":"INVALID_CRITERIA",'
                                             '"description":"criteria are invalid"}]}')
            return {}

        with patch("handler._title_editor_request", side_effect=fake):
            with self.assertRaises(Exception) as ctx:
                handler._update_title("https://te", "t", "11", "17.0.0", self._drc(handler), False)
        message = str(ctx.exception)
        self.assertIn("DRC INSIGHT", message)
        self.assertIn("INVALID_CRITERIA", message)

    def test_an_oversized_400_body_is_bounded_before_it_reaches_the_alert(self):
        handler = _reload_handler()

        def fake(base, tok, method, path, body=None):
            if method == "POST":
                raise self._http_error(400, "x" * 5000)
            return {}

        with patch("handler._title_editor_request", side_effect=fake):
            with self.assertRaises(Exception) as ctx:
                handler._update_title("https://te", "t", "11", "17.0.0", self._drc(handler), False)
        self.assertLessEqual(len(str(ctx.exception)), 600)


class TestDownloadCanaryRetries(unittest.TestCase):

    def _resp(self, status):
        resp = MagicMock()
        resp.status = status
        resp.__enter__ = MagicMock(return_value=resp)
        resp.__exit__ = MagicMock(return_value=False)
        return resp

    def test_transient_blip_is_retried_not_reported_as_dead(self):
        handler = _reload_handler()
        calls = []

        def urlopen(req, timeout=None):
            calls.append(1)
            if len(calls) == 1:
                raise urllib.error.URLError(TimeoutError("timed out"))
            return self._resp(206)

        with patch("time.sleep"):
            with patch("handler.urllib.request.urlopen", side_effect=urlopen):
                self.assertTrue(handler._url_is_live("https://ccmdls.adobe.test/x.dmg"))
        self.assertEqual(len(calls), 2)

    def test_a_real_404_is_dead_without_retrying(self):
        handler = _reload_handler()
        calls = []

        def urlopen(req, timeout=None):
            calls.append(1)
            raise urllib.error.HTTPError("https://x", 404, "Not Found", {}, None)

        with patch("handler.urllib.request.urlopen", side_effect=urlopen):
            self.assertFalse(handler._url_is_live("https://x"))
        self.assertEqual(len(calls), 1)

    def test_config_fetch_retries_a_transient_blip(self):
        handler = _reload_handler()
        calls = []

        def urlopen(req, timeout=None):
            calls.append(1)
            if len(calls) == 1:
                raise urllib.error.HTTPError("https://x", 503, "busy", {}, io.BytesIO(b""))
            return _make_response("<config/>", raw=True)

        with patch("time.sleep"):
            with patch("handler.urllib.request.urlopen", side_effect=urlopen):
                self.assertEqual(handler._http_get_text("https://x"), "<config/>")
        self.assertEqual(len(calls), 2)


class TestBlockedDowngradeInFailureEmail(unittest.TestCase):

    def _results(self):
        return [
            {"app": "GMetrix SMSe", "status": "failed", "error": "boom"},
            {"app": "Google Chrome", "status": "regression_blocked",
             "from": "150.0.7871.190", "blocked": "149.0.7827.54"},
        ]

    def test_blocked_downgrade_is_named_when_another_item_failed(self):
        handler = _reload_handler()
        _, body = handler._build_failure_alert(self._results(), ["GMetrix SMSe"], [], "r1")
        self.assertIn("Version downgrade refused", body)
        self.assertIn("kept 150.0.7871.190, refused 149.0.7827.54", body)
        self.assertNotIn("Only the items under 'Failed'", body)


class TestVersionDowngradeDetection(unittest.TestCase):

    def test_lower_candidate_is_a_downgrade(self):
        handler = _reload_handler()
        self.assertTrue(handler._is_version_downgrade("16.0.0", "17.0.0"))
        self.assertTrue(handler._is_version_downgrade("17.0.0", "17.0.1"))

    def test_higher_or_equal_candidate_is_not_a_downgrade(self):
        handler = _reload_handler()
        self.assertFalse(handler._is_version_downgrade("17.0.0", "16.0.0"))
        self.assertFalse(handler._is_version_downgrade("17.0.1", "17.0.0"))
        self.assertFalse(handler._is_version_downgrade("17.0.0", "17.0.0"))

    def test_comparison_is_numeric_not_lexical(self):
        handler = _reload_handler()
        self.assertTrue(handler._is_version_downgrade("9.0.0", "10.0.0"))
        self.assertFalse(handler._is_version_downgrade("10.0.0", "9.0.0"))

    def test_differing_part_counts_pad_with_zero(self):
        handler = _reload_handler()
        self.assertFalse(handler._is_version_downgrade("18.0", "18.0.0"))
        self.assertTrue(handler._is_version_downgrade("18.0", "18.0.1"))

    def test_unparseable_versions_are_not_treated_as_a_downgrade(self):
        """Fail open: an unexpected shape must not block a legitimate update.
        version_pattern already gates the candidate, so this only guards the
        genuinely unexpected."""
        handler = _reload_handler()
        self.assertFalse(handler._is_version_downgrade("2024-01", "2024-02"))
        self.assertFalse(handler._is_version_downgrade("17.0.0", ""))


class TestVersionDowngradeGuard(unittest.TestCase):
    """A vendor serving an older version than Title Editor already holds would,
    unguarded, be written straight through as the new current version and then
    ingested by Jamf Pro, pointing patch reporting backwards. The guard refuses
    the write, alarms, and leaves the definition where it is."""

    def setUp(self):
        self.handler = _reload_handler()
        self._apps = self.handler.APPS
        self.handler.APPS = [a for a in self.handler.APPS if a["name"] == "Google Chrome"]
        self.addCleanup(setattr, self.handler, "APPS", self._apps)
        _disable_download_checks(self)
        os.environ.pop("JAMF_PRO_URL", None)
        os.environ.pop("JAMF_PRO_SECRET_ID", None)
        os.environ["ALERT_TOPIC_ARN"] = "arn:aws:sns:us-west-2:111122223333:test-alerts"
        self.addCleanup(os.environ.pop, "ALERT_TOPIC_ARN", None)
        self.mock_ssm = MagicMock()
        self.mock_ssm.get_parameter.side_effect = [
            {"Parameter": {"Value": "u"}}, {"Parameter": {"Value": "p"}},
        ]
        self.cloudwatch = MagicMock()
        self.mock_sns = MagicMock()

    def _router(self):
        def route(svc):
            return {"ssm": self.mock_ssm, "cloudwatch": self.cloudwatch,
                    "sns": self.mock_sns}.get(svc, MagicMock())
        return route

    def _metric(self, name):
        return [
            c.kwargs["MetricData"][0]["Value"]
            for c in self.cloudwatch.put_metric_data.call_args_list
            if c.kwargs["MetricData"][0]["MetricName"] == name
        ]

    def _run(self, vendor_version, te_current):
        responses = [
            _make_response({"token": "t"}),
            _make_response({"releases": [{"version": vendor_version, "fraction": 1}]}),
            _make_response({"currentVersion": te_current, "enabled": True,
                            "id": "googlechrome", "patches": [{"version": te_current}]}),
            # a POST and PUT are queued but must go unused when the guard fires
            _make_response({"patchId": 1}),
            _make_response({"currentVersion": vendor_version}),
        ]
        with patch("handler.urllib.request.urlopen", side_effect=responses) as urlopen:
            with patch("handler.boto3.client", side_effect=self._router()):
                result = self.handler.lambda_handler({}, None)
        return result, urlopen

    def test_downgrade_is_not_written_and_run_still_succeeds(self):
        result, urlopen = self._run("149.0.7827.54", "150.0.7871.190")
        self.assertEqual(result["statusCode"], 200)
        # GET token, GET version, GET title only. No POST, no PUT.
        self.assertEqual(urlopen.call_count, 3)
        entry = result["results"][0]
        self.assertEqual(entry["status"], "regression_blocked")
        self.assertEqual(entry["from"], "150.0.7871.190")
        self.assertEqual(entry["blocked"], "149.0.7827.54")

    def test_downgrade_emits_the_metric_but_no_email_on_its_own(self):
        self._run("149.0.7827.54", "150.0.7871.190")
        self.assertEqual(self._metric("VersionRegressionBlocked"), [1])
        self.mock_sns.publish.assert_not_called()

    def test_a_real_upgrade_is_still_written_normally(self):
        result, urlopen = self._run("150.0.7871.190", "149.0.7827.54")
        self.assertEqual(result["results"][0]["status"], "updated")
        self.assertEqual(urlopen.call_count, 5)
        self.assertEqual(self._metric("VersionRegressionBlocked"), [0])

    def test_blocked_app_reports_the_held_version_to_the_drift_check(self):
        """te_state must carry what Title Editor still holds (the higher
        version), not the rejected one, or the drift check would chase a value
        that was never written."""
        captured = {}
        orig = self.handler._run_jamf_pro_drift_check

        def spy(te_state):
            captured.update(te_state)
            return []

        os.environ["JAMF_PRO_URL"] = "https://jamf.test:8443"
        os.environ["JAMF_PRO_SECRET_ID"] = "s"
        self.addCleanup(os.environ.pop, "JAMF_PRO_URL", None)
        self.addCleanup(os.environ.pop, "JAMF_PRO_SECRET_ID", None)
        with patch.object(self.handler, "_run_jamf_pro_drift_check", side_effect=spy):
            self._run("149.0.7827.54", "150.0.7871.190")
        self.assertEqual(captured["googlechrome"]["version"], "150.0.7871.190")


if __name__ == "__main__":
    unittest.main()
