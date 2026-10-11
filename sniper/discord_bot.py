"""Optional Discord bot to control the sniper (slash commands, optional prefix).

Only the user ids in Discord.Owner_ids can use it. The bot refuses to start
without at least one owner (the old bot let *anyone* in the server control it
when the list was empty).
"""

from __future__ import annotations

import logging
import time

import discord
from discord.ext import commands

from . import __version__
from .commands import HELP, CommandError, Commands
from .engine import Engine

log = logging.getLogger(__name__)

OK, ERR, INFO = 0x2ECC71, 0xE74C3C, 0x7B2CBF
MAX_FILE = 1_000_000


def _embed(text: str, color: int = OK, title: str | None = None) -> discord.Embed:
    e = discord.Embed(description=text[:4000], color=color, title=title)
    e.set_footer(text=f"Limited Sniper v{__version__}")
    return e


class SniperBot(commands.Bot):
    def __init__(self, engine: Engine, cmds: Commands):
        s = engine.settings
        intents = discord.Intents.default()
        if s.discord_prefix_enabled:
            intents.message_content = True
            prefix = commands.when_mentioned_or(s.discord_prefix)
        else:
            prefix = commands.when_mentioned
        super().__init__(command_prefix=prefix, intents=intents, help_command=None)
        self.engine = engine
        self.cmds = cmds
        self._register()

    def owners(self) -> list[int]:
        return self.engine.settings.discord_owner_ids

    async def setup_hook(self) -> None:
        synced = await self.tree.sync()
        log.info("Discord: synced %d slash commands", len(synced))

    async def on_ready(self) -> None:
        self.engine.stats.event(f"Discord bot online as {self.user}")
        await self.change_presence(status=discord.Status.idle, activity=discord.Game(name="Limited Sniper"))
        if self.engine.settings.discord_startup_message:
            for uid in self.owners():
                try:
                    user = await self.fetch_user(uid)
                    await user.send(embed=_embed("Sniper is online. Use `/help`.", INFO))
                except discord.HTTPException:
                    pass

    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        if isinstance(error, commands.CheckFailure):
            await ctx.send(embed=_embed("You are not allowed to use this bot.", ERR), ephemeral=True)
            return
        if isinstance(error, commands.CommandNotFound):
            return
        original = getattr(error, "original", error)
        if isinstance(original, CommandError):
            await ctx.send(embed=_embed(f"❌ {original}", ERR), ephemeral=True)
            return
        if isinstance(error, (commands.UserInputError,)):
            await ctx.send(embed=_embed(f"❌ {error}", ERR), ephemeral=True)
            return
        log.error("Discord command %s failed: %r", ctx.command, original)
        await ctx.send(embed=_embed(f"❌ Error: {original}", ERR), ephemeral=True)

    def _register(self) -> None:
        bot = self
        c = self.cmds

        def owner_only():
            async def predicate(ctx: commands.Context) -> bool:
                return ctx.author.id in bot.owners()
            return commands.check(predicate)

        async def reply(ctx: commands.Context, text: str, color: int = OK, title: str | None = None) -> None:
            await ctx.send(embed=_embed(text, color, title), ephemeral=True)

        @self.hybrid_command(name="stats", description="Sniper statistics", aliases=["s"])
        @owner_only()
        async def stats(ctx: commands.Context) -> None:
            e = discord.Embed(title="Limited Sniper", color=INFO)
            for k, v in c.stats().items():
                e.add_field(name=k, value=v or "-", inline=k not in ("Watching",))
            await ctx.send(embed=e, ephemeral=True)

        @self.hybrid_command(name="list_items", description="List watched items", aliases=["li"])
        @owner_only()
        async def list_items(ctx: commands.Context) -> None:
            rows = c.list_items()
            lines = [f"`{i}` **{p} R$**" + (f" (limit {l})" if l else "") + f" - {n}" for i, n, p, l in rows]
            text = "\n".join(lines[:60]) + (f"\n... and {len(lines) - 60} more" if len(lines) > 60 else "")
            await reply(ctx, text or "Watchlist is empty", INFO, f"{len(rows)} item(s)")

        @self.hybrid_command(name="add_item", description="Watch an item with a max price", aliases=["ai"])
        @owner_only()
        async def add_item(ctx: commands.Context, item: str, max_price: int, buy_limit: int | None = None) -> None:
            await reply(ctx, "✅ " + c.add(item, max_price, buy_limit))

        @self.hybrid_command(name="mass_add_ids", description="Add many ids (text file or list) with one price", aliases=["mai"])
        @owner_only()
        async def mass_add_ids(ctx: commands.Context, max_price: int, ids_file: discord.Attachment | None = None, ids: str | None = None) -> None:
            refs: list[str] = []
            if ids_file is not None:
                if ids_file.size > MAX_FILE:
                    raise CommandError("file is larger than 1 MB")
                refs += (await ids_file.read()).decode("utf-8", errors="replace").replace(",", "\n").split()
            if ids:
                refs += ids.replace(",", " ").split()
            if not refs:
                raise CommandError("attach a .txt file or pass ids")
            await reply(ctx, "✅ " + c.mass_add(max_price, refs))

        @self.hybrid_command(name="remove_item", description="Stop watching an item", aliases=["ri"])
        @owner_only()
        async def remove_item(ctx: commands.Context, item: str) -> None:
            await reply(ctx, "✅ " + c.remove(item))

        @self.hybrid_command(name="remove_all", description="Clear the watchlist", aliases=["ra"])
        @owner_only()
        async def remove_all(ctx: commands.Context) -> None:
            await reply(ctx, "✅ " + c.remove_all())

        @self.hybrid_command(name="change_max_price", description="Change an item's max price", aliases=["cmp"])
        @owner_only()
        async def change_max_price(ctx: commands.Context, item: str, new_max_price: int) -> None:
            await reply(ctx, "✅ " + c.price(item, new_max_price))

        @self.hybrid_command(name="mass_change_max_prices", description="Change every item's max price", aliases=["mcmp"])
        @owner_only()
        async def mass_change_max_prices(ctx: commands.Context, new_max_price: int) -> None:
            await reply(ctx, "✅ " + c.price_all(new_max_price))

        @self.hybrid_command(name="set_buy_limit", description="Set an item's buy limit (0 = none)", aliases=["sbl"])
        @owner_only()
        async def set_buy_limit(ctx: commands.Context, item: str, limit: int) -> None:
            await reply(ctx, "✅ " + c.limit(item, limit))

        @self.hybrid_command(name="focus", description="Only check one item", aliases=["f"])
        @owner_only()
        async def focus(ctx: commands.Context, item: str, max_price: int) -> None:
            await reply(ctx, "🎯 " + c.focus(item, max_price))

        @self.hybrid_command(name="unfocus", description="Go back to the whole watchlist", aliases=["uf"])
        @owner_only()
        async def unfocus(ctx: commands.Context) -> None:
            await reply(ctx, "✅ " + c.unfocus())

        @self.hybrid_command(name="pause", description="Stop checking and buying")
        @owner_only()
        async def pause(ctx: commands.Context) -> None:
            await reply(ctx, "⏸️ " + c.pause(), INFO)

        @self.hybrid_command(name="resume", description="Resume checking and buying")
        @owner_only()
        async def resume(ctx: commands.Context) -> None:
            await reply(ctx, "▶️ " + c.resume())

        @self.hybrid_command(name="dry_run", description="Simulate purchases without buying")
        @owner_only()
        async def dry_run(ctx: commands.Context, enabled: bool) -> None:
            await reply(ctx, c.dry_run(enabled), INFO)

        @self.hybrid_command(name="history", description="Recent purchases and attempts", aliases=["h"])
        @owner_only()
        async def history(ctx: commands.Context, count: int = 10) -> None:
            await reply(ctx, "```\n" + "\n".join(c.history(count))[:3900] + "\n```", INFO, "History")

        @self.hybrid_command(name="lanes", description="Show proxy / alt account lanes")
        @owner_only()
        async def lanes(ctx: commands.Context) -> None:
            lines = c.lanes()
            await reply(ctx, "```\n" + "\n".join(lines[:40])[:3900] + "\n```", INFO, f"{len(lines)} lane(s)")

        @self.hybrid_command(name="add_user", description="Allow a user to control the bot", aliases=["au"])
        @owner_only()
        async def add_user(ctx: commands.Context, user: discord.User) -> None:
            ids = list(bot.owners())
            if user.id in ids:
                raise CommandError(f"{user} is already an owner")
            bot.engine.store.update_setting("Discord", "Owner_ids", ids + [user.id])
            await reply(ctx, f"✅ {user.mention} can now use the bot")

        @self.hybrid_command(name="remove_user", description="Remove a user from the owners", aliases=["ru"])
        @owner_only()
        async def remove_user(ctx: commands.Context, user: discord.User) -> None:
            ids = list(bot.owners())
            if user.id not in ids:
                raise CommandError(f"{user} is not an owner")
            if len(ids) == 1:
                raise CommandError("can not remove the last owner")
            ids.remove(user.id)
            bot.engine.store.update_setting("Discord", "Owner_ids", ids)
            await reply(ctx, f"✅ {user.mention} removed")

        @self.hybrid_command(name="owner_ids", description="Show the users allowed to use the bot", aliases=["oi"])
        @owner_only()
        async def owner_ids(ctx: commands.Context) -> None:
            await reply(ctx, "\n".join(f"<@{i}> (`{i}`)" for i in bot.owners()), INFO, "Owners")

        @self.hybrid_command(name="ping", description="Bot latency")
        @owner_only()
        async def ping(ctx: commands.Context) -> None:
            t0 = time.perf_counter()
            await reply(ctx, f"🏓 Gateway {bot.latency * 1000:.0f} ms", INFO)
            log.debug("ping reply took %.0f ms", (time.perf_counter() - t0) * 1000)

        @self.hybrid_command(name="restart", description="Restart the sniper", aliases=["r"])
        @owner_only()
        async def restart(ctx: commands.Context) -> None:
            await reply(ctx, "🔄 Restarting...", INFO)
            c.restart()

        @self.hybrid_command(name="help", description="All commands")
        @owner_only()
        async def help_(ctx: commands.Context) -> None:
            names = {
                "list": "list_items", "add": "add_item", "mass_add": "mass_add_ids", "remove": "remove_item",
                "price": "change_max_price", "price_all": "mass_change_max_prices", "limit": "set_buy_limit",
            }
            lines = [f"`/{names.get(h.name, h.name)}` - {h.text}" for h in HELP]
            lines += ["`/add_user` `/remove_user` `/owner_ids` - manage who can use the bot", "`/ping` - latency"]
            await reply(ctx, "\n".join(lines), INFO, "Commands")


async def run_bot(engine: Engine, cmds: Commands, token: str) -> None:
    if not engine.settings.discord_owner_ids:
        engine.stats.event("Discord bot NOT started: set at least one id in Discord.Owner_ids", logging.ERROR)
        return
    bot = SniperBot(engine, cmds)
    try:
        await bot.start(token)
    except discord.LoginFailure:
        engine.stats.event("Discord bot token is invalid", logging.ERROR)
    except discord.PrivilegedIntentsRequired:
        engine.stats.event("Enable the MESSAGE CONTENT intent for your bot (needed for prefix commands)", logging.ERROR)
    finally:
        if not bot.is_closed():
            await bot.close()
