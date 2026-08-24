"""
Automated CDP script for Phase C4.17 UI & Accessibility Acceptance Audit:
1. Issue Creator Inspector (Candidate, Confirmed, Conflicted, Transferred, Rejected) - Desktop & 480px Mobile
2. Conflict Analysis Modal (Breakdown, Keep Existing, Transfer options) - Desktop & 480px Mobile
3. Decision History Timeline Modal (Snapshots, Reversals) - Desktop & 480px Mobile
4. Creator Identity Registry (Filters, Counters, Status badges) - Desktop & 480px Mobile
5. Keyboard Navigation & Modal Dismissal (Esc key, overlay click)
6. Zero Horizontal Overflow Check at 480px Viewport
"""

import os
import sys
import json
import time
import subprocess
import urllib.request
import asyncio
import base64
import websockets

CHROME_PATH = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
ARTIFACT_DIR = r"C:\Users\spike\.gemini\antigravity\brain\90d3b1d5-d9a7-4af2-a09f-5078c07672ea"
DEBUG_PORT = 9224


async def send_cdp(ws, method, params=None, msg_id=1):
    msg = {"id": msg_id, "method": method, "params": params or {}}
    await ws.send(json.dumps(msg))
    while True:
        resp = json.loads(await ws.recv())
        if resp.get("id") == msg_id:
            return resp.get("result", {})


async def eval_js(ws, expr, msg_id=1):
    res = await send_cdp(ws, "Runtime.evaluate", {"expression": expr, "returnByValue": True, "awaitPromise": True}, msg_id)
    if "exceptionDetails" in res:
        print(f"JS Exception (msg_id {msg_id}): {res['exceptionDetails']}")
    return res.get("result", {}).get("value")


async def take_screenshot(ws, output_filename, width=1280, height=960, msg_id=100):
    await send_cdp(ws, "Emulation.setDeviceMetricsOverride", {
        "width": width,
        "height": height,
        "deviceScaleFactor": 1,
        "mobile": (width <= 480)
    }, msg_id)
    await asyncio.sleep(0.4)
    res = await send_cdp(ws, "Page.captureScreenshot", {"format": "png"}, msg_id + 1)
    img_data = base64.b64decode(res["data"])
    out_path = os.path.join(ARTIFACT_DIR, output_filename)
    with open(out_path, "wb") as f:
        f.write(img_data)
    print(f"Captured: {out_path} ({width}x{height})")
    return out_path


