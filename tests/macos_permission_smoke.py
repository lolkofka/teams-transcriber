"""Check the native WKUIDelegate's decisions without opening capture devices."""
from types import SimpleNamespace

import WebKit
from Foundation import NSURL

from teams_browser import _mac_teams_delegate

origin = SimpleNamespace(protocol=lambda: 'https', host=lambda: 'teams.cloud.microsoft', port=lambda: 443)
frame = SimpleNamespace(securityOrigin=lambda: origin)
view = SimpleNamespace(URL=lambda: NSURL.URLWithString_('https://teams.cloud.microsoft/'))
delegate = _mac_teams_delegate()
decide = delegate.webView_requestMediaCapturePermissionForOrigin_initiatedByFrame_type_decisionHandler_
decisions = []
for _ in range(2):
    decide(view, origin, frame, WebKit.WKMediaCaptureTypeMicrophone, decisions.append)
assert decisions == [WebKit.WKPermissionDecisionGrant] * 2, decisions
for kind in (WebKit.WKMediaCaptureTypeCamera, WebKit.WKMediaCaptureTypeCameraAndMicrophone):
    decisions.clear()
    decide(view, origin, frame, kind, decisions.append)
    assert decisions == [WebKit.WKPermissionDecisionPrompt], decisions
decisions.clear()
outside = SimpleNamespace(URL=lambda: NSURL.URLWithString_('https://example.com/'))
decide(outside, origin, frame, WebKit.WKMediaCaptureTypeMicrophone, decisions.append)
assert decisions == [WebKit.WKPermissionDecisionPrompt], decisions
print({'ok': True, 'repeated_microphone_requests': 'grant', 'camera_and_other_pages': 'prompt'})
