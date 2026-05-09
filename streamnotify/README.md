# StreamNotify — Red Discord Bot Cog

Posts live stream and clip notifications with rich embeds to a designated channel.
Supports **Twitch**, **YouTube**, **Kick**, and **TikTok** (TikTok live detection is limited by their API).

---

## Installation

1. Copy the `streamnotify/` folder into your Red cogs directory.
2. In Discord, run:
   ```
   [p]addpath /path/to/your/cogs/directory
   [p]load streamnotify
   ```

---

## Admin Setup (run in Discord server)

| Command | Description |
|---|---|
| `[p]streamnotify setchannel #channel` | Set the channel where notifications will be posted |
| `[p]streamnotify setrole @Role` | Set the role a user must have to register channels |
| `[p]streamnotify settwitch <client_id> <client_secret>` | Set Twitch API credentials |
| `[p]streamnotify setyoutube <api_key>` | Set YouTube Data API v3 key |
| `[p]streamnotify block @user` | Block a user from registering channels |
| `[p]streamnotify unblock @user` | Unblock a user |
| `[p]streamnotify blocklist` | View the blocklist |
| `[p]streamnotify liststreamers` | View all registered streamers |
| `[p]streamnotify removestreamer @user` | Remove all channels for a user |
| `[p]streamnotify status` | Show current configuration |

---

## Getting API Keys

### Twitch
1. Go to https://dev.twitch.tv/console
2. Create an application → set OAuth redirect to `http://localhost`
3. Copy your **Client ID** and **Client Secret**
4. Run `[p]streamnotify settwitch <client_id> <client_secret>`

### YouTube
1. Go to https://console.cloud.google.com/
2. Create a project → Enable **YouTube Data API v3**
3. Create an API Key credential
4. Run `[p]streamnotify setyoutube <api_key>`

### Kick
No API key needed — uses Kick's public API.

### TikTok
TikTok does not have a public live-stream detection API. This cog registers TikTok usernames but cannot automatically detect when they go live without an approved TikTok developer account. You can extend the `_check_guild` method with your own TikTok integration.

---

## User Self-Registration (via DM)

Users who have the required role can **DM the bot** to register their channels:

| DM Command | Action |
|---|---|
| `add twitch <username>` | Register Twitch channel |
| `add youtube <channel_id>` | Register YouTube channel (use channel ID, e.g. `UCxxxxxx`) |
| `add kick <username>` | Register Kick channel |
| `add tiktok <username>` | Register TikTok username |
| `remove twitch` | Remove Twitch registration |
| `remove youtube` | Remove YouTube registration |
| `remove kick` | Remove Kick registration |
| `remove tiktok` | Remove TikTok registration |
| `list` | Show your registered channels |
| `help` | Show help |

**To find your YouTube Channel ID:**  
Go to https://www.youtube.com/account_advanced while logged in.

---

## How It Works

- The bot polls all registered channels **every 2 minutes**.
- When a streamer goes live, a rich embed is posted with the stream title, thumbnail (clickable), game/category, and viewer count.
- For YouTube, new video uploads/clips are also detected and posted.
- Once a streamer goes offline, the `live` state is reset so the next stream will trigger a new notification.
- Twitch access tokens are automatically refreshed via client credentials flow.

---

## Notification Embed Example

```
🔴 raiNofchAos™ is LIVE on Twitch!
DMZ LIVE - Friday DMZ!
Playing: Call of Duty: Warzone  |  Viewers: 142
[Thumbnail Image — click to open stream]
Streamer: raiNofchAos™
```

---

## File Structure

```
streamnotify/
├── __init__.py
├── streamnotify.py
├── info.json
└── README.md
```