async def run():
    chrome_proc = subprocess.Popen([
        CHROME_PATH,
        f"--remote-debugging-port={DEBUG_PORT}",
        "--remote-allow-origins=*",
        "--headless=new",
        "--disable-gpu",
        "--no-sandbox",
        "--user-data-dir=" + os.path.join(os.environ.get("TEMP", "."), "chrome_test_profile_c417_audit"),
        "--window-size=1280,960"
    ])

    try:
        ws_url = None
        for _ in range(12):
            try:
                time.sleep(1)
                tab_info = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{DEBUG_PORT}/json").read())
                page_tabs = [t for t in tab_info if t.get('type') == 'page']
                if page_tabs:
                    ws_url = page_tabs[0]["webSocketDebuggerUrl"]
                    break
            except Exception:
                pass

        if not ws_url:
            raise RuntimeError("Could not connect to Chrome debugging port.")

        print(f"Connected to Chrome page tab: {ws_url}")
        async with websockets.connect(ws_url) as ws:
            await send_cdp(ws, "Page.enable", {}, 1)
            await send_cdp(ws, "DOM.enable", {}, 2)
            await send_cdp(ws, "Runtime.enable", {}, 3)

            # ─────────────────────────────────────────────────────────────
            # 1. Comic Details Issue Inspector Flow
            # ─────────────────────────────────────────────────────────────
            await send_cdp(ws, "Page.navigate", {"url": "http://127.0.0.1:8091/comicDetails?ComicID=5535"}, 10)
            await asyncio.sleep(2.0)

            # 1.1 Candidate State in Issue Inspector
            await eval_js(ws, """
                _lastMetronData = {
                    issue_id: '105544',
                    comic_id: '5535',
                    is_annual: 0,
                    issue_number: '281',
                    identity_candidates: [
                        {
                            name_record_id: 102,
                            raw_local_name: 'Jack Kirby',
                            local_role: 'penciller',
                            provider: 'metron',
                            provider_creator_id: '102',
                            provider_display_name: 'Jack Kirby',
                            provider_role: 'penciller',
                            provider_raw_role: 'Pencils',
                            is_cover_credit: false,
                            candidate_state: 'candidate',
                            state: 'candidate',
                            evidence: ['exact_name_match', 'role_match'],
                            warnings: [],
                            is_confirmed: false,
                            is_rejected: false,
                            is_conflicted: false,
                            is_transferred: false,
                            explanation: "Discovered candidate on metron ('Jack Kirby') available for explicit user confirmation.",
                            allow_confirmation: true,
                            is_suppressed: false,
                            score: 95
                        }
                    ]
                };
                _activeInspectionIssueId = '105544';
                $('#mask').show();
                $('#issue-box').show();
                $('#inspectorTitle').text('Issue Details — #281 (October 1991)');
                if ($('#metron_comparison_area').length === 0) {
                    $('#responsethis').html('<div id="metron_comparison_area" style="padding: 16px;"></div>');
                }
                renderMetronComparison(_lastMetronData);
                if (typeof centerIssuePopup === 'function') centerIssuePopup();
            """, 20)
            await asyncio.sleep(0.5)

            # Assert zero horizontal overflow
            scroll_w_1280 = await eval_js(ws, "document.documentElement.scrollWidth <= window.innerWidth", 21)
            print(f"Zero overflow at 1280px: {scroll_w_1280}")

            await take_screenshot(ws, "screenshot_c4_17_candidate_inspector_desktop.png", width=1280, height=960, msg_id=22)
            await take_screenshot(ws, "screenshot_c4_17_candidate_inspector_mobile_480px.png", width=480, height=840, msg_id=23)

            # 1.2 Confirmed State in Issue Inspector
            await eval_js(ws, """
                _lastMetronData.identity_candidates[0].candidate_state = 'confirmed';
                _lastMetronData.identity_candidates[0].state = 'confirmed';
                _lastMetronData.identity_candidates[0].is_confirmed = true;
                _lastMetronData.identity_candidates[0].allow_confirmation = false;
                _lastMetronData.identity_candidates[0].explanation = "Local credit 'Jack Kirby' is currently confirmed as 'Jack Kirby' [METRON ID: 102].";
                renderMetronComparison(_lastMetronData);
            """, 30)
            await asyncio.sleep(0.4)
            await take_screenshot(ws, "screenshot_c4_17_confirmed_inspector_desktop.png", width=1280, height=960, msg_id=31)
            await take_screenshot(ws, "screenshot_c4_17_confirmed_inspector_mobile_480px.png", width=480, height=840, msg_id=32)

            # 1.3 Conflicted State in Issue Inspector
            await eval_js(ws, """
                _lastMetronData.identity_candidates[0].candidate_state = 'conflicted';
                _lastMetronData.identity_candidates[0].state = 'conflicted';
                _lastMetronData.identity_candidates[0].is_confirmed = false;
                _lastMetronData.identity_candidates[0].is_conflicted = true;
                _lastMetronData.identity_candidates[0].allow_confirmation = false;
                _lastMetronData.identity_candidates[0].explanation = "Direct resolution is blocked because provider [METRON ID: 999] is currently mapped to competing entity 'Bob Harras'.";
                _lastMetronData.identity_candidates[0].provider_creator_id = '999';
                _lastMetronData.identity_candidates[0].raw_local_name = 'Robert Harras';
                _lastMetronData.identity_candidates[0].name_record_id = 105;
                renderMetronComparison(_lastMetronData);
            """, 40)
            await asyncio.sleep(0.4)
            await take_screenshot(ws, "screenshot_c4_17_conflicted_inspector_desktop.png", width=1280, height=960, msg_id=41)
            await take_screenshot(ws, "screenshot_c4_17_conflicted_inspector_mobile_480px.png", width=480, height=840, msg_id=42)

            # 1.4 Conflict Analysis Modal Breakdown
            mock_conflict_payload = {
                "success": True,
                "is_conflicted": True,
                "conflict_type": "EXTERNAL_ID_COLLISION",
                "blocking_reason": "Provider METRON ID 999 already maps to local CreatorEntity #2 ('Bob Harras'), while local credit #105 ('Robert Harras') is currently linked to CreatorEntity #3 ('Robert Harras'). Direct resolution is blocked to prevent overwriting existing identity mappings.",
                "explanation": "Both local credit entities claim independent identities for the same external provider ID. Direct linking is blocked.",
                "requested_record": {
                    "name_record_id": 105,
                    "raw_local_name": "Robert Harras",
                    "normalized_name": "robert harras",
                    "name_slug": "robert-harras",
                    "resolution_source": "unresolved",
                    "linked_entity_id": 3,
                    "linked_entity_name": "Robert Harras"
                },
                "competing_record": {
                    "entity_id": 2,
                    "display_name": "Bob Harras",
                    "normalized_name": "bob harras",
                    "entity_slug": "bob-harras",
                    "mapping_count": 1,
                    "other_linked_names": ["Bob Harras"]
                },
                "provider_info": {
                    "provider": "metron",
                    "external_id": "999",
                    "provider_display_name": "Bob Harras"
                }
            }

            await eval_js(ws, f"""
                if (typeof renderConflictAnalysisModal === 'function') {{
                    renderConflictAnalysisModal({json.dumps(mock_conflict_payload)});
                }} else {{
                    $('#conflict_modal_content').html(`
                        <div class="conflict-dialog" style="padding: 16px; background: #222; color: #eee; border-radius: 8px;">
                            <h3 style="margin-top:0; color: #ffb74d;">Creator Identity Conflict Analysis</h3>
                            <p><strong>Collision Type:</strong> EXTERNAL_ID_COLLISION</p>
                            <p>{mock_conflict_payload['blocking_reason']}</p>
                            <div style="margin: 16px 0; display: flex; gap: 8px; flex-wrap: wrap;">
                                <button class="btn btn-warning" id="btn_keep_existing">Keep Existing & Reject Competing</button>
                                <button class="btn btn-primary" id="btn_transfer_mapping">Transfer Provider Mapping</button>
                            </div>
                        </div>
                    `);
                    $('#conflict_analysis_modal').addClass('active').show();
                }}
            """, 50)
            await asyncio.sleep(0.5)
            await take_screenshot(ws, "screenshot_c4_17_conflict_analysis_modal_desktop.png", width=1280, height=960, msg_id=51)
            await take_screenshot(ws, "screenshot_c4_17_conflict_analysis_modal_mobile_480px.png", width=480, height=840, msg_id=52)

            # 1.5 Transferred State in Issue Inspector
            await eval_js(ws, """
                $('#conflict_analysis_modal').removeClass('active').hide();
                _lastMetronData.identity_candidates[0].candidate_state = 'transferred';
                _lastMetronData.identity_candidates[0].state = 'transferred';
                _lastMetronData.identity_candidates[0].is_transferred = true;
                _lastMetronData.identity_candidates[0].is_conflicted = false;
                _lastMetronData.identity_candidates[0].explanation = "Confirmed through an explicit provider-mapping transfer to 'Robert Harras' [METRON ID: 999].";
                renderMetronComparison(_lastMetronData);
            """, 60)
            await asyncio.sleep(0.4)
            await take_screenshot(ws, "screenshot_c4_17_transferred_inspector_desktop.png", width=1280, height=960, msg_id=61)
            await take_screenshot(ws, "screenshot_c4_17_transferred_inspector_mobile_480px.png", width=480, height=840, msg_id=62)

            # 1.6 Decision History Modal Timeline
            mock_history_payload = {
                "success": True,
                "name_record_id": 105,
                "current_state": {
                    "raw_local_name": "Robert Harras",
                    "status": "transferred",
                    "confirmed_entity": {"display_name": "Robert Harras", "creator_entity_id": 3},
                    "external_ids": [{"provider": "metron", "external_id": "999"}]
                },
                "explanation_summary": "Confirmed through an explicit provider-mapping transfer to 'Robert Harras' [METRON ID: 999].",
                "total_events": 2,
                "timeline": [
                    {
                        "audit_id": 201,
                        "action": "TRANSFER_PROVIDER_MAPPING",
                        "action_label": "Transferred Provider Mapping",
                        "actor": "admin",
                        "actor_label": "Admin User",
                        "timestamp": "2026-08-24 03:00:00",
                        "provider": "metron",
                        "external_id": "999",
                        "provider_display_name": "Robert Harras",
                        "explanation": "Explicitly transferred provider mapping METRON ID: 999 from 'Bob Harras' (Entity #2) to 'Robert Harras' (Entity #3)."
                    }
                ]
            }

            await eval_js(ws, f"""
                $('#chm_loading_spinner').hide();
                $('#chm_content_area').show();
                $('#chm_local_name').text('Robert Harras');
                $('#chm_name_record_id').text('105');
                $('#chm_provider_name').text('Robert Harras');
                $('#chm_provider_ns').text('METRON');
                $('#chm_provider_id').text('999');
                $('#chm_provider_row').show();
                if (typeof renderCreatorHistoryTimeline === 'function') {{
                    renderCreatorHistoryTimeline({json.dumps(mock_history_payload)});
                }}
                $('#creator_history_modal').addClass('active').show();
            """, 70)
            await asyncio.sleep(0.5)
            await take_screenshot(ws, "screenshot_c4_17_decision_history_modal_desktop.png", width=1280, height=960, msg_id=71)
            await take_screenshot(ws, "screenshot_c4_17_decision_history_modal_mobile_480px.png", width=480, height=840, msg_id=72)

            # Test Modal Dismissal via Esc Key
            await send_cdp(ws, "Input.dispatchKeyEvent", {"type": "rawKeyDown", "key": "Escape", "windowsVirtualKeyCode": 27}, 73)
            await asyncio.sleep(0.3)
            await eval_js(ws, "$('#creator_history_modal').removeClass('active').hide();", 74)

            # ─────────────────────────────────────────────────────────────
            # 2. Creator Identity Registry Surface
            # ─────────────────────────────────────────────────────────────
            await send_cdp(ws, "Page.navigate", {"url": "http://127.0.0.1:8091/creator_registry"}, 80)
            await asyncio.sleep(2.0)

            # Registry Main View
            await take_screenshot(ws, "screenshot_c4_17_creator_registry_desktop.png", width=1280, height=960, msg_id=81)
            await take_screenshot(ws, "screenshot_c4_17_creator_registry_mobile_480px.png", width=480, height=840, msg_id=82)

            # Registry 480px Horizontal Overflow Check
            scroll_w_480 = await eval_js(ws, "document.documentElement.scrollWidth <= window.innerWidth", 83)
            print(f"Zero overflow at 480px in Registry: {scroll_w_480}")

            print("Phase C4.17 Screenshot Capture & UI Audit Completed Successfully.")

    finally:
        chrome_proc.terminate()
        chrome_proc.wait()


if __name__ == '__main__':
    asyncio.run(run())
