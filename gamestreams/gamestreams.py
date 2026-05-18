"""
MIT License

Copyright (c) 2023-present AmazingAkai

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""

from __future__ import annotations

import datetime
import logging
import re
from typing import Dict, List, Optional, Tuple

import aiohttp
import discord
from discord.ext import tasks
from redbot.cogs.streams.streams import Streams
from redbot.cogs.streams.streamtypes import TWITCH_BASE_URL
from redbot.core import Config, commands
from redbot.core.bot import Red
from redbot.core.utils.views import SimpleMenu

from .exceptions import FetchError, GameNotFoundError, StreamFetchError
from .utils import Game, Stream

TWITCH_GAMES_ENDPOINT = TWITCH_BASE_URL + "/helix/games"


log = logging.getLogger("akaicogs.gamestreams")


class GameStreams(commands.Cog):
    """Receive live announcements for new game streams."""

    __version__ = "0.6.3"
    __author__ = "Akai & AI"

    def __init__(self, bot: Red) -> None:
        self.bot = bot

        self.games: Dict[str, Optional[Game]] = {}

        self.config = Config.get_conf(self, identifier=7474034061)
        self.config.register_global(alerts=[])

        self.monitored_games: Dict[Game, List[Stream]] = {}

        self.check_streams.start()

    def format_help_for_context(self, ctx: commands.Context) -> str:
        """Thanks Sinbad!"""
        pre_processed = super().format_help_for_context(ctx)
        return f"{pre_processed}\n\nAuthor: {self.__author__}\nCog Version: {self.__version__}"

    @property
    def streams_cog(self) -> Optional[Streams]:
        return self.bot.get_cog("Streams")  # type: ignore

    def cog_unload(self):
        self.check_streams.cancel()

    async def fetch_game_headers(self):
        if not self.streams_cog:
            return None

        await self.streams_cog.maybe_renew_twitch_bearer_token()
        token = (await self.bot.get_shared_api_tokens("twitch")).get("client_id")

        if token is None:
            return None

        access_token = self.streams_cog.ttv_bearer_cache.get("access_token")

        if access_token is None:
            return None

        headers = {
            "Client-ID": token,
            "Authorization": f"Bearer {access_token}",
        }

        return headers

    async def process_game_alert(
        self, game_alert: dict, headers: dict
    ) -> Optional[Tuple[List[Stream], List[dict]]]:
        game_name = game_alert["game"]
        try:
            game = await self.fetch_game(game_name, headers=headers)
        except GameNotFoundError:
            return None
            
        alerts = game_alert["alerts"]
        all_streams = await game.fetch_streams()
        
        filtered_streams = []
        for stream in all_streams:
            for alert in alerts:
                lang_filter = alert.get("language")
                title_filter = alert.get("title_match")
                
                lang_match = (lang_filter is None) or (stream.language.lower() == lang_filter.lower())
                title_match = (title_filter is None) or (title_filter.lower() in stream.title.lower())
                
                if lang_match and title_match:
                    filtered_streams.append(stream)
                    break

        if game in self.monitored_games.keys():
            new_streams = [
                stream for stream in filtered_streams if not stream in self.monitored_games[game]
            ]
            self.monitored_games[game] = filtered_streams
            return new_streams, alerts
        else:
            self.monitored_games[game] = filtered_streams
            return [], alerts

    @tasks.loop(minutes=5)
    async def check_streams(self):
        if self.streams_cog is None:
            return

        headers = await self.fetch_game_headers()
        if headers is None:
            return

        game_alerts = await self.config.alerts()
        self.last_checked = datetime.datetime.now(datetime.timezone.utc)
        to_post_alerts: Dict[int, List[discord.Embed]] = {}

        for game_alert in game_alerts:
            new_game_alerts = await self.process_game_alert(game_alert, headers)
            self.init = True

            if new_game_alerts:
                new_streams, alerts = new_game_alerts

                for stream in new_streams:
                    embed = stream.make_embed()

                    for alert in alerts:
                        lang_filter = alert.get("language")
                        title_filter = alert.get("title_match")
                        
                        lang_match = (lang_filter is None) or (stream.language.lower() == lang_filter.lower())
                        title_match = (title_filter is None) or (title_filter.lower() in stream.title.lower())
                        
                        if lang_match and title_match:
                            to_post_alerts.setdefault(alert["channel_id"], []).append(embed)

        if to_post_alerts:
            for channel_id, embeds in to_post_alerts.items():
                channel = self.bot.get_channel(channel_id)
                if channel is not None:
                    for embeds_chunk in discord.utils.as_chunks(embeds, max_size=10):
                        try:
                            await channel.send(content="Some new streams have started: ", embeds=embeds_chunk)  # type: ignore 
                        except Exception as error:
                            log.error(error)

    @check_streams.before_loop
    async def check_streams_before_loop(self):
        await self.bot.wait_until_ready()

    @check_streams.error
    async def check_streams_error(self, error: BaseException) -> None:
        log.error("An error got raised while annoucing new streams: ", exc_info=error)

    async def fetch_game(self, game_name: str, *, headers: dict) -> Game:
        if game_name.lower() in self.games:
            game = self.games[game_name.lower()]
            if game is not None:
                return game

            raise GameNotFoundError("That game does not exist on Twitch.")

        async with aiohttp.ClientSession() as session:
            async with session.get(
                TWITCH_GAMES_ENDPOINT,
                headers=headers,
                params={"name": game_name, "first": 1},
            ) as response:
                if response.status == 401:
                    raise FetchError(
                        "Failed to fetch that game, make sure to set proper credentials. Check `[p]streamset twitchtoken` for more info."
                    )

                data = await response.json()
                games_data = data["data"]
                if not games_data:
                    self.games[game_name.lower()] = None
                    raise GameNotFoundError("That game does not exist on Twitch.")

                game = Game(games_data[0], headers=headers)
                self.games[game_name.lower()] = game
                return game

    @commands.group(name="gamestreams", aliases=["gs", "gamestream"])
    @commands.guild_only()
    async def gamestreams(self, ctx: commands.Context) -> None:
        """Command to announce game streams and search them."""

    @gamestreams.group(name="twitch")
    @commands.guild_only()
    async def gamestreams_twitch(self, ctx: commands.Context) -> None:
        """Command to announce game streams and search them on twitch."""

    @gamestreams_twitch.command(name="search", cooldown_after_parsing=True)
    @commands.guild_only()
    @commands.cooldown(rate=1, per=10, type=commands.BucketType.member)
    async def gamestreams_twitch_search(
        self, ctx: commands.GuildContext, *, game_name: str
    ) -> None:
        """Search ongoing streams for a game on Twitch."""
        if self.streams_cog is None:
            await ctx.send(
                f"Streams cog is currently not loaded. {' You can load the cog using `[p]load streams`' if await self.bot.is_owner(ctx.author) else ''}"
            )
            return

        headers = await self.fetch_game_headers()
        if headers is None:
            await ctx.send(
                "The Twitch Client ID is not set. Please read `;streamset twitchtoken`."
            )
            return

        game_name = game_name.strip("'\"")

        try:
            game = await self.fetch_game(game_name, headers=headers)
        except Exception as error:
            await ctx.send(str(error))
            return

        async with ctx.typing():
            try:
                streams = await game.fetch_streams()
            except StreamFetchError as error:
                await ctx.send(str(error))
                return

            if not streams:
                await ctx.send("No streams found for this game.")
                return

            embeds: List[discord.Embed] = []

            for i, stream in enumerate(streams):
                embed = stream.make_embed()
                embed.set_footer(
                    text=f"Page {i + 1}/{len(streams)}",
                    icon_url=ctx.guild.icon or self.bot.user.display_avatar,  # type: ignore
                )
                embeds.append(embed)

            pages = SimpleMenu(embeds, disable_after_timeout=True)  # type: ignore

            await pages.start(ctx)

    @gamestreams_twitch.command(name="alert", cooldown_after_parsing=True)
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    @commands.cooldown(rate=1, per=10, type=commands.BucketType.member)
    async def gamestreams_twitch_alert(
        self,
        ctx: commands.GuildContext,
        channel: Optional[discord.TextChannel] = None,
        *,
        game_and_filters: str,
    ) -> None:
        """Announce streams for a specific game with optional filters.
        
        Syntax:
        `[p]gamestream twitch alert [channel] <game_name> [lang/language=<val>] [title=<val>]`
        """
        if self.streams_cog is None:
            await ctx.send(
                f"Streams cog is currently not loaded. {' You can load the cog using `[p]load streams`' if await self.bot.is_owner(ctx.author) else ''}"
            )
            return

        headers = await self.fetch_game_headers()
        if headers is None:
            await ctx.send(
                "The Twitch Client ID is not set. Please read `;streamset twitchtoken`."
            )
            return

        if channel is None:
            if not isinstance(ctx.channel, discord.TextChannel):
                await ctx.send("Announcements channel must be a text channel.")
                return
            channel = ctx.channel

        # Capture both filters out of string
        lang_match = re.search(r"(?:lang|language)=(?:\"([^\"]+)\"|'([^']+)'|(\S+))", game_and_filters, re.IGNORECASE)
        title_match = re.search(r"title=(?:\"([^\"]+)\"|'([^']+)'|(.+))$", game_and_filters, re.IGNORECASE)

        language_val = None
        if lang_match:
            language_val = lang_match.group(1) or lang_match.group(2) or lang_match.group(3)

        title_val = None
        if title_match:
            title_val = title_match.group(1) or title_match.group(2) or title_match.group(3)
            if title_val:
                title_val = title_val.strip()

        # Isolate game title text
        game_name = game_and_filters
        if lang_match:
            game_name = game_name.replace(lang_match.group(0), "")
        if title_match:
            game_name = game_name.replace(title_match.group(0), "")
            
        game_name = game_name.strip().strip("'\"").strip()

        try:
            game = await self.fetch_game(game_name, headers=headers)
        except Exception as error:
            await ctx.send(str(error))
            return

        alerts = await self.config.alerts()
        removed = False

        for alert in alerts:
            if alert["game"] == game.name.lower():
                for game_alert in alert["alerts"]:
                    if (
                        game_alert["guild_id"] == ctx.guild.id
                        and game_alert["channel_id"] == channel.id
                    ):
                        # FIX: Retain previously configured values if they are missing from current call
                        final_lang = language_val if lang_match else game_alert.get("language")
                        final_title = title_val if title_match else game_alert.get("title_match")
                        
                        if final_lang or final_title:
                            game_alert["language"] = final_lang
                            game_alert["title_match"] = final_title
                            message = f"Updated filters for `{game.name}` in {channel.mention}: Language=`{final_lang}`, Title Contains=`{final_title}`."
                            await self.config.alerts.set(alerts)
                            await ctx.reply(message, mention_author=False)
                            return
                        else:
                            alert["alerts"].remove(game_alert)
                            removed = True
                            break
                else:
                    alert["alerts"].append(
                        {
                            "guild_id": ctx.guild.id,
                            "channel_id": channel.id,
                            "language": language_val,
                            "title_match": title_val,
                        }
                    )
                break
        else:
            alerts.append(
                {
                    "game": game.name.lower(),
                    "alerts": [
                        {
                            "guild_id": ctx.guild.id,
                            "channel_id": channel.id,
                            "language": language_val,
                            "title_match": title_val,
                        }
                    ],
                }
            )

        await self.config.alerts.set(alerts)

        if removed:
            message = f"Successfully removed alert for `{game.name}` from {channel.mention}."
        else:
            message = f"Successfully added alert for `{game.name}` to {channel.mention}."
            if language_val or title_val:
                message += f" (Filters -> Language: `{language_val}` | Title contains: `{title_val}`)"
        
        await ctx.reply(message, mention_author=False)

    @gamestreams_twitch.command(name="alerts", cooldown_after_parsing=True)
    @commands.guild_only()
    @commands.has_permissions(manage_guild=True)
    @commands.cooldown(rate=1, per=5, type=commands.BucketType.member)
    async def gamestreams_twitch_alerts(
        self,
        ctx: commands.GuildContext,
    ) -> None:
        """Check all the streams that get announced."""

        alerts = await self.config.alerts()
        embeds: List[discord.Embed] = []

        for i, alert in enumerate(alerts):
            game_name = alert["game"]
            game_alerts = alert["alerts"]
            description = ""
            idx = 1

            for game_alert in game_alerts:
                guild = ctx.bot.get_guild(game_alert["guild_id"])
                if guild and guild.id == ctx.guild.id:
                    channel = guild.get_channel(game_alert["channel_id"])
                    lang = game_alert.get("language") or "None (Disabled)"
                    title = game_alert.get("title_match") or "None (Disabled)"
                    
                    description += (
                        f"**{idx}.** {channel.mention if channel else 'Channel Not Found'}\n"
                        f" └ Lang Filter: `{lang}` | Title Filter: `{title}`\n"
                    )
                    idx += 1

            if description:
                embed = discord.Embed(
                    title=game_name.title(),
                    description=description,
                    colour=discord.Colour.random(),
                )
                embed.set_footer(text=f"Page {i + 1}/{len(alerts)}")
                embeds.append(embed)

        if embeds:
            pages = SimpleMenu(embeds, disable_after_timeout=True)  # type: ignore
            await pages.start(ctx)
        else:
            await ctx.send("No active alerts configured for this server.")