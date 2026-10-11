import json

import pytest

from sniper.config import ConfigError, ConfigStore, WatchItem, migrate_legacy, parse_item_ref


def test_parse_item_ref():
    assert parse_item_ref("15181542808") == 15181542808
    assert parse_item_ref("https://www.roblox.com/catalog/1028606/Red-Baseball-Cap") == 1028606
    assert parse_item_ref("https://www.roblox.com/de/catalog/1028606/x") == 1028606
    for bad in ("abc", "https://evil.example/catalog/1", "-5", "0"):
        with pytest.raises(ValueError):
            parse_item_ref(bad)


def test_items_both_forms(config_file):
    store = config_file(Items={"1": 5, "2": {"max_price": 9, "buy_limit": 2}})
    assert store.settings.items == {1: WatchItem(1, 5), 2: WatchItem(2, 9, 2)}


@pytest.mark.parametrize("items", [{"1": -1}, {"1": "5"}, {"1": True}, {"1": {"max_price": 5, "buy_limit": 0}}, {"x": 1}])
def test_bad_items_rejected(config_file, items):
    with pytest.raises(ConfigError):
        config_file(Items=items)


def test_webhook_must_be_discord(config_file):
    with pytest.raises(ConfigError):
        config_file(Webhook={"Enabled": True, "Url": "https://example.com/hook"})
    store = config_file(Webhook={"Enabled": True, "Url": "https://discord.com/api/webhooks/123/abc-DEF_1"})
    assert store.settings.webhook_enabled


def test_legacy_v1_config_migrates(tmp_path):
    old = {
        "Webhook": {"Enabled": False, "Send_Fails": True, "UserID_To_Ping": 0, "Url": ""},
        "Cookies": {"Buy_cookie": "a" * 20, "Check_cookie": "b" * 20, "Bypass_cookies": True},
        "Global_buy_limit": {"Enabled": True, "Limit": 3},
        "Items": {"15181542808": 1},
        "Auto-Restart": {"Enabled": True, "Seconds": 120},
        "Proxies": {"Enabled": False, "Amount": 20, "Max_Threads": 100},
        "Global_Logs": {"Anonymous": False},
        "Discord": {"Enabled": False, "Token": "", "StartUp_Message": True,
                    "Use_Prefix": {"Enabled": False, "Prefix": "!"}, "Owner_id": [123]},
    }
    p = tmp_path / "config.json"
    p.write_text(json.dumps(old))
    s = ConfigStore(p).load()
    assert s.check_cookies == ["b" * 20]
    assert s.refresh_cookies_on_start is True
    assert s.webhook_send_fails is True
    assert s.auto_restart_minutes == 2
    assert s.tor_instances == 20
    assert s.discord_owner_ids == [123]
    assert s.global_buy_limit == 3
    assert "Global_Logs" not in migrate_legacy(old)


def test_atomic_save_roundtrip(config_file):
    store = config_file(Items={"1": 5})
    store.set_items({1: WatchItem(1, 7), 3: WatchItem(3, 1, 4)})
    again = ConfigStore(store.path)
    s = again.load()
    assert s.items == {1: WatchItem(1, 7), 3: WatchItem(3, 1, 4)}
    assert not store.changed_on_disk()
    assert not list(store.path.parent.glob(".config-*"))


def test_invalid_update_is_rolled_back(config_file):
    store = config_file()
    with pytest.raises(ConfigError):
        store.update_setting("Safety", "Daily_spend_limit", -1)
    assert store.raw["Safety"]["Daily_spend_limit"] == 0


def test_bad_json_message(tmp_path):
    p = tmp_path / "config.json"
    p.write_text('{"Items": {"1": 5,}}')
    with pytest.raises(ConfigError, match="line 1"):
        ConfigStore(p).load()
