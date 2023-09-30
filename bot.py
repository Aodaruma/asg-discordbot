import os
import asyncio
import dotenv
import discord
from discord import app_commands
from discord.ext import commands

# ======================================================================

dotenv.load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
if TOKEN is None:
    raise Exception("DISCORD_TOKEN is not set")
ASG_NAME = os.getenv("ASG_NAME")
if ASG_NAME is None:
    raise Exception("ASG_NAME is not set")
BOT_OWNER_ID = os.getenv("BOT_OWNER_ID")
COMMAND_PREFIX = os.getenv("COMMAND_PREFIX") or "¥"

intents = discord.Intents.default()
intents.reactions = True

bot = commands.Bot(command_prefix=COMMAND_PREFIX, intents=intents)
bot.ASG_NAME = ASG_NAME  # type: ignore
if BOT_OWNER_ID is None:
    BOT_OWNER_ID = bot.owner_id
else:
    BOT_OWNER_ID = int(BOT_OWNER_ID)


# ======================================================================
async def load_cogs() -> None:
    for file in os.listdir(f"{os.path.realpath(os.path.dirname(__file__))}/cogs"):
        if file.endswith(".py"):
            extension = file[:-3]
            try:
                await bot.load_extension(f"cogs.{extension}")
                # bot.logger.info(f"Loaded extension '{extension}'")
                print(f"Loaded extension '{extension}'")
            except Exception as e:
                exception = f"{type(e).__name__}: {e}"
                print(f"Failed to load extension {extension}\n{exception}")
                # bot.logger.error(f"Failed to load extension {extension}\n{exception}")


# --------------------------------------------------


@bot.tree.command(
    name="stop",
    description="stop bot (only bot owner)",
)
# is bot owner
@app_commands.check(lambda interaction: interaction.user.id == bot.owner_id)
async def stop(interaction: discord.Interaction):
    """
    stop bot
    :param interaction: interaction
    :return: None
    """
    await interaction.response.send_message("Botを終了します。", ephemeral=True)
    await bot.close()


@bot.event
async def on_ready():
    if bot.user is not None:
        await load_cogs()
        await bot.tree.sync()
        print(f"Logged in as {bot.user.name} - {bot.user.id}")
        print("------")


# --------------------------------------------------

if __name__ == "__main__":
    bot.run(TOKEN)
