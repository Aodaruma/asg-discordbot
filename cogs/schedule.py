from typing import List, Optional, Tuple, Callable
import discord
from discord import app_commands
from discord.ext import commands, tasks
from datetime import datetime, timedelta
from dateutil.tz import gettz
from dateutil.parser import parse, ParserError
import emoji
from num2words import num2words
import jpholiday


class ScheduleCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.reaction_message = None
        self.schedule_range: int = 60
        self.start_date = None
        self.end_date = None
        self.website_url = None
        self.collecting = False
        self.reaction_emojis = []
        self.schedule_collect_range = 7
        self.time_range: Tuple[int, int] = (21, 23)
        self.collect_start_date: Optional[datetime] = None
        self.collect_end_date: Optional[datetime] = None
        self.FILTER_TYPE = {
            "all": lambda x: True,
            "weekday": lambda x: x.weekday() < 5,
            "weekend": lambda x: x.weekday() >= 5,
            "holydays": lambda x: x.weekday() >= 5 or jpholiday.is_holiday(x)
        }

        self.reaction_emojis += [
            emoji.emojize(f":{num2words(i+1)}:", language="alias") for i in range(9)
        ]
        # self.reaction_emojis += [
        #     emoji.emojize(f":regional_indicator_{chr(i+97)}:", language="alias")
        #     for i in range(26)
        # ]
        #### emoji lib does not support regional indicator emojis, so use chr() instead
        regioanl_indicator_a = u"\U0001F1E6"
        self.reaction_emojis += [
            chr(ord(regioanl_indicator_a) + i) for i in range(26)
        ]

        for v in self.reaction_emojis:
            if v == "":
                self.reaction_emojis.remove(v)
        print("collected reaction_emojis successfully:", self.reaction_emojis)

    def generate_schedule_dates(
        self, start_date: Optional[datetime] = None, end_date: Optional[datetime]=None, schedule_range: Optional[int] = None, filter: Callable[[datetime], bool] = lambda x: True, timezone: str = "Asia/Tokyo"
    ) -> List[datetime]:
        """
        return list of schedule dates from start_date to end_date.
        if end_date is None, return list of schedule dates from start_date to start_date + schedule_range.
        :param start_date: start date
        :param end_date: end date
        :param schedule_range: range of schedule
        :param timezone: timezone
        :return: list of schedule dates
        """
        if start_date is None:
            start_date = datetime.now()
        start_date = start_date.astimezone(gettz(timezone)).replace(hour=0, minute=0, second=0)

        if end_date is None:
            if schedule_range is None:
                schedule_range = self.schedule_range
        else:
            end_date = end_date.astimezone(gettz(timezone)).replace(hour=0, minute=0, second=0)
            schedule_range = (end_date - start_date).days + 1

        dates: List[datetime] = []
        for i in range(schedule_range):
            day = start_date + timedelta(days=i)
            if filter(day):
                dates.append(day)
        return dates

    def generate_embed(
        self,
        title: str,
        description: str = "",
        color: discord.Color = discord.Color.blue(),
        author_text: Optional[str] = None,
        footer_text: Optional[str] = None,
    ):
        """
        generate embed
        :param title: title of embed
        :param description: description of embed
        :param color: color of embed
        :param author_text: author text of embed
        :param footer_text: footer text of embed
        :return: embed
        """
        embed = discord.Embed(title=title, description=description)
        embed.color = color
        if author_text and self.bot.user:
            embed.set_author(
                name=author_text,
                icon_url=self.bot.user.display_avatar.url,
            )
        if footer_text:
            embed.set_footer(text=footer_text)
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
        time_range: Tuple[int, int] = (21, 23),
        timezone: str = "Asia/Tokyo",
    ) -> discord.ScheduledEvent:
        """
        create scheduled event
        :param guild: guild
        :param name: name of event
        :param channel: channel of event
        :param date: date of event
        :param description: description of event
        :param time_range: time range of event
        :return: scheduled event
        """
        description = description or ""

        if time_range[0] < 0 or time_range[0] > 23:
            raise ValueError("time_range[0] must be in range 0-23")
        if time_range[1] < 0 or time_range[1] > 23:
            raise ValueError("time_range[1] must be in range 0-23")

        # start_time and end_time must be timezone aware
        # see https://discordpy.readthedocs.io/ja/latest/api.html?highlight=create_scheduled_event#discord.Guild.create_scheduled_event
        start_time = datetime(
            date.year, date.month, date.day, time_range[0], 0, 0, tzinfo=gettz(timezone)
        )
        end_time = datetime(
            date.year, date.month, date.day, time_range[1], 0, 0, tzinfo=gettz(timezone)
        )

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
        description="自動で投票を開始し、イベントを作成します。",
    )
    async def schedule(
        self,
        interaction: discord.Interaction,
        event_number: int,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        schedule_range: Optional[int] = None,
        timezone: str = "Asia/Tokyo",
        filter_type: str = "all",
        website_url: Optional[str] = None,
        debug_vote: bool = False,
    ):
        """
        generate schedule
        :param interaction: interaction
        :param event_number: number of event
        :param schedule_range: range of schedule
        :param website_url: website url of event
        :return: None
        """
        # -------------------- checking  --------------------
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

        if schedule_range is not None and end_date is not None:
            res_embed = self.generate_embed(
                title="エラー",
                description="schedule_rangeとend_dateは同時に指定できません。どちらか片方のみを指定できます。",
                color=discord.Color.red(),
            )
            await interaction.response.send_message(embed=res_embed, ephemeral=True)
            return
        
        if filter_type not in self.FILTER_TYPE:
            res_embed = self.generate_embed(
                title="エラー",
                description="filter_typeは以下のいずれかを指定してください。\n" + "\n".join(self.FILTER_TYPE.keys()),
                color=discord.Color.red(),
            )
            await interaction.response.send_message(embed=res_embed, ephemeral=True)
            return
        
        start_datetime: Optional[datetime] = None
        end_datetime: Optional[datetime] = None
        if start_date is not None:
            try:
                start_datetime = parse(timestr=start_date)
            except ParserError as e:
                res_embed = self.generate_embed(
                    title="エラー",
                    description="start_dateの形式が不正です。",
                    color=discord.Color.red(),
                )
                await interaction.response.send_message(embed=res_embed, ephemeral=True)
                return
            
        if end_date is not None:
            try:
                end_datetime = parse(timestr=end_date)
            except ParserError as e:
                res_embed = self.generate_embed(
                    title="エラー",
                    description="end_dateの形式が不正です。",
                    color=discord.Color.red(),
                )
                await interaction.response.send_message(embed=res_embed, ephemeral=True)
                return


        # -------------------- initialize variables --------------------
        if schedule_range is not None:
            self.schedule_range = schedule_range
        self.website_url = website_url
        self.collect_start_date = datetime.now()
        if debug_vote:
            self.collect_end_date = (self.collect_start_date + timedelta(minutes=3)).replace(second=0)
        else:
            self.collect_end_date = (self.collect_start_date + timedelta(
                days=self.schedule_collect_range
            )).replace(hour=0, minute=0, second=0)
        author_text = f"{self.bot.ASG_NAME} 第{event_number}回"  # type: ignore

        # -------------------- generate schedule message and reactions for voting --------------------
        dates = self.generate_schedule_dates(
            start_date=start_datetime,
            end_date=end_datetime,
            schedule_range=schedule_range,
            filter=self.FILTER_TYPE[filter_type],
            timezone=timezone,
        )
        if len(dates) == 0:
            res_embed = self.generate_embed(
                title="エラー",
                description="指定された条件に該当する日程が存在しませんでした。",
                color=discord.Color.red(),
            )
            await interaction.response.send_message(embed=res_embed, ephemeral=True)
            return
        elif len(dates) > len(self.reaction_emojis):
            res_embed = self.generate_embed(
                title="エラー",
                description=f"条件に一致する日程が、つけることができる絵文字数より多く存在します。{len(self.reaction_emojis)}個以下にしてください。",
                color=discord.Color.red(),
            )
            await interaction.response.send_message(embed=res_embed, ephemeral=True)
            return

        res_embed = self.generate_embed(
            title="以下のリアクションからスケジュールを選択してください。", author_text=author_text
        )
        res_embed.add_field(
            name="日時の候補",
            value="\n".join(
                [
                    f"{self.reaction_emojis[i]} `{d.strftime('%Y-%m-%d')}`"
                    for i, d in enumerate(dates)
                ]
            ),
        )
        res_embed.add_field(
            name="時間",
            value=f"{self.time_range[0]}:00 - {self.time_range[1]}:00",
        )
        res_embed.add_field(
            name="投票期間",
            value=f"{self.collect_start_date.strftime('%m/%d %H:%M')} 〜 {self.collect_end_date.strftime('%m/%d %H:%M')}",
        )
        await interaction.response.send_message(embed=res_embed, ephemeral=False)
        self.reaction_message = await interaction.original_response()
        for i in range(len(dates)):
            await self.reaction_message.add_reaction(self.reaction_emojis[i])

        # -------------------- wait until voting end --------------------
        self.collecting = True
        await self.change_presence(is_collecting=True)
        await discord.utils.sleep_until(
            self.collect_end_date
        )

        # -------------------- voting end --------------------
        self.collecting = False
        await self.change_presence(is_collecting=False)
        self.reaction_message = await self.reaction_message.fetch()

        # -------------------- collecting result --------------------
        reaction_result: List[int] = []
        # print(
        #     self.reaction_message.reactions,
        #     type(self.reaction_message.reactions),
        # )
        for r in self.reaction_message.reactions:  # type: ignore
            reaction_result.append(r.count - 1)

        # -------------------- display result --------------------
        try:
            max_reaction_date_index: int = reaction_result.index(max(reaction_result))
        except ValueError as e:
            ans_embed = self.generate_embed(
                title="エラー",
                description="内部エラーによりスケジュールの集計に失敗しました。\nお手数をおかけしますが、手動で集計を行ってください。（エラーは開発者に通知されています）",
                color=discord.Color.red(),
            )
            await interaction.followup.send(embed=ans_embed, ephemeral=False)
            raise e

        max_reaction_date: datetime = dates[max_reaction_date_index]
        ans_embed = self.generate_embed(
            title="以下の通りスケジュールが集計されました。",
            author_text=author_text,
        )
        ans_embed.add_field(
            name="スケジュールの集計結果",
            value="\n".join(
                [
                    f"{self.reaction_emojis[i]} `{d.strftime('%Y-%m-%d')}`: {'**' if i == max_reaction_date_index else ''}{reaction_result[i]}人{'**' if i == max_reaction_date_index else ''} {'   :eyes:' if i == max_reaction_date_index else ''}"
                    for i, d in enumerate(dates)
                    # if reaction_result[i] > 0
                ]
            ),
        )
        await self.reaction_message.edit(embed=ans_embed)

        # -------------------- create event --------------------
        event = await self.create_event(
            guild=interaction.guild,
            name=f"{self.bot.ASG_NAME} 第{event_number}回",  # type: ignore
            channel=interaction.guild.voice_channels[0],
            date=max_reaction_date,
            description=f"website: {self.website_url}" if self.website_url else None,
            time_range=self.time_range,
        )
        ans_embed2 = self.generate_embed(
            title=f"次のイベントの日時は **{max_reaction_date.strftime('%m/%d')} {event.start_time.astimezone(gettz(timezone)).strftime('%H:%M')}{'-'+event.end_time.astimezone(gettz(timezone)).strftime('%H:%M') if event.end_time is not None else ''}** です。",
        )
        await interaction.followup.send(event.url, embed=ans_embed2, ephemeral=False)

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
