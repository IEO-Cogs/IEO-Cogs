"""
StreamNotify - Red Discord Bot Cog
Posts live stream and clip notifications to a defined channel.
Users can self-register their YouTube/Twitch/Kick/TikTok channels via DM.
"""

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Optional

import aiohttp
import discord
from redbot.core import commands, Config, checks
from redbot.core.bot import Red

log = logging.getLogger("red.streamnotify")

# Minimum and maximum allowed poll intervals (seconds)
MIN_POLL_INTERVAL = 60       # 1 minute
MAX_POLL_INTERVAL = 3600     # 60 minutes
DEFAULT_POLL_INTERVAL = 120  # 2 minutes

# ---------------------------------------------------------------------------
# Platform checker helpers
# ---------------------------------------------------------------------------

async def check_twitch_live(session: aiohttp.ClientSession, username: str, client_id: str, access_token: str):
    """Return stream data dict if live, else None."""
    url = f"https://api.twitch.tv/helix/streams?user_login={username}"
    headers = {
        "Client-ID": client_id,
        "Authorization": f"Bearer {access_token}",
    }
    try:
        async with session.get(url, headers=headers) as resp:
            if resp.status != 200:
                log.debug(f"[Twitch] HTTP {resp.status} for channel '{username}'")
                return None
            data = await resp.json()
            streams = data.get("data", [])
            if streams:
                stream = streams[0]
                log.debug(f"[Twitch] '{username}' is LIVE — title: {stream.get('title')}")
                # Fetch user info for avatar/display name
                user_url = f"https://api.twitch.tv/helix/users?login={username}"
                async with session.get(user_url, headers=headers) as uresp:
                    udata = await uresp.json() if uresp.status == 200 else {}
                user_info = udata.get("data", [{}])[0]
                stream["profile_image_url"] = user_info.get("profile_image_url", "")
                stream["display_name"] = user_info.get("display_name", username)
                return stream
            log.debug(f"[Twitch] '{username}' is not live")
    except Exception as e:
        log.error(f"[Twitch] Check error for '{username}': {e}")
    return None


async def check_youtube_live(session: aiohttp.ClientSession, channel_id: str, api_key: str):
    """Return live video data if channel is live, else None."""
    search_url = (
        f"https://www.googleapis.com/youtube/v3/search"
        f"?part=snippet&channelId={channel_id}&eventType=live&type=video&key={api_key}"
    )
    try:
        async with session.get(search_url) as resp:
            if resp.status != 200:
                log.debug(f"[YouTube] HTTP {resp.status} for channel '{channel_id}'")
                return None
            data = await resp.json()
            items = data.get("items", [])
            if items:
                item = items[0]
                video_id = item["id"]["videoId"]
                snippet = item["snippet"]
                log.debug(f"[YouTube] Channel '{channel_id}' is LIVE — video: {video_id}")
                return {
                    "video_id": video_id,
                    "title": snippet.get("title", "Live Stream"),
                    "channel_title": snippet.get("channelTitle", channel_id),
                    "thumbnail": snippet.get("thumbnails", {}).get("high", {}).get("url", ""),
                    "url": f"https://www.youtube.com/watch?v={video_id}",
                }
            log.debug(f"[YouTube] Channel '{channel_id}' is not live")
    except Exception as e:
        log.error(f"[YouTube] Live check error for '{channel_id}': {e}")
    return None


async def check_youtube_clips(session: aiohttp.ClientSession, channel_id: str, api_key: str, last_check_time: str):
    """Return list of new video uploads (clips) since last_check_time."""
    search_url = (
        f"https://www.googleapis.com/youtube/v3/search"
        f"?part=snippet&channelId={channel_id}&type=video&order=date"
        f"&publishedAfter={last_check_time}&key={api_key}&maxResults=5"
    )
    clips = []
    try:
        async with session.get(search_url) as resp:
            if resp.status != 200:
                log.debug(f"[YouTube] Clips check HTTP {resp.status} for '{channel_id}'")
                return clips
            data = await resp.json()
            for item in data.get("items", []):
                video_id = item["id"]["videoId"]
                snippet = item["snippet"]
                clips.append({
                    "video_id": video_id,
                    "title": snippet.get("title", "New Video"),
                    "channel_title": snippet.get("channelTitle", channel_id),
                    "thumbnail": snippet.get("thumbnails", {}).get("high", {}).get("url", ""),
                    "url": f"https://www.youtube.com/watch?v={video_id}",
                    "published_at": snippet.get("publishedAt", ""),
                })
            if clips:
                log.debug(f"[YouTube] Found {len(clips)} new clip(s) for channel '{channel_id}'")
    except Exception as e:
        log.error(f"[YouTube] Clips check error for '{channel_id}': {e}")
    return clips


