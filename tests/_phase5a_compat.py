"""
Shared test helpers for Phase 5A compatibility.
After Phase 5A, start_cycle no longer synchronously generates Q1.
This helper provides a reusable function for existing tests.
"""


def start_cycle_and_generate_ga(client, material_id=None):
    """Start a cycle and generate grammar A translation questions.

    After Phase 5A, start_cycle creates 19 planned slots without generation.
    Use this helper in existing tests to restore the old 'Q1 ready' state.
    Returns the response of the generate_module call.
    """
    resp = client.post("/study/start_cycle",
                       data={"material_id": material_id or 1},
                       follow_redirects=False)
    assert resp.status_code in (303, 302), f"start_cycle failed: {resp.status_code}"
    resp2 = client.post("/study/generate_module", follow_redirects=False)
    import json
    data = json.loads(resp2.body) if hasattr(resp2, 'body') else resp2.json()
    return resp, resp2, data
