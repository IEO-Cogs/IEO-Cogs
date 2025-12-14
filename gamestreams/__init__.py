from redbot.core.bot import Red
from redbot.core.errors import CogLoadError

from .gamestreams import GameStreams

__red_end_user_data_statement__ = (
    "This cog does not persistently store data about users."
)


async def setup(bot: Red):
    streams_cog = bot.get_cog("Streams")

    if streams_cog is None:
        raise CogLoadError(
            "Streams cog required to run this cog was not loaded, please load the cog using `[p]load streams`."
        )
    await bot.add_cog(GameStreams(bot))