async def check_kick_live(session: aiohttp.ClientSession, username: str):
    """Return stream data if Kick channel is live, else None."""
    url = f"https://kick.com/api/v1/channels/{username}"
    headers = {"Accept": "application/json"}
    try:
        async with session.get(url, headers=headers) as resp:
            if resp.status != 200:
                log.debug(f"[Kick] HTTP {resp.status} for channel '{username}'")
                return None
            data = await resp.json()
            livestream = data.get("livestream")
            if livestream:
                log.debug(f"[Kick] '{username}' is LIVE — title: {livestream.get('session_title')}")
                return {
                    "title": livestream.get("session_title", "Live Stream"),
                    "display_name": data.get("user", {}).get("username", username),
                    "thumbnail": livestream.get("thumbnail", {}).get("url", ""),
                    "url": f"https://kick.com/{username}",
                    "viewer_count": livestream.get("viewer_count", 0),
                    "avatar": data.get("user", {}).get("profile_pic", ""),
                }
            log.debug(f"[Kick] '{username}' is not live")
    except Exception as e:
        log.error(f"[Kick] Check error for '{username}': {e}")
    return None


async def check_tiktok_live(session: aiohttp.ClientSession, username: str):
    """
    Scrape the TikTok profile page to detect if a user is currently live.

    TikTok does not have a public live-stream API so we scrape the profile
    page HTML.  We look for two signals:
      1. The JSON-LD or embedded __NEXT_DATA__ that contains a liveRoomInfo
         or "isLiving" flag.
      2. A fallback: check the /live sub-page for a redirect/title that
         indicates an active broadcast.

    Returns a dict with stream metadata if live, else None.
    """
    handle = username.lstrip("@")
    profile_url = f"https://www.tiktok.com/@{handle}"
    live_url = f"https://www.tiktok.com/@{handle}/live"

    # ENHANCED HEADERS FOR TIKTOK DETECTION
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Referer": "https://www.tiktok.com/",
    }

    # ── Method 1: scrape profile page for NEXT_DATA live signals ─────────────
    try:
        async with session.get(profile_url, headers=headers, allow_redirects=True) as resp:
            if resp.status == 200:
                html = await resp.text()

                # Signal A: liveRoomInfo present in __NEXT_DATA__
                if '"liveRoomInfo"' in html or '"isLiving":true' in html or '"isLiving": true' in html:
                    log.debug(f"[TikTok] '{handle}' LIVE detected via __NEXT_DATA__ liveRoomInfo")
                    # Try to extract title from the page
                    title_match = re.search(r'"title"\s*:\s*"([^"]{3,120})"', html)
                    title = title_match.group(1) if title_match else "TikTok LIVE"
                    # Try to extract display name
                    dn_match = re.search(r'"nickname"\s*:\s*"([^"]{1,60})"', html)
                    display_name = dn_match.group(1) if dn_match else handle
                    # Try to extract avatar
                    av_match = re.search(r'"avatarLarger"\s*:\s*"(https?://[^"]+)"', html)
                    avatar = av_match.group(1).replace("\\u002F", "/") if av_match else ""
                    return {
                        "title": title,
                        "display_name": display_name,
                        "url": live_url,
                        "avatar": avatar,
                        "thumbnail": "",  # TikTok doesn't expose live thumbnails publicly
                    }

                # Signal B: page contains a LIVE badge indicator in meta/og tags
                if re.search(r'is\s+LIVE', html, re.IGNORECASE):
                    log.debug(f"[TikTok] '{handle}' LIVE detected via 'is LIVE' text signal")
                    dn_match = re.search(r'"nickname"\s*:\s*"([^"]{1,60})"', html)
                    display_name = dn_match.group(1) if dn_match else handle
                    av_match = re.search(r'"avatarLarger"\s*:\s*"(https?://[^"]+)"', html)
                    avatar = av_match.group(1).replace("\\u002F", "/") if av_match else ""
                    return {
                        "title": "TikTok LIVE",
                        "display_name": display_name,
                        "url": live_url,
                        "avatar": avatar,
                        "thumbnail": "",
                    }

                log.debug(f"[TikTok] '{handle}' profile page loaded, no live signal found")
            else:
                log.debug(f"[TikTok] Profile page HTTP {resp.status} for '{handle}'")
    except Exception as e:
        log.error(f"[TikTok] Profile scrape error for '{handle}': {e}")

    # ── Method 2: check /live page — TikTok redirects to profile if not live ──
    try:
        async with session.get(live_url, headers=headers, allow_redirects=False) as resp:
            # A 200 on the /live page is a strong signal they're live
            if resp.status == 200:
                live_html = await resp.text()
                # Confirm it's actually a live page, not just a profile fallback
                if '"liveRoomInfo"' in live_html or '"isLiving":true' in live_html or "LIVE" in live_html[:2000]:
                    log.debug(f"[TikTok] '{handle}' LIVE confirmed via /live page (200 + content match)")
                    dn_match = re.search(r'"nickname"\s*:\s*"([^"]{1,60})"', live_html)
                    display_name = dn_match.group(1) if dn_match else handle
                    title_match = re.search(r'"title"\s*:\s*"([^"]{3,120})"', live_html)
                    title = title_match.group(1) if title_match else "TikTok LIVE"
                    av_match = re.search(r'"avatarLarger"\s*:\s*"(https?://[^"]+)"', live_html)
                    avatar = av_match.group(1).replace("\\u002F", "/") if av_match else ""
                    return {
                        "title": title,
                        "display_name": display_name,
                        "url": live_url,
                        "avatar": avatar,
                        "thumbnail": "",
                    }
            elif resp.status in (301, 302, 307, 308):
                # Redirect away from /live = almost certainly not live
                log.debug(f"[TikTok] '{handle}' /live redirected ({resp.status}) → not live")
            else:
                log.debug(f"[TikTok] '{handle}' /live returned HTTP {resp.status}")
    except Exception as e:
        log.error(f"[TikTok] Live page check error for '{handle}': {e}")

    return None


