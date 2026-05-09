"""
StreamNotify - Red Discord Bot Cog
Posts live stream and clip notifications to a defined channel.
Users can self-register their YouTube/Twitch/Kick/TikTok channels via DM.
"""

import asyncio
import logging
from typing import Optional

import aiohttp
import discord
from redbot.core import commands, Config, checks
from redbot.core.bot import Red

log = logging.getLogger("red.streamnotify")

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
                return None
            data = await resp.json()
            streams = data.get("data", [])
            if streams:
                stream = streams[0]
                # Fetch user info for avatar/display name
                user_url = f"https://api.twitch.tv/helix/users?login={username}"
                async with session.get(user_url, headers=headers) as uresp:
                    udata = await uresp.json() if uresp.status == 200 else {}
                user_info = udata.get("data", [{}])[0]
                stream["profile_image_url"] = user_info.get("profile_image_url", "")
                stream["display_name"] = user_info.get("display_name", username)
                return stream
    except Exception as e:
        log.error(f"Twitch check error for {username}: {e}")
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
                return None
            data = await resp.json()
            items = data.get("items", [])
            if items:
                item = items[0]
                video_id = item["id"]["videoId"]
                snippet = item["snippet"]
                return {
                    "video_id": video_id,
                    "title": snippet.get("title", "Live Stream"),
                    "channel_title": snippet.get("channelTitle", channel_id),
                    "thumbnail": snippet.get("thumbnails", {}).get("high", {}).get("url", ""),
                    "url": f"https://www.youtube.com/watch?v={video_id}",
                }
    except Exception as e:
        log.error(f"YouTube check error for {channel_id}: {e}")
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
    except Exception as e:
        log.error(f"YouTube clips check error for {channel_id}: {e}")
    return clips


async def check_kick_live(session: aiohttp.ClientSession, username: str):
    """Return stream data if Kick channel is live, else None."""
    url = f"https://kick.com/api/v1/channels/{username}"
    headers = {"Accept": "application/json"}
    try:
        async with session.get(url, headers=headers) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
            livestream = data.get("livestream")
            if livestream:
                return {
                    "title": livestream.get("session_title", "Live Stream"),
                    "display_name": data.get("user", {}).get("username", username),
                    "thumbnail": livestream.get("thumbnail", {}).get("url", ""),
                    "url": f"https://kick.com/{username}",
                    "viewer_count": livestream.get("viewer_count", 0),
                    "avatar": data.get("user", {}).get("profile_pic", ""),
                }
    except Exception as e:
        log.error(f"Kick check error for {username}: {e}")
    return None


# ---------------------------------------------------------------------------
# Embed builders
# ---------------------------------------------------------------------------

