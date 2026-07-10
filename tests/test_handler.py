# ABOUTME: Unit tests for the jamf-patch-sync Lambda handler.
# ABOUTME: Covers version check, update flow, idempotency, auth, and error paths.

import io
import json
import os
import sys
import types
import unittest
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
        with patch("handler.urllib.request.urlopen", side_effect=urllib.error.URLError("boom")):
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


if __name__ == "__main__":
    unittest.main()