# ---------------------------------------------------------------------------
# Embed builders
# ---------------------------------------------------------------------------

def build_live_embed(platform: str, stream_data: dict, discord_user: discord.Member) -> discord.Embed:
    colour_map = {
        "twitch": 0x9146FF,
        "youtube": 0xFF0000,
        "kick": 0x53FC18,
        "tiktok": 0xFE2C55,  # TikTok brand pink/red
    }
    colour = colour_map.get(platform.lower(), 0x5865F2)

    if platform.lower() == "twitch":
        title = stream_data.get("title", "Live Stream")
        display_name = stream_data.get("display_name", "Streamer")
        url = f"https://www.twitch.tv/{stream_data.get('user_login', '')}"
        thumbnail = stream_data.get("thumbnail_url", "").replace("{width}", "1280").replace("{height}", "720")
        game = stream_data.get("game_name", "")
        viewers = stream_data.get("viewer_count", 0)

        embed = discord.Embed(
            title=f"🔴 {display_name} is LIVE on Twitch!",
            description=f"**{title}**",
            url=url,
            colour=colour,
        )
        if game:
            embed.add_field(name="Playing", value=game, inline=True)
        embed.add_field(name="Viewers", value=str(viewers), inline=True)
        if thumbnail:
            embed.set_image(url=thumbnail)
        avatar = stream_data.get("profile_image_url", "")
        if avatar:
            embed.set_thumbnail(url=avatar)

    elif platform.lower() == "youtube":
        title = stream_data.get("title", "Live Stream")
        display_name = stream_data.get("channel_title", "Creator")
        url = stream_data.get("url", "")
        thumbnail = stream_data.get("thumbnail", "")

        embed = discord.Embed(
            title=f"🔴 {display_name} is LIVE on YouTube!",
            description=f"**{title}**",
            url=url,
            colour=colour,
        )
        if thumbnail:
            embed.set_image(url=thumbnail)

    elif platform.lower() == "kick":
        title = stream_data.get("title", "Live Stream")
        display_name = stream_data.get("display_name", "Streamer")
        url = stream_data.get("url", "")
        thumbnail = stream_data.get("thumbnail", "")
        viewers = stream_data.get("viewer_count", 0)
        avatar = stream_data.get("avatar", "")

        embed = discord.Embed(
            title=f"🟢 {display_name} is LIVE on Kick!",
            description=f"**{title}**",
            url=url,
            colour=colour,
        )
        embed.add_field(name="Viewers", value=str(viewers), inline=True)
        if thumbnail:
            embed.set_image(url=thumbnail)
        if avatar:
            embed.set_thumbnail(url=avatar)

    elif platform.lower() == "tiktok":
        title = stream_data.get("title", "TikTok LIVE")
        display_name = stream_data.get("display_name", "Creator")
        url = stream_data.get("url", "")
        avatar = stream_data.get("avatar", "")
        thumbnail = stream_data.get("thumbnail", "")

        embed = discord.Embed(
            title=f"🎵 {display_name} is LIVE on TikTok!",
            description=f"**{title}**",
            url=url,
            colour=colour,
        )
        if thumbnail:
            embed.set_image(url=thumbnail)
        if avatar:
            embed.set_thumbnail(url=avatar)

    else:
        embed = discord.Embed(
            title=f"🔴 Someone went LIVE on {platform}!",
            colour=colour,
        )

    embed.set_footer(
        text=f"Streamer: {discord_user.display_name}",
        icon_url=discord_user.display_avatar.url if discord_user.display_avatar else discord.Embed.Empty,
    )
    return embed


