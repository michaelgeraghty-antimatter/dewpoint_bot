# Dewpoint Slack bot

Posts a warning to Slack when the forecast says the temperature will be within 5 °F of
the dewpoint (condensation / fog / frost risk) at any point in the next 24 hours.

A single Python file using only the standard library. The only secret it needs is a Slack webhook URL.
It's designed to run on a small Linux VM at each site and post to that site's channel(s).

## How it works

```
systemd timer (hourly) ──> dewpoint_bot.py
                             │ 1. ZIP -> lat/lon      api.zippopotam.us     (free, no key)
                             │ 2. hourly forecast     api.open-meteo.com    (temp + dewpoint in °F, free, no key)
                             │ 3. find hours where temp - dewpoint <= SPREAD_F, group into windows
                             │ 4. check state.json so it doesn't repeat the same alert
                             └──> Slack incoming webhook(s), one per channel
```

When it alerts:
- **Risk appears**: posts a warning listing each risk window, the closest hour, temp,
  dewpoint, spread and RH. If the spread gets to ≤ 0.5 °F it also says "at dewpoint".
- **Risk persists**: stays quiet, then posts again every `REALERT_HOURS` (default 12).
- **Risk clears**: posts an all-clear (turn this off with `SEND_ALL_CLEAR=0`).

Example message:

> 💧 **Dewpoint warning for Warehouse 3 — Miami, FL (33101)**
> Forecast temperature comes within 5°F of the dewpoint in the next 24h:
> • Fri 9PM–Sat 10AM GMT-4: closest Sat 7AM, temp 76°F / dewpoint 74°F (spread 1.8°F, RH 94%)

## Slack setup (once)

Create a single Slack app for all sites, then add a webhook for each channel:

1. Go to https://api.slack.com/apps, then *Create New App* → *From scratch* (name it e.g. "Dewpoint Bot").
2. *Incoming Webhooks* → turn it on.
3. Click *Add New Webhook to Workspace*, pick a channel, and copy the
   `https://hooks.slack.com/services/...` URL. Repeat for each channel. **Each webhook URL posts
   to exactly one channel**, and the channel can't be changed later; add another webhook instead.

Keep track of which URL goes to which channel. Each site's config lists the URLs for the
channels that site should post to.

## Per-site VM setup

Works on any Linux VM with systemd, git and Python 3.7+ (Debian/Ubuntu: `sudo apt-get install -y git python3`).

### 1. Get the code onto the VM (private repo, read-only deploy key)

Each VM gets its own read-only SSH key, so you can revoke one site without affecting the others.

```bash
sudo ssh-keygen -t ed25519 -N "" -C "dewpoint-bot@$(hostname)" -f /root/.ssh/dewpoint_deploy
sudo cat /root/.ssh/dewpoint_deploy.pub
```

On GitHub, open the repo's *Settings* → *Deploy keys* → *Add deploy key*. Paste the public key,
name it after the site, and leave *Allow write access* **unchecked**. Then clone:

```bash
sudo GIT_SSH_COMMAND="ssh -i /root/.ssh/dewpoint_deploy -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new" \
  git clone git@github.com:michaelgeraghty-antimatter/dewpoint_bot.git /usr/local/src/dewpoint-bot
sudo git -C /usr/local/src/dewpoint-bot config core.sshCommand "ssh -i /root/.ssh/dewpoint_deploy -o IdentitiesOnly=yes"
cd /usr/local/src/dewpoint-bot
```

The clone remembers the key, so later updates are just `sudo git pull` (see the table below).

### 2. Install

```bash
sudo ./install.sh
```

The first run creates `/etc/dewpoint-bot.env` and stops. Fill it in:

```bash
SITE_NAME="Warehouse 3"
ZIP_CODE="33101"
# one URL per channel; comma-separate to post to several channels
SLACK_WEBHOOK_URL="https://hooks.slack.com/services/AAA,https://hooks.slack.com/services/BBB"
```

Then run it again with `--test`. The installer copies the bot to `/opt/dewpoint-bot`, runs a dry
run, posts a "set up and will post here" message to each channel so you can confirm the routing,
and starts the hourly timer:

```bash
sudo ./install.sh --test
```

Day to day:

| Task | Command |
|---|---|
| See logs / failures | `journalctl -u dewpoint-bot.service` |
| Run a check now | `sudo systemctl start dewpoint-bot.service` |
| When's the next run | `systemctl list-timers dewpoint-bot.timer` |
| Change ZIP/channels | edit `/etc/dewpoint-bot.env` (applies on the next run) |
| Update to latest | `cd /usr/local/src/dewpoint-bot && sudo git pull && sudo ./install.sh` |
| Remove | `sudo ./install.sh --uninstall` |

The service runs as an unprivileged throwaway user (systemd `DynamicUser`). Its state is kept in
`/var/lib/dewpoint-bot/state.json`. If one channel's webhook fails, the others are still posted
to, and the failure appears in the journal (webhook URLs are never logged).

**Firewall:** the VM needs outbound HTTPS to `api.zippopotam.us`, `api.open-meteo.com` and
`hooks.slack.com`, plus outbound SSH (port 22) to `github.com` for clones and pulls. Nothing inbound.
If port 22 is blocked, GitHub also accepts SSH on port 443 at `ssh.github.com`. Add this to
`/root/.ssh/config`:

```
Host github.com
  HostName ssh.github.com
  Port 443
```

**Know this:** if a VM is down or offline, its site stops getting alerts, and no message
says so. The silence looks the same as "no risk".

## Configuration (`/etc/dewpoint-bot.env`)

| Var | Default | Meaning |
|---|---|---|
| `ZIP_CODE` | required | Postal code to watch |
| `SLACK_WEBHOOK_URL` | required | Incoming webhook URL(s), comma-separated, one per channel |
| `SITE_NAME` | none | Label at the start of messages, e.g. "Warehouse 3" |
| `COUNTRY` | `us` | Country for the postal code (zippopotam.us codes, e.g. `ca`, `gb`) |
| `SPREAD_F` | `5` | Alert threshold: temp − dewpoint ≤ this many °F |
| `HOURS` | `24` | Look-ahead window |
| `REALERT_HOURS` | `12` | Re-post interval while the risk continues |
| `SEND_ALL_CLEAR` | `1` | Post when the risk goes away |

## Command-line flags

| Flag | Does |
|---|---|
| `--dry-run` | Fetch the forecast and print what it would post. Posts nothing and saves no state. |
| `--test` | Post a setup-check message to every webhook. Alert state is untouched. |
| `--force` | Post the warning if there's risk, even if it already alerted recently. |

Trying it on any machine without installing it:

```bash
ZIP_CODE=10001 SITE_NAME="Test" python3 dewpoint_bot.py --dry-run
```

## Notes / extensions

- One VM can also watch several sites. Run one job per ZIP, each with its own `STATE_FILE`.
- To switch to NWS (api.weather.gov, US only), replace `hourly_forecast()`. The rest of the script stays the same.
- For a frost-specific alert, also check `temp <= 32` together with a small spread.