def build_live_embed(platform: str, stream_data: dict, discord_user: discord.Member) -> discord.Embed:
    colour_map = {
        "twitch": 0x9146FF,
        "youtube": 0xFF0000,
        "kick": 0x53FC18,
        "tiktok": 0x010101,
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
        "tiktok": 0x010101,
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
    The bot polls all registered channels every 2 minutes and posts embed
    notifications when someone goes live or posts a new clip.
    """

    default_guild = {
        "notify_channel": None,       # channel id to post notifications in
        "streamer_role": None,         # role id required to register channels
        "blocklist": [],               # list of discord user ids blocked from registering
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
        while not self.bot.is_closed():
            try:
                await self._check_all_guilds()
            except Exception as e:
                log.exception(f"Poll loop error: {e}")
            await asyncio.sleep(120)  # poll every 2 minutes

    async def _check_all_guilds(self):
        for guild in self.bot.guilds:
            try:
                await self._check_guild(guild)
            except Exception as e:
                log.error(f"Error checking guild {guild.id}: {e}")

    async def _check_guild(self, guild: discord.Guild):
        cfg = self.config.guild(guild)
        notify_channel_id = await cfg.notify_channel()
        if not notify_channel_id:
            return
        notify_channel = guild.get_channel(notify_channel_id)
        if not notify_channel:
            return

        streamers = await cfg.streamers()
        live_cache = await cfg.live_cache()
        last_clip_check = await cfg.last_clip_check()
        posted_clips = await cfg.posted_clips()

        twitch_client_id = await cfg.twitch_client_id()
        twitch_token = await cfg.twitch_access_token()
        yt_key = await cfg.youtube_api_key()

        import datetime

        for user_id_str, platforms in streamers.items():
            member = guild.get_member(int(user_id_str))
            if not member:
                continue

            user_live = live_cache.get(user_id_str, {})
            user_last_clip = last_clip_check.get(user_id_str, {})
            user_posted = posted_clips.get(user_id_str, {})

            for platform, handle in platforms.items():
                if not handle:
                    continue

                # ---- LIVE CHECK ----
                stream_data = None
                if platform == "twitch" and twitch_client_id and twitch_token:
                    stream_data = await check_twitch_live(self._session, handle, twitch_client_id, twitch_token)
                elif platform == "youtube" and yt_key:
                    stream_data = await check_youtube_live(self._session, handle, yt_key)
                elif platform == "kick":
                    stream_data = await check_kick_live(self._session, handle)

                was_live = user_live.get(platform, False)
                is_live = stream_data is not None

                if is_live and not was_live:
                    # Just went live — post notification
                    embed = build_live_embed(platform, stream_data, member)
                    try:
                        await notify_channel.send(content=f"@everyone {member.mention} just went live!", embed=embed)
                    except discord.Forbidden:
                        log.warning(f"No permission to post in {notify_channel}")

                user_live[platform] = is_live

                # ---- CLIPS CHECK (YouTube only for now) ----
                if platform == "youtube" and yt_key:
                    last_time = user_last_clip.get(platform, "2020-01-01T00:00:00Z")
                    new_clips = await check_youtube_clips(self._session, handle, yt_key, last_time)
                    known_ids = user_posted.get(platform, [])
                    new_time = last_time
                    for clip in new_clips:
                        vid_id = clip.get("video_id", "")
                        if vid_id and vid_id not in known_ids:
                            embed = build_clip_embed(platform, clip, member)
                            try:
                                await notify_channel.send(content=f"{member.mention} posted a new clip!", embed=embed)
                            except discord.Forbidden:
                                pass
                            known_ids.append(vid_id)
                            pub = clip.get("published_at", "")
                            if pub > new_time:
                                new_time = pub
                    user_last_clip[platform] = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
                    user_posted[platform] = known_ids[-50:]  # keep last 50 to avoid bloat

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
        tw = await cfg.twitch_client_id()
        yt = await cfg.youtube_api_key()
        bl = await cfg.blocklist()
        streamers = await cfg.streamers()

        channel = ctx.guild.get_channel(channel_id) if channel_id else None
        role = ctx.guild.get_role(role_id) if role_id else None

        embed = discord.Embed(title="⚙️ StreamNotify Status", colour=discord.Colour.blurple())
        embed.add_field(name="Notify Channel", value=channel.mention if channel else "❌ Not set", inline=True)
        embed.add_field(name="Required Role", value=role.name if role else "❌ Not set", inline=True)
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
        # Ignore bot commands
        if message.content.startswith(tuple(await self.bot.get_prefix(message))):
            return

        content = message.content.strip()
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
        Find a mutual guild where the user has the required streamer role and is not blocked.
        Returns (guild, config) or (None, None) if no valid guild found.
        """
        for guild in self.bot.guilds:
            member = guild.get_member(user.id)
            if not member:
                continue
            cfg = self.config.guild(guild)
            role_id = await cfg.streamer_role()
            bl = await cfg.blocklist()

            if user.id in bl:
                await user.send(f"🚫 You are blocked from registering channels in **{guild.name}**.")
                return None, None

            if role_id:
                if not any(r.id == role_id for r in member.roles):
                    role = guild.get_role(role_id)
                    role_name = role.name if role else "the required streamer role"
                    await user.send(
                        f"❌ You don't have the required role **{role_name}** in **{guild.name}** "
                        f"to register channels."
                    )
                    return None, None

            return guild, cfg

        await user.send("❌ We don't share any server where I can register channels for you.")
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
