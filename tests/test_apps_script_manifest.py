"""Guard the Apps Script manifest against the code it ships with.

Why these tests exist (2026-09-20): `appsscript.json` lists explicit
`oauthScopes`, and explicit scopes switch off Apps Script's auto-detection,
so the script is granted exactly what is listed and nothing else. The
manifest had carried the same three scopes since the first commit while
`Code.gs` grew calls to `ScriptApp.newTrigger` (Google's reference: requires
`script.scriptapp`) and `SpreadsheetApp.openById` (requires `spreadsheets`).

Nobody noticed, because the original deployment was pasted into the editor
WITHOUT the manifest and has always run on auto-detected scopes. A cloner
who followed SETUP ("paste the manifest", or `clasp push`, which always
ships it) would have met "You do not have permission to call
ScriptApp.newTrigger" on `setupTrigger()`, the one REQUIRED step of SETUP
5.5. That is an M1-shaped failure: right in the repo, absent in effect. It
was found by a cold-start walkthrough and checked against Google's
reference pages; it has not been reproduced by execution.

The map below is deliberately small and explicit. A new Apps Script service
in Code.gs fails the second test until someone adds it here with the scope
Google documents for it.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CODE_GS = REPO / "apps-script" / "Code.gs"
MANIFEST = REPO / "apps-script" / "appsscript.json"

_AUTH = "https://www.googleapis.com/auth/"

# service -> scopes of which AT LEAST ONE must be listed for every call this
# repo makes on it. GmailApp: `search` reads, `markRead` modifies; Google's
# reference accepts the full-mailbox scope "or appropriate scopes from the
# related REST API", and gmail.modify is that narrower scope.
_REQUIRED = {
    "ScriptApp": {_AUTH + "script.scriptapp"},
    "SpreadsheetApp": {_AUTH + "spreadsheets"},
    "UrlFetchApp": {_AUTH + "script.external_request"},
    "GmailApp": {"https://mail.google.com/", _AUTH + "gmail.modify"},
}

# Built-in services that need no OAuth scope for the calls made here.
_NO_SCOPE = {"PropertiesService", "Utilities", "Logger", "Session", "console"}

_SERVICE_CALL = re.compile(r"\b([A-Z][A-Za-z]+(?:App|Service)|Utilities|Logger|Session)\.[A-Za-z]")


def _services_used() -> set[str]:
    code = CODE_GS.read_text(encoding="utf-8")
    code = re.sub(r"/\*.*?\*/", "", code, flags=re.S)      # block comments
    code = re.sub(r"(?m)^\s*//.*$", "", code)              # line comments
    return set(_SERVICE_CALL.findall(code))


def _scopes() -> set[str]:
    return set(json.loads(MANIFEST.read_text(encoding="utf-8"))["oauthScopes"])


class TestManifestScopes:
    def test_every_service_code_gs_calls_has_a_scope_in_the_manifest(self):
        scopes = _scopes()
        missing = {
            service: sorted(accepted)
            for service, accepted in _REQUIRED.items()
            if service in _services_used() and not (accepted & scopes)
        }
        assert missing == {}, (
            "appsscript.json lists explicit oauthScopes, so these calls would "
            f"throw a permission error at run time: {missing}")

    def test_no_service_is_used_that_this_guard_does_not_know(self):
        unknown = _services_used() - set(_REQUIRED) - _NO_SCOPE
        assert unknown == set(), (
            f"Code.gs calls {sorted(unknown)}; add each to _REQUIRED with the "
            "scope Google's reference documents, and to appsscript.json")

    def test_the_trigger_scope_is_present_because_setup_requires_setuptrigger(self):
        """Pinned on its own: `setupTrigger()` is the required step of SETUP
        5.5, and this is the scope whose absence broke it."""
        assert "ScriptApp" in _services_used()
        assert _AUTH + "script.scriptapp" in _scopes()
