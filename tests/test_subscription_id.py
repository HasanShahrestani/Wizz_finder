import json

from wizz_finder.portal import UUID_RE, subscription_id_from_recording

SUB_ID = "9da7635e-870f-45ff-8105-3c66635b08ea"


def _recording(tmp_path, rows):
    path = tmp_path / "rec.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return path


def test_finds_id_in_availability_request(tmp_path):
    rows = [
        {"url": "https://multipass.wizzair.com/w6/translations/en", "status": 200, "body": {}},
        {"url": f"https://multipass.wizzair.com/w6/subscriptions/json/availability/{SUB_ID}",
         "status": 200, "body": {"flightsOutbound": []}},
    ]
    assert subscription_id_from_recording(_recording(tmp_path, rows)) == SUB_ID


def test_ignores_unrelated_uuids(tmp_path):
    rows = [{"url": "https://multipass.wizzair.com/w6/subscriptions/profile/"
                    "99575169-fcb1-4ba9-b20d-6b710d530281/bookings", "status": 200, "body": {}}]
    assert subscription_id_from_recording(_recording(tmp_path, rows)) is None


def test_missing_file_returns_none(tmp_path):
    assert subscription_id_from_recording(tmp_path / "nope.jsonl") is None


def test_uuid_pattern_rejects_junk():
    assert UUID_RE.match(SUB_ID)
    assert not UUID_RE.match("not-a-uuid")
