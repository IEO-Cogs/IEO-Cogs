from .streamnotify import StreamNotify


async def setup(bot):
    await bot.add_cog(StreamNotify(bot))
