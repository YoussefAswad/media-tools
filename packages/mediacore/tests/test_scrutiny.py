from __future__ import annotations

from mediacore.scrutiny import ScrutinyClient

# real Scrutiny shape: data -> summary -> {wwn: {device, smart}}
_SUMMARY = {
    "success": True,
    "data": {
        "summary": {
            "0xabc": {
                "device": {"device_name": "sda", "model_name": "Seagate", "device_status": 0},
                "smart": {"temp": 38, "power_on_hours": 12000},
            },
            "0xdef": {
                "device": {"device_name": "nvme0n1", "model_name": "Samsung", "device_status": 2},
                "smart": {"temp": 45, "power_on_hours": 8000},
            },
        }
    },
}


def _client(monkeypatch, payload):
    c = ScrutinyClient("http://scrutiny:8080")
    monkeypatch.setattr(c, "_get_json", lambda path: payload)
    return c


def test_drives_parsed_and_sorted(monkeypatch) -> None:
    drives = _client(monkeypatch, _SUMMARY).drives()
    assert drives is not None
    assert [d.name for d in drives] == ["nvme0n1", "sda"]  # sorted
    sda = next(d for d in drives if d.name == "sda")
    assert sda.passed and sda.temp == 38 and sda.power_on_hours == 12000
    nvme = next(d for d in drives if d.name == "nvme0n1")
    assert not nvme.passed  # device_status 2 = failed


def test_drives_unreachable(monkeypatch) -> None:
    assert _client(monkeypatch, None).drives() is None


def test_drives_unexpected_body(monkeypatch) -> None:
    assert _client(monkeypatch, {"success": False}).drives() is None  # no "data"
    assert _client(monkeypatch, {"data": {}}).drives() is None  # no "summary"


def test_drives_missing_smart_block(monkeypatch) -> None:
    payload = {"data": {"summary": {"0x1": {"device": {"device_name": "sdb", "device_status": 0}}}}}
    drives = _client(monkeypatch, payload).drives()
    assert drives is not None and drives[0].temp is None and drives[0].power_on_hours is None
