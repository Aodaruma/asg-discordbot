from typing import List, Optional, Tuple
import discord
from discord import app_commands
from discord.ext import commands, tasks
from datetime import datetime, timedelta
from dateutil.tz import gettz
import emoji
from num2words import num2words


class ScheduleCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.reaction_message = None
        self.schedule_range = 60
        self.scrapbox_url = None
        self.collecting = False
        self.reaction_emojis = []
        self.schedule_collect_range = 7
        self.collect_start_date: Optional[datetime] = None
        self.collect_end_date: Optional[datetime] = None

        self.reaction_emojis += [
            emoji.emojize(f":{num2words(i+1)}:", language="alias") for i in range(9)
        ]
        self.reaction_emojis += [
            emoji.emojize(f":regional_indicator_{chr(i+97)}:", language="alias")
            for i in range(26)
        ]

    def generate_schedule_dates(
        self, schedule_range: Optional[int] = None, timezone: str = "Asia/Tokyo"
    ) -> List[datetime]:
        """
        return list of schedule dates (saturday only)
        :param schedule_range: range of schedule
        :param timezone: timezone
        :return: list of schedule dates
        """
        if schedule_range is None:
            schedule_range = self.schedule_range

        now = (
            datetime.now()
            .astimezone(gettz(timezone))
            .replace(hour=0, minute=0, second=0)
        )
        dates: List[datetime] = []
        for i in range(self.schedule_range):
            day = now + timedelta(days=i)
            if day.weekday() == 5:
                dates.append(day)
        return dates

    def generate_embed(
        self,
        title: str,
        description: str = "",
        color: discord.Color = discord.Color.blue(),
        show_author: bool = True,
    ):
        """
        generate embed
        :param title: title of embed
        :param description: description of embed
        :param color: color of embed
        :return: embed
        """
        embed = discord.Embed(title=title, description=description)
        embed.color = color
        if show_author and self.bot.user:
            embed.set_author(
                name=self.bot.user.name,
                icon_url=self.bot.user.display_avatar.url,
            )
        return embed

    async def change_presence(self, is_collecting: bool = False):
        """
        change presence
        :param is_collecting: is collecting
        :return: None
        """
        if is_collecting:
            if self.collect_start_date is None:
                raise ValueError("schedule_collect_startdate is None")
            if self.collect_end_date is None:
                raise ValueError("schedule_collect_enddate is None")

            await self.bot.change_presence(
                status=discord.Status.dnd,
                activity=discord.Activity(
                    name=f"集計中 (〜{self.collect_end_date.strftime('%m/%d')}, 残り {(self.collect_end_date - datetime.now()).days } 日)",
                    type=discord.ActivityType.watching,
                ),
            )
        else:
            await self.bot.change_presence(
                status=discord.Status.online,
            )

    async def create_event(
        self,
        guild: discord.Guild,
        name: str,
        channel: discord.VoiceChannel,
        date: datetime,
        description: Optional[str] = None,
    ) -> discord.ScheduledEvent:
        """
        create scheduled event
        :param guild: guild
        :param name: name of event
        :param channel: channel of event
        :param date: date of event
        :param description: description of event
        :return: scheduled event
        """
        description = description or ""

        # the event hold on 21:00-23:00 JST
        # and start_time and end_time must be timezone aware
        # see https://discordpy.readthedocs.io/ja/latest/api.html?highlight=create_scheduled_event#discord.Guild.create_scheduled_event
        JST = gettz("Asia/Tokyo")
        start_time = datetime(date.year, date.month, date.day, 21, 0, 0, tzinfo=JST)
        end_time = datetime(date.year, date.month, date.day, 23, 0, 0, tzinfo=JST)

        return await guild.create_scheduled_event(
            name=name,
            description=description,
            channel=channel,
            start_time=start_time,
            end_time=end_time,
            privacy_level=discord.PrivacyLevel.guild_only,
            entity_type=discord.EntityType.voice,
        )

    # @commands.command()
    @app_commands.command(
        name="schedule",
        description="generate schedule",
    )
    async def schedule(
        self,
        interaction: discord.Interaction,
        schedule_range: int = 60,
        scrapbox_url: Optional[str] = None,
    ):
        """
        generate schedule
        :param interaction: interaction
        :param range: range of schedule
        :param scrapbox_url: scrapbox url
        :return: None
        """
        # -------------------- checking if bot is collecting --------------------
        if self.collecting:
            res_embed = self.generate_embed(
                title="エラー",
                description="現在集計中です。しばらくお待ちください。",
                color=discord.Color.red(),
            )
            await interaction.response.send_message(embed=res_embed, ephemeral=True)
            return

        if interaction.guild is None:
            res_embed = self.generate_embed(
                title="エラー",
                description="サーバー以外ではこのコマンドは使用できません。",
                color=discord.Color.red(),
            )
            await interaction.response.send_message(embed=res_embed, ephemeral=True)
            return

        # -------------------- initialize variables --------------------
        self.schedule_range = schedule_range
        self.scrapbox_url = scrapbox_url
        self.collecting = True
        self.collect_start_date = datetime.now()
        self.collect_end_date = self.collect_start_date + timedelta(
            days=self.schedule_collect_range
        )

        # -------------------- generate schedule message and reactions for voting --------------------
        dates = self.generate_schedule_dates()
        res_embed = self.generate_embed(title="以下のリアクションからスケジュールを選択してください。")
        res_embed.add_field(
            name="日時の候補",
            value="\n".join(
                [
                    f"{self.reaction_emojis[i]} `{d.strftime('%Y-%m-%d')}`"
                    for i, d in enumerate(dates)
                ]
            ),
        )
        await interaction.response.send_message(embed=res_embed, ephemeral=False)
        self.reaction_message = await interaction.original_response()
        for i in range(len(dates)):
            await self.reaction_message.add_reaction(self.reaction_emojis[i])

        # -------------------- wait until voting end --------------------
        await self.change_presence(is_collecting=True)
        await discord.utils.sleep_until(
            # datetime(
            #     self.collect_end_date.year,
            #     self.collect_end_date.month,
            #     self.collect_end_date.day,
            #     0,
            #     0,
            #     0,
            # )
            datetime.now()
            + timedelta(minutes=1)
        )

        # -------------------- voting end --------------------
        self.collecting = False
        await self.change_presence(is_collecting=False)
        self.reaction_message = await self.reaction_message.fetch()

        # -------------------- collecting result --------------------
        reaction_result: List[int] = []
        print(
            self.reaction_message.reactions,
            type(self.reaction_message.reactions),
        )
        for r in self.reaction_message.reactions:  # type: ignore
            reaction_result.append(r.count - 1)

        # -------------------- display result --------------------
        try:
            max_reaction_date_index: int = reaction_result.index(max(reaction_result))
        except ValueError:
            ans_embed = self.generate_embed(
                title="エラー",
                description="内部エラーによりスケジュールの集計に失敗しました。\nお手数をおかけしますが、手動で集計を行ってください。",
                color=discord.Color.red(),
            )
            await interaction.followup.send(embed=ans_embed, ephemeral=False)
            return

        max_reaction_date: datetime = dates[max_reaction_date_index]
        ans_embed = self.generate_embed(
            title="以下の通りスケジュールが集計されました。",
        )
        ans_embed.add_field(
            name="スケジュールの集計結果",
            value="\n".join(
                [
                    f"{self.reaction_emojis[i]} `{d.strftime('%Y-%m-%d')}`: {'**' if i == max_reaction_date_index else ''}{reaction_result[i]}人{'**' if i == max_reaction_date_index else ''} {':eyes:' if i == max_reaction_date_index else ''}"
                    for i, d in enumerate(dates)
                    if reaction_result[i] > 0
                ]
            ),
        )
        await self.reaction_message.edit(embed=ans_embed)

        # -------------------- create event --------------------
        event = await self.create_event(
            guild=interaction.guild,
            name="スケジュール",
            channel=interaction.guild.voice_channels[0],
            date=max_reaction_date,
            description=self.scrapbox_url,
        )
        ans_embed2 = self.generate_embed(
            title=f"次のイベントの日時は **{max_reaction_date.strftime('%m/%d')} {event.start_time.strftime('%H:%M')}{'-'+event.end_time.strftime('%H:%M') if event.end_time is not None else ''}** です。",
        )
        await interaction.followup.send(embed=ans_embed2, ephemeral=False)
        await interaction.followup.send(event.url)

    @tasks.loop(hours=12)
    async def update_presence(self):
        if self.collecting:
            await self.change_presence(is_collecting=True)

    # @tasks.loop(hours=168)  # 一週間後に集計結果を表示
    # async def display_results(self):
    #     if self.collecting:
    #         self.collecting = False
    #         # await self.reaction_message.delete()
    #         await self.change_presence(is_collecting=False)

    # @display_results.before_loop
    # async def before_display_results(self):
    #     await self.bot.wait_until_ready()

    #     while True:
    #         now = datetime.now()
    #         if now.weekday() == 5:  # 土曜日に集計結果表示タスクを開始
    #             delta = timedelta(days=7)
    #             next_saturday = datetime(now.year, now.month, now.day) + delta
    #             next_saturday = next_saturday.replace(hour=12, minute=0, second=0)
    #             break
    #         else:
    #             now += timedelta(days=1)

    #     await discord.utils.sleep_until(next_saturday)
    #     self.display_results.start()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ScheduleCog(bot))
