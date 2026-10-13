# Limited Sniper

Snipes Roblox UGC limiteds (new stock **and** resale listings, including classic
limiteds) at or below the max price you set per item.

**v2 is a full rewrite**: plain readable Python, no obfuscation, no telemetry,
no bundled executables. The program only talks to:

| Host | Why |
|---|---|
| `*.roblox.com` | checking items and buying them |
| your Discord webhook / bot | only if you enable them |
| your own proxies / your own Tor | only if you enable them |
| `raw.githubusercontent.com/samiko121/Limited-Sniper` | only if `Update_check` is `true` |

A test (`tests/test_misc.py`) fails if any other host shows up in the code.

> Automated buying is against Roblox's Terms of Use. Any sniper can get an
> account banned. Use alt accounts for checking and start with `Dry_run`.

## Quick start

1. Install **Python 3.11 or newer** from [python.org](https://www.python.org/downloads/) (tick "Add to PATH").
2. Double-click `start.bat` (Windows) or run `./start.sh` (Linux/macOS).
   The first run creates a private virtual environment, installs the pinned
   dependencies (verified by hash) and creates `config.json`.
3. Put your cookie and items into `config.json` (see below) and start again.
4. Optional: `start.bat selftest` checks your cookies and the Roblox endpoints without buying.
5. The example config starts in **dry run** (nothing is bought). Set
   `"Dry_run": false` when the log shows it would buy what you expect.

`config.json`, `proxies.txt`, `data/` and `logs/` are git-ignored, so your
secrets are never committed and `git pull` updates never overwrite them.
Config files from v1.x are migrated automatically.

## config.json

```jsonc
{
    "Cookies": {
        "Buy_cookie": "",                 // account that buys (see "Keeping cookies safe")
        "Check_cookies": [],              // optional alt accounts used only for checking
        "Use_buy_cookie_for_checks": false, // false = check anonymously when no alts are set
        "Refresh_on_start": false         // old "Bypass_cookies": re-issue the cookie for this IP/region
    },
    "Items": {
        "15181542808": 1,                               // item id: max price
        "13335138883": {"max_price": 5, "buy_limit": 2} // with its own buy limit
    },
    "Global_buy_limit": {"Enabled": false, "Limit": 3}, // max copies to own per item
    "Safety": {
        "Dry_run": true,                  // log/notify only, never buy
        "Max_price_per_purchase": 0,      // hard cap per purchase, 0 = off (protects against typos)
        "Daily_spend_limit": 0,           // max Robux per day, 0 = off
        "Keep_robux_reserve": 0           // never go below this balance
    },
    "Watchers": {
        "Marketplace_batch": true,        // apis.roblox.com, 120 items per request, ~55 ms - the main watcher
        "Catalog_batch": true,            // catalog.roblox.com, 120 items per request, but ~1 req/s allowed
        "Economy_single": true,           // economy.roblox.com, one item per request, generous limit
        "Marketplace_batch_size": 120,
        "Catalog_batch_size": 120,
        "Rate_per_lane": {"Marketplace": 2, "Catalog": 0.5, "Economy": 15}, // max requests/s per endpoint per lane
        "Workers": {"Marketplace": 2, "Catalog": 1, "Economy": 8}         // parallel requests per lane
    },
    "Proxies": {
        "Enabled": false,
        "File": "proxies.txt",            // see proxies.example.txt for formats
        "Proxies_only": false,            // false = also check from your own IP
        "Send_check_cookie": false,       // false = proxies check anonymously
        "Max_failures": 5,                // bench a proxy after this many errors in a row
        "Tor": {"Enabled": false, "Path": "", "Instances": 10, "Start_port": 9080}
    },
    "Webhook": {"Enabled": false, "Url": "", "Send_fails": false, "UserID_To_Ping": 0},
    "Discord": {
        "Enabled": false, "Token": "", "Owner_ids": [], "Startup_message": false,
        "Prefix": {"Enabled": false, "Prefix": "!"}
    },
    "Console": {"Dashboard": true},
    "Auto_restart": {"Enabled": false, "Minutes": 60},
    "Update_check": false
}
```

You can edit `config.json` while the sniper runs; it is reloaded automatically.
Buy limits also respect Roblox's own per-user quantity limit.

## Keeping cookies safe

Any secret field (`Buy_cookie`, entries of `Check_cookies`, `Discord.Token`) accepts:

* the raw value, which works but sits in plain text in `config.json`
* `"env:VARIABLE_NAME"` to read an environment variable
* `"keyring:name"` to read Windows Credential Manager / macOS Keychain / Linux Secret Service.
  Run `start.bat set-secret buy` (or `discord`, or any name such as `alt1` for a
  check cookie) and paste the value; it is stored in the OS and `config.json` is
  updated to point at it. On headless Linux without a keyring use `env:`.

Cookies and tokens are masked in all logs. Purchases always go directly from
your machine with the buy cookie, never through a proxy.

## Controlling the sniper

### Console

The dashboard shows account, Robux, checks per second, latency, buys, errors
and recent events. Type a command and press Enter. Pressing Enter on an empty
line pauses the dashboard while you type.

```
stats  list  add <id|link> <max> [limit]  mass_add <max> <id> <id> ...
remove <id>  remove_all  price <id> <max>  price_all <max>  limit <id> <n|none>
focus <id> <max>  unfocus  pause  resume  dry_run on|off  history [n]  lanes  restart  quit
```

Start with `--no-dashboard` for plain log output (e.g. on a server).

### Discord bot

1. Create an application at <https://discord.com/developers/applications>, add a bot, copy its token.
2. Invite it with the `bot` and `applications.commands` scopes.
3. Set `Discord.Enabled`, `Token` (preferably `keyring:discord`) and **`Owner_ids`**.
   The bot refuses to start without at least one owner. Only owners can use it,
   and replies are only visible to them.
4. For prefix commands (`!stats`) also enable `Prefix.Enabled` and the
   *Message Content* intent in the developer portal. Slash commands need no intents.

Commands: `/stats /list_items /add_item /mass_add_ids /remove_item /remove_all
/change_max_price /mass_change_max_prices /set_buy_limit /focus /unfocus /pause
/resume /dry_run /history /lanes /add_user /remove_user /owner_ids /ping /restart /help`

## Scaling: alts, proxies, Tor

Checks run on **lanes**. A lane is your own IP, an alt cookie, or a proxy, and
each has its own rate limit. When Roblox answers 429 a lane halves its speed and
cools down, then ramps back up. Lanes that keep failing are benched. More lanes
means more checks per second.

* **Alt accounts:** add cookies to `Check_cookies`; each one becomes a lane.
* **Proxies:** copy `proxies.example.txt` to `proxies.txt`, set `Proxies.Enabled`.
  HTTP and SOCKS5 proxies with or without auth are supported.
* **Tor:** download the official *Tor Expert Bundle* from
  <https://www.torproject.org/download/tor/>, set `Proxies.Tor.Path` to `tor.exe`
  and `Proxies.Tor.Enabled` + `Proxies.Enabled` to `true`. Tor runs as a normal
  child process (no admin rights, no Windows service) with one circuit per port.
  Roblox throttles many Tor exits, so private proxies or alts usually work better.

## How it works

```
watchers (marketplace / catalog / economy) ──► offer ≤ max price?
          │ round-robin over your items, via lanes      │
          ▼                                            ▼
   resolver learns collectible ids          buyer: fetch the live listing,
                                            re-check price, safety limits and
                                            buy limit, then POST purchase-item /
                                            purchase-resale (one idempotency key,
                                            safe network retry) → history, webhook
```

## Development

```
python -m venv .venv
.venv/bin/pip install --require-hashes -r requirements.txt
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

To change dependencies, edit `requirements.in` and regenerate `requirements.txt`
with the command at the top of that file.