def build_clip_embed(platform: str, clip_data: dict, discord_user: discord.Member) -> discord.Embed:
    colour_map = {
        "twitch": 0x9146FF,
        "youtube": 0xFF0000,
        "kick": 0x53FC18,
        "tiktok": 0xFE2C55,
    }
    colour = colour_map.get(platform.lower(), 0x5865F2)
    title = clip_data.get("title", "New Clip")
    display_name = clip_data.get("channel_title", clip_data.get("display_name", "Creator"))
    url = clip_data.get("url", "")
    thumbnail = clip_data.get("thumbnail", clip_data.get("thumbnail_url", ""))

    embed = discord.Embed(
        title=f"🎬 {display_name} posted a new clip on {platform.capitalize()}!",
        description=f"**{title}**",
        url=url,
        colour=colour,
    )
    if thumbnail:
        if "{width}" in thumbnail:
            thumbnail = thumbnail.replace("{width}", "1280").replace("{height}", "720")
        embed.set_image(url=thumbnail)
    embed.set_footer(
        text=f"Posted by: {discord_user.display_name}",
        icon_url=discord_user.display_avatar.url if discord_user.display_avatar else discord.Embed.Empty,
    )
    return embed


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------

class StreamNotify(commands.Cog):
    """
    Stream and clip notification cog.

    Admin sets a notification channel and a required role.
    Users with that role can DM the bot to register their platform channels.
    The bot polls all registered channels on a configurable interval (default
    2 minutes, range 1–60 minutes) and posts embed notifications when someone
    goes live or posts a new clip.
    """

    default_guild = {
        "notify_channel": None,       # channel id to post notifications in
        "streamer_role": None,         # role id required to register channels
        "ping_role_id": None,          # NEW: Custom role to ping instead of @everyone
        "blocklist": [],               # list of discord user ids blocked from registering
        "poll_interval": DEFAULT_POLL_INTERVAL,  # seconds between each poll cycle
        # API keys
        "twitch_client_id": None,
        "twitch_client_secret": None,
        "twitch_access_token": None,
        "youtube_api_key": None,
        # registered streamers: {str(discord_user_id): {platform: channel_handle, ...}}
        "streamers": {},
        # live status cache: {str(discord_user_id): {platform: bool}}
        "live_cache": {},
        # last checked time per user+platform for clips (ISO string)
        "last_clip_check": {},
        # already-posted clip IDs to avoid duplication: {str(discord_user_id): {platform: [ids]}}
        "posted_clips": {},
    }

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=0xFA57C0DE, force_registration=True)
        self.config.register_guild(**self.default_guild)
        self._poll_task: Optional[asyncio.Task] = None
        self._session: Optional[aiohttp.ClientSession] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def cog_load(self):
        self._session = aiohttp.ClientSession()
        self._poll_task = self.bot.loop.create_task(self._poll_loop())

    async def cog_unload(self):
        if self._poll_task:
            self._poll_task.cancel()
        if self._session:
            await self._session.close()

    # ------------------------------------------------------------------
    # Poll loop
    # ------------------------------------------------------------------

    async def _poll_loop(self):
        await self.bot.wait_until_ready()
        log.info("[StreamNotify] Poll loop started.")
        while not self.bot.is_closed():
            cycle_start = datetime.now(timezone.utc)
            try:
                await self._check_all_guilds()
            except Exception as e:
                log.exception(f"[StreamNotify] Unhandled error in poll cycle: {e}")

            # Determine the shortest poll interval across all guilds so we
            # never sleep longer than the admin-configured minimum.
            min_interval = DEFAULT_POLL_INTERVAL
            for guild in self.bot.guilds:
                try:
                    interval = await self.config.guild(guild).poll_interval()
                    if interval and interval < min_interval:
                        min_interval = interval
                except Exception:
                    pass

            elapsed = (datetime.now(timezone.utc) - cycle_start).total_seconds()
            sleep_for = max(10, min_interval - elapsed)
            log.debug(
                f"[StreamNotify] Poll cycle complete in {elapsed:.1f}s. "
                f"Next check in {sleep_for:.0f}s."
            )
            await asyncio.sleep(sleep_for)

    async def _check_all_guilds(self):
        guilds = self.bot.guilds
        log.debug(f"[StreamNotify] Checking {len(guilds)} guild(s).")
        for guild in guilds:
            try:
                await self._check_guild(guild)
            except Exception as e:
                log.error(f"[StreamNotify] Error checking guild '{guild.name}' ({guild.id}): {e}")

    async def _check_guild(self, guild: discord.Guild):
        cfg = self.config.guild(guild)
        notify_channel_id = await cfg.notify_channel()
        if not notify_channel_id:
            log.debug(f"[StreamNotify] Guild '{guild.name}': no notify channel set, skipping.")
            return
        notify_channel = guild.get_channel(notify_channel_id)
        if not notify_channel:
            log.warning(f"[StreamNotify] Guild '{guild.name}': notify channel {notify_channel_id} not found.")
            return

        # PING ROLE LOGIC
        ping_role_id = await cfg.ping_role_id()
        ping_content = f"<@&{ping_role_id}>" if ping_role_id else "@everyone"

        streamers = await cfg.streamers()
        if not streamers:
            log.debug(f"[StreamNotify] Guild '{guild.name}': no registered streamers.")
            return

        live_cache = await cfg.live_cache()
        last_clip_check = await cfg.last_clip_check()
        posted_clips = await cfg.posted_clips()

        twitch_client_id = await cfg.twitch_client_id()
        twitch_token = await cfg.twitch_access_token()
        yt_key = await cfg.youtube_api_key()

        log.debug(
            f"[StreamNotify] Guild '{guild.name}': checking {len(streamers)} streamer(s). "
            f"Twitch={'✓' if twitch_client_id else '✗'}  "
            f"YouTube={'✓' if yt_key else '✗'}"
        )

        for user_id_str, platforms in streamers.items():
            member = guild.get_member(int(user_id_str))
            if not member:
                log.debug(f"[StreamNotify] User {user_id_str} not found in guild '{guild.name}', skipping.")
                continue

            user_live = live_cache.get(user_id_str, {})
            user_last_clip = last_clip_check.get(user_id_str, {})
            user_posted = posted_clips.get(user_id_str, {})

            for platform, handle in platforms.items():
                if not handle:
                    continue

                log.debug(f"[StreamNotify] Checking {platform} '{handle}' for {member.display_name}...")

                # ── LIVE CHECK ────────────────────────────────────────────────
                stream_data = None
                if platform == "twitch":
                    if twitch_client_id and twitch_token:
                        stream_data = await check_twitch_live(
                            self._session, handle, twitch_client_id, twitch_token
                        )
                    else:
                        log.debug(f"[StreamNotify] Twitch API not configured, skipping '{handle}'")
                elif platform == "youtube":
                    if yt_key:
                        stream_data = await check_youtube_live(self._session, handle, yt_key)
                    else:
                        log.debug(f"[StreamNotify] YouTube API not configured, skipping '{handle}'")
                elif platform == "kick":
                    stream_data = await check_kick_live(self._session, handle)
                elif platform == "tiktok":
                    stream_data = await check_tiktok_live(self._session, handle)

                was_live = user_live.get(platform, False)
                is_live = stream_data is not None

                if is_live and not was_live:
                    log.info(
                        f"[StreamNotify] 🔴 {member.display_name} just went LIVE on {platform} "
                        f"('{handle}') in guild '{guild.name}'"
                    )
                    embed = build_live_embed(platform, stream_data, member)
                    try:
                        # USE DYNAMIC PING CONTENT
                        await notify_channel.send(
                            content=f"{ping_content} {member.mention} just went live!", embed=embed
                        )
                    except discord.Forbidden:
                        log.warning(
                            f"[StreamNotify] Missing permissions to post in "
                            f"#{notify_channel.name} (guild '{guild.name}')"
                        )
                    except Exception as e:
                        log.error(f"[StreamNotify] Failed to send live notification: {e}")
                elif not is_live and was_live:
                    log.info(
                        f"[StreamNotify] ⚪ {member.display_name} went offline on {platform} "
                        f"('{handle}') in guild '{guild.name}'"
                    )

                user_live[platform] = is_live

                # ── CLIPS CHECK (YouTube only for now) ─────────────────────────
                if platform == "youtube" and yt_key:
                    last_time = user_last_clip.get(platform, "2020-01-01T00:00:00Z")
                    new_clips = await check_youtube_clips(self._session, handle, yt_key, last_time)
                    known_ids = user_posted.get(platform, [])
                    for clip in new_clips:
                        vid_id = clip.get("video_id", "")
                        if vid_id and vid_id not in known_ids:
                            log.info(
                                f"[StreamNotify] 🎬 New clip from {member.display_name} on YouTube: "
                                f"'{clip.get('title')}' ({vid_id})"
                            )
                            embed = build_clip_embed(platform, clip, member)
                            try:
                                await notify_channel.send(
                                    content=f"{member.mention} posted a new clip!", embed=embed
                                )
                            except discord.Forbidden:
                                log.warning(
                                    f"[StreamNotify] Missing permissions to post clip in "
                                    f"#{notify_channel.name}"
                                )
                            except Exception as e:
                                log.error(f"[StreamNotify] Failed to send clip notification: {e}")
                            known_ids.append(vid_id)
                    user_last_clip[platform] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                    user_posted[platform] = known_ids[-50:]

            live_cache[user_id_str] = user_live
            last_clip_check[user_id_str] = user_last_clip
            posted_clips[user_id_str] = user_posted

        await cfg.live_cache.set(live_cache)
        await cfg.last_clip_check.set(last_clip_check)
        await cfg.posted_clips.set(posted_clips)

    # ------------------------------------------------------------------
    # Admin commands  [p]streamnotify
    # ------------------------------------------------------------------

    @commands.group(name="streamnotify", aliases=["sn"])
    @commands.guild_only()
    @checks.admin_or_permissions(administrator=True)
    async def streamnotify(self, ctx: commands.Context):
        """Admin configuration for StreamNotify."""

    @streamnotify.command(name="setpingrole")
    async def sn_setpingrole(self, ctx: commands.Context, role: discord.Role = None):
        """Set a specific role to ping in notifications. Mention no role to revert to @everyone."""
        if role:
            await self.config.guild(ctx.guild).ping_role_id.set(role.id)
            await ctx.send(f"✅ Notifications will now ping **{role.name}**.")
        else:
            await self.config.guild(ctx.guild).ping_role_id.set(None)
            await ctx.send("✅ Notifications will now ping **@everyone**.")

    @streamnotify.command(name="setchannel")
    async def sn_setchannel(self, ctx: commands.Context, channel: discord.TextChannel):
        """Set the channel where stream notifications will be posted."""
        await self.config.guild(ctx.guild).notify_channel.set(channel.id)
        await ctx.send(f"✅ Notification channel set to {channel.mention}.")

    @streamnotify.command(name="setrole")
    async def sn_setrole(self, ctx: commands.Context, role: discord.Role):
        """Set the role required for users to register their channels."""
        await self.config.guild(ctx.guild).streamer_role.set(role.id)
        await ctx.send(f"✅ Required streamer role set to **{role.name}**.")

    @streamnotify.command(name="settwitch")
    async def sn_settwitch(self, ctx: commands.Context, client_id: str, client_secret: str):
        """Set Twitch API credentials (Client-ID and Client-Secret)."""
        # Obtain app access token
        token_url = "https://id.twitch.tv/oauth2/token"
        params = {
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "client_credentials",
        }
        try:
            async with self._session.post(token_url, params=params) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    token = data.get("access_token")
                    await self.config.guild(ctx.guild).twitch_client_id.set(client_id)
                    await self.config.guild(ctx.guild).twitch_client_secret.set(client_secret)
                    await self.config.guild(ctx.guild).twitch_access_token.set(token)
                    await ctx.send("✅ Twitch credentials saved and access token obtained.")
                else:
                    await ctx.send("❌ Failed to obtain Twitch access token. Check your credentials.")
        except Exception as e:
            await ctx.send(f"❌ Error: {e}")
        try:
            await ctx.message.delete()  # remove credentials from chat
        except Exception:
            pass

    @streamnotify.command(name="setyoutube")
    async def sn_setyoutube(self, ctx: commands.Context, api_key: str):
        """Set YouTube Data API v3 key."""
        await self.config.guild(ctx.guild).youtube_api_key.set(api_key)
        await ctx.send("✅ YouTube API key saved.")
        try:
            await ctx.message.delete()
        except Exception:
            pass

    @streamnotify.command(name="setinterval")
    async def sn_setinterval(self, ctx: commands.Context, minutes: int):
        """
        Set how often the bot checks for live streams (in minutes).

        Minimum: 1 minute. Maximum: 60 minutes. Default: 2 minutes.

        Example: `[p]streamnotify setinterval 5`
        """
        if minutes < 1:
            return await ctx.send("❌ Interval must be at least **1 minute**.")
        if minutes > 60:
            return await ctx.send("❌ Interval cannot exceed **60 minutes**.")
        seconds = minutes * 60
        await self.config.guild(ctx.guild).poll_interval.set(seconds)
        log.info(
            f"[StreamNotify] Poll interval updated to {minutes}m ({seconds}s) "
            f"by {ctx.author} in guild '{ctx.guild.name}'"
        )
        await ctx.send(
            f"✅ Stream check interval set to **{minutes} minute{'s' if minutes != 1 else ''}**.\n"
            f"The poll loop will pick this up on its next cycle."
        )

    @streamnotify.command(name="block")
    async def sn_block(self, ctx: commands.Context, member: discord.Member):
        """Add a user to the blocklist (prevents them from registering channels)."""
        async with self.config.guild(ctx.guild).blocklist() as bl:
            if member.id not in bl:
                bl.append(member.id)
        await ctx.send(f"🚫 **{member.display_name}** has been added to the blocklist.")

    @streamnotify.command(name="unblock")
    async def sn_unblock(self, ctx: commands.Context, member: discord.Member):
        """Remove a user from the blocklist."""
        async with self.config.guild(ctx.guild).blocklist() as bl:
            if member.id in bl:
                bl.remove(member.id)
        await ctx.send(f"✅ **{member.display_name}** has been removed from the blocklist.")

    @streamnotify.command(name="blocklist")
    async def sn_blocklist(self, ctx: commands.Context):
        """Show the current blocklist."""
        bl = await self.config.guild(ctx.guild).blocklist()
        if not bl:
            return await ctx.send("The blocklist is empty.")
        lines = []
        for uid in bl:
            m = ctx.guild.get_member(uid)
            lines.append(f"• {m.display_name} (`{uid}`)" if m else f"• Unknown (`{uid}`)")
        embed = discord.Embed(title="🚫 Blocklist", description="\n".join(lines), colour=discord.Colour.red())
        await ctx.send(embed=embed)

    @streamnotify.command(name="liststreamers")
    async def sn_liststreamers(self, ctx: commands.Context):
        """List all registered streamers and their channels."""
        streamers = await self.config.guild(ctx.guild).streamers()
        if not streamers:
            return await ctx.send("No streamers registered yet.")
        lines = []
        for uid_str, platforms in streamers.items():
            m = ctx.guild.get_member(int(uid_str))
            name = m.display_name if m else f"Unknown ({uid_str})"
            parts = ", ".join(f"{p}: `{h}`" for p, h in platforms.items() if h)
            lines.append(f"• **{name}** — {parts or 'no channels'}")
        embed = discord.Embed(
            title="📋 Registered Streamers",
            description="\n".join(lines),
            colour=discord.Colour.blurple(),
        )
        await ctx.send(embed=embed)

    @streamnotify.command(name="removestreamer")
    async def sn_removestreamer(self, ctx: commands.Context, member: discord.Member):
        """Remove a streamer and all their registered channels."""
        async with self.config.guild(ctx.guild).streamers() as s:
            s.pop(str(member.id), None)
        await ctx.send(f"✅ Removed all channels for **{member.display_name}**.")

    @streamnotify.command(name="status")
    async def sn_status(self, ctx: commands.Context):
        """Show the current cog configuration."""
        cfg = self.config.guild(ctx.guild)
        channel_id = await cfg.notify_channel()
        role_id = await cfg.streamer_role()
        ping_id = await cfg.ping_role_id()
        tw = await cfg.twitch_client_id()
        yt = await cfg.youtube_api_key()
        bl = await cfg.blocklist()
        streamers = await cfg.streamers()
        interval_secs = await cfg.poll_interval()
        interval_mins = interval_secs // 60

        channel = ctx.guild.get_channel(channel_id) if channel_id else None
        role = ctx.guild.get_role(role_id) if role_id else None
        prole = ctx.guild.get_role(ping_id) if ping_id else None

        embed = discord.Embed(title="⚙️ StreamNotify Status", colour=discord.Colour.blurple())
        embed.add_field(name="Notify Channel", value=channel.mention if channel else "❌ Not set", inline=True)
        embed.add_field(name="Required Role", value=role.name if role else "❌ Not set", inline=True)
        embed.add_field(name="Ping Role", value=prole.name if prole else "@everyone", inline=True)
        embed.add_field(
            name="Poll Interval",
            value=f"⏱️ Every **{interval_mins}** minute{'s' if interval_mins != 1 else ''}",
            inline=True,
        )
        embed.add_field(name="Twitch API", value="✅ Configured" if tw else "❌ Not set", inline=True)
        embed.add_field(name="YouTube API", value="✅ Configured" if yt else "❌ Not set", inline=True)
        embed.add_field(name="Registered Streamers", value=str(len(streamers)), inline=True)
        embed.add_field(name="Blocked Users", value=str(len(bl)), inline=True)
        await ctx.send(embed=embed)

    # ------------------------------------------------------------------
    # DM-based self-registration  (users DM the bot)
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Listen for DMs to handle self-registration."""
        # Only handle DMs, ignore bots
        if message.guild is not None or message.author.bot:
            return

        content = message.content.strip()
        if not content:
            return

        # Ignore actual bot command invocations (e.g. !help, [p]command)
        # We check against known prefixes safely — DMs may not have guild prefixes
        try:
            prefixes = await self.bot.get_prefix(message)
            if isinstance(prefixes, str):
                prefixes = [prefixes]
            if any(content.startswith(p) for p in prefixes):
                return
        except Exception:
            pass  # If prefix lookup fails in DMs, just continue

        lower = content.lower()

        # Simple help trigger
        if lower in ("help", "?", "!help"):
            await self._send_dm_help(message.author)
            return

        # Parse: add <platform> <channel>
        if lower.startswith("add "):
            parts = content.split(None, 2)
            if len(parts) < 3:
                return await message.channel.send(
                    "Usage: `add <platform> <channel_handle_or_id>`\n"
                    "Platforms: `twitch`, `youtube`, `kick`, `tiktok`"
                )
            _, platform, handle = parts
            platform = platform.lower()
            if platform not in ("twitch", "youtube", "kick", "tiktok"):
                return await message.channel.send("❌ Unknown platform. Use: `twitch`, `youtube`, `kick`, `tiktok`")
            await self._register_channel(message.author, platform, handle)
            return

        # Parse: remove <platform>
        if lower.startswith("remove "):
            parts = content.split(None, 1)
            if len(parts) < 2:
                return await message.channel.send("Usage: `remove <platform>`")
            platform = parts[1].lower()
            await self._unregister_channel(message.author, platform)
            return

        # Show current registrations
        if lower in ("list", "my channels", "channels"):
            await self._list_user_channels(message.author)
            return

        # Default: show help
        await self._send_dm_help(message.author)

    async def _send_dm_help(self, user: discord.User):
        embed = discord.Embed(
            title="📡 StreamNotify — DM Commands",
            description=(
                "Register your streaming channels so the bot can post live notifications!\n\n"
                "**Commands:**\n"
                "`add twitch <username>` — Add your Twitch channel\n"
                "`add youtube <channel_id>` — Add your YouTube channel ID\n"
                "`add kick <username>` — Add your Kick channel\n"
                "`add tiktok <username>` — Add your TikTok username\n\n"
                "`remove twitch` — Remove your Twitch channel\n"
                "`remove youtube` — Remove your YouTube channel\n"
                "`remove kick` — Remove your Kick channel\n"
                "`remove tiktok` — Remove your TikTok channel\n\n"
                "`list` — Show your registered channels\n\n"
                "**Note:** You must have the required streamer role in the server "
                "and must not be on the blocklist."
            ),
            colour=discord.Colour.blurple(),
        )
        await user.send(embed=embed)

    async def _get_shared_guild_and_check(self, user: discord.User):
        """
        Find a mutual guild where:
          - the user is a member
          - the user is NOT on the blocklist
          - the user HAS the required streamer role (if one is configured)

        Iterates ALL shared guilds. Returns the first valid (guild, cfg) pair,
        or (None, None) with an explanatory DM if none qualify.
        """
        no_role_guilds = []    # guilds where user lacks the required role
        blocked_guilds = []    # guilds where user is blocked
        member_of = []         # all guilds the user is a member of

        for guild in self.bot.guilds:
            member = guild.get_member(user.id)
            if not member:
                continue

            member_of.append(guild)
            cfg = self.config.guild(guild)
            role_id = await cfg.streamer_role()
            bl = await cfg.blocklist()

            # Blocklist check
            if user.id in bl:
                blocked_guilds.append(guild.name)
                continue

            # Role check — only enforced when a role has actually been configured
            if role_id and not any(r.id == role_id for r in member.roles):
                role = guild.get_role(role_id)
                no_role_guilds.append((guild.name, role.name if role else "Unknown Role"))
                continue

            # All checks passed — this guild is valid
            log.debug(
                f"[StreamNotify] DM registration: '{user}' passed checks for guild '{guild.name}'"
            )
            return guild, cfg

        # Nothing passed — send a useful error
        if not member_of:
            await user.send(
                "❌ I couldn't find any server we share. "
                "Make sure you're in the same server as me and try again."
            )
        elif blocked_guilds:
            names = ", ".join(f"**{n}**" for n in blocked_guilds)
            await user.send(f"🚫 You are blocked from registering channels in: {names}.")
        elif no_role_guilds:
            lines = "\n".join(
                f"• **{gname}** — requires the **{rname}** role"
                for gname, rname in no_role_guilds
            )
            await user.send(
                f"❌ You don't have the required streamer role in the following server(s):\n{lines}\n\n"
                f"Ask a server admin to assign you the role, then try again."
            )
        else:
            await user.send(
                "❌ Something went wrong finding a valid server. "
                "Make sure the bot is configured in your server (`[p]streamnotify status`)."
            )

        return None, None

    async def _register_channel(self, user: discord.User, platform: str, handle: str):
        guild, cfg = await self._get_shared_guild_and_check(user)
        if not guild:
            return

        async with cfg.streamers() as streamers:
            uid = str(user.id)
            if uid not in streamers:
                streamers[uid] = {}
            streamers[uid][platform] = handle

        # FIX FOR "INSTANT NOTIFICATION" BUG
        # Initialize the cache as 'True' so it doesn't trigger a "went live" state change immediately
        async with cfg.live_cache() as cache:
            if str(user.id) not in cache:
                cache[str(user.id)] = {}
            cache[str(user.id)][platform] = True

        await user.send(
            f"✅ Your **{platform.capitalize()}** channel has been registered as `{handle}` in **{guild.name}**!\n"
            f"You'll be notified in the announcements channel when you go live."
        )
        log.info(f"User {user} registered {platform} channel '{handle}' in {guild}")

    async def _unregister_channel(self, user: discord.User, platform: str):
        guild, cfg = await self._get_shared_guild_and_check(user)
        if not guild:
            return

        async with cfg.streamers() as streamers:
            uid = str(user.id)
            if uid in streamers and platform in streamers[uid]:
                del streamers[uid][platform]
                if not streamers[uid]:
                    del streamers[uid]
                await user.send(f"✅ Your **{platform.capitalize()}** channel has been removed.")
            else:
                await user.send(f"❌ No **{platform.capitalize()}** channel registered.")

    async def _list_user_channels(self, user: discord.User):
        lines = []
        for guild in self.bot.guilds:
            member = guild.get_member(user.id)
            if not member:
                continue
            streamers = await self.config.guild(guild).streamers()
            uid = str(user.id)
            if uid in streamers and streamers[uid]:
                for p, h in streamers[uid].items():
                    lines.append(f"• **{p.capitalize()}**: `{h}` (in *{guild.name}*)")

        if not lines:
            await user.send("You don't have any channels registered yet. Send `help` to see how to add one.")
        else:
            embed = discord.Embed(
                title="📋 Your Registered Channels",
                description="\n".join(lines),
                colour=discord.Colour.blurple(),
            )
            await user.send(embed=embed)