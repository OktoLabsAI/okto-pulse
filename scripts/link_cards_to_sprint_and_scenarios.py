"""Link the freshly created onboarding test cards to scenarios and the sprint."""
import json
import urllib.request
import urllib.error

BOARD_ID = "617aa40d-8cba-4bec-a930-27740e53c66e"
SPEC_ID = "da2f7a14-f8bf-4128-adad-7fb4015ac0ae"
SPRINT_ID = "8f9c9fe8-5957-4c8d-baa1-934e32d51f80"
BASE = "http://127.0.0.1:8113/api/v1"


def call(method, url, payload=None):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"} if data else {},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"  HTTP {e.code}: {body[:600]}")
        raise


# Get all card IDs from the spec
status, spec = call("GET", f"{BASE}/specs/{SPEC_ID}")
cards_by_title = {c["title"]: c["id"] for c in spec.get("cards", [])}
print(f"Found {len(cards_by_title)} cards in spec")

# Map: card title -> list of scenario ids it covers
mapping = {
    "Vitest suite — unit + integration": ["TS-3", "TS-4", "TS-6", "TS-8", "TS-9", "TS-11", "TS-12"],
    "Playwright suite — e2e": ["TS-1", "TS-2", "TS-7", "TS-10"],
    "Manual contrast audit (WCAG AA) — light variant": ["TS-5"],
}

# 1) Link each test card to each scenario it covers
print("\n--- linking scenarios -> cards ---")
for title, scenarios in mapping.items():
    cid = cards_by_title[title]
    for sid in scenarios:
        status, _ = call("POST", f"{BASE}/specs/{SPEC_ID}/scenarios/{sid}/link-task/{cid}")
        print(f"  {sid} <- {cid[:8]} ({title[:40]}) HTTP {status}")

# 2) Assign all 10 cards to the sprint
print("\n--- assigning all 10 cards to sprint ---")
all_card_ids = list(cards_by_title.values())
status, body = call(
    "POST",
    f"{BASE}/sprints/{SPRINT_ID}/assign-tasks",
    {"card_ids": all_card_ids},
)
print(f"  HTTP {status} | sprint cards now: {len(body.get('cards', []))}")

# Confirm
status, spec_after = call("GET", f"{BASE}/specs/{SPEC_ID}")
covered = sum(1 for ts in spec_after["test_scenarios"] if ts.get("linked_task_ids"))
print(f"\nFinal coverage: {covered}/12 scenarios have linked test cards")
