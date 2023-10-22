from typing import Any, Coroutine, List, Literal, Optional, Tuple, Callable
import discord
from discord import app_commands
from discord.ext import commands, tasks
from datetime import datetime, timedelta
from dateutil.tz import gettz
from dateutil.parser import parse, ParserError
import emoji
from num2words import num2words
import jpholiday
import locale
import pytz


class CollectingStatus(object):
    """
    Infomations and Status of Collecting votes
    """

    def __init__(
        self,
        interaction: discord.Interaction,
        hooked_message: discord.Message,
        event_number: int,
        start_date: datetime,
        end_date: datetime,
        collect_start_date: datetime,
        collect_end_date: datetime,
        author_text: str,
        website_url: str = "",
        dates: List[datetime] = [],
        time_range: Tuple[int, int] = (21, 23),
        timezone: str = "Asia/Tokyo",
    ):
        self.interaction = interaction
        self.hooked_message = hooked_message
        self.event_number = event_number
        self.start_date = start_date
        self.end_date = end_date
        self.author_text = author_text
        self.website_url = website_url
        self.dates = dates
        self.time_range = time_range
        self.timezone = timezone
        self.collect_start_date = collect_start_date
        self.collect_end_date = collect_end_date

    @property
    def schedule_range(self):
        return self.end_date - self.start_date

    @property
    def schedule_collect_range(self):
        return self.collect_end_date - self.collect_start_date


class ScheduleCog(commands.Cog):
    DEFAULT_COLLECT_RANGE: int = 7
    FILTER_TYPE = {
        "all": lambda x: True,
        "weekday": lambda x: x.weekday() < 5,
        "weekend": lambda x: x.weekday() >= 5,
        "holydays": lambda x: x.weekday() >= 5 or jpholiday.is_holiday(x),
    }
    DEFAULT_SCHEDULE_RANGE: int = 60
    DEFAULT_TIME_ZONE: str = "Asia/Tokyo"
    dateFormat: str = "%Y/%m/%d(%a)"
    MAX_DISCORD_EMOJI_NUM: int = 20

    def __init__(self, bot: commands.Bot):
        locale.setlocale(locale.LC_TIME, "ja_JP.UTF-8")

        self.bot = bot
        self.collecting: List[CollectingStatus] = []

        self.reaction_emojis = []
        self.reaction_emojis += [
            emoji.emojize(f":{num2words(i+1)}:", language="alias") for i in range(9)
        ]
        # self.reaction_emojis += [
        #     emoji.emojize(f":regional_indicator_{chr(i+97)}:", language="alias")
        #     for i in range(26)
        # ]
        #### emoji lib does not support regional indicator emojis, so use chr() instead
        regioanl_indicator_a = "\U0001F1E6"
        self.reaction_emojis += [chr(ord(regioanl_indicator_a) + i) for i in range(26)]
        self.reaction_emojis = self.reaction_emojis[: self.MAX_DISCORD_EMOJI_NUM]

        for v in self.reaction_emojis:
            if v == "":
                self.reaction_emojis.remove(v)
        print("collected reaction_emojis successfully:", self.reaction_emojis)

        self.update_collecting.start()

    def cog_unload(self) -> Coroutine[Any, Any, None]:
        self.update_collecting.cancel()
        return super().cog_unload()

    def generate_schedule_dates(
        self,
        start_date: datetime,
        end_date: datetime,
        collect_end_date: Optional[datetime] = None,
        filter: Callable[[datetime], bool] = lambda x: True,
        timezone: str = "Asia/Tokyo",
    ) -> List[datetime]:
        """
        return list of schedule dates from start_date to end_date.
        if end_date is None, return list of schedule dates from start_date to start_date + schedule_range.
        :param start_date: start date
        :param end_date: end date
        :param timezone: timezone
        :return: list of schedule dates
        """
        start_date = start_date.astimezone(gettz(timezone)).replace(
            hour=0, minute=0, second=0
        )
        end_date = end_date.astimezone(gettz(timezone)).replace(
            hour=0, minute=0, second=0
        )
        schedule_range = (end_date - start_date).days + 1

        if collect_end_date is None:
            collect_end_date = (
                datetime.now() + timedelta(days=self.DEFAULT_COLLECT_RANGE)
            ).replace(hour=0, minute=0, second=0, tzinfo=gettz(timezone))

        dates: List[datetime] = []
        for i in range(schedule_range):
            day = start_date + timedelta(days=i)
            if filter(day) and day > collect_end_date:
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

    async def send_error_response(
        self,
        error_message: str,
        interaction: discord.Interaction,
        ephemeral: bool = True,
    ):
        """
        send error response
        :param error_message: error message
        :param interaction: interaction
        :return: None
        """
        res_embed = self.generate_embed(
            title="エラー",
            description=error_message,
            color=discord.Color.red(),
        )
        await interaction.response.send_message(embed=res_embed, ephemeral=ephemeral)

    async def change_presence(
        self,
        collect_end_date: Optional[datetime] = None
    ):
        """
        change presence
        :param collect_end_date: collect end date
        :param left_days: left days
        :return: None
        """
        if collect_end_date is not None:
            if collect_end_date.tzinfo is None or collect_end_date.tzinfo.utcoffset(collect_end_date) is None:
                collect_end_date = collect_end_date.astimezone(gettz(self.DEFAULT_TIME_ZONE))
            left_timedelta = collect_end_date - datetime.now(tz=gettz(self.DEFAULT_TIME_ZONE))
            lefttime = ""
            if left_timedelta.days > 0:
                lefttime += f"{left_timedelta.days}日"
            elif left_timedelta.seconds // 3600 > 0:
                lefttime += f"{left_timedelta.seconds // 3600}時間"
            elif left_timedelta.seconds % 3600 // 60 > 0:
                lefttime += f"{left_timedelta.seconds % 3600 // 60}分"
            else:
                lefttime += "1分以内"
            await self.bot.change_presence(
                status=discord.Status.dnd,
                activity=discord.Activity(
                    name=f"集計中 (〜{collect_end_date.strftime('%m/%d')}, 残り {lefttime})",
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
        if time_range[0] >= time_range[1]:
            raise ValueError("time_range[0] must be smaller than time_range[1]")
        if date < datetime.now(gettz(timezone)):
            raise ValueError("date must be in the future")

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
    @app_commands.describe(
        event_number="イベントの回数",
        start_date="スケジュールの開始日",
        end_date="スケジュールの終了日",
        timezone="タイムゾーン",
        filter_type="日程の種類",
        website_url="イベントのウェブサイトのURL",
        time_range_start="イベントの開始時間",
        time_range_end="イベントの終了時間",
        debug_vote="デバッグ用の投票",
    )
    # @app_commands.guild_only()
    async def schedule(
        self,
        interaction: discord.Interaction,
        event_number: int,
        start_date: str,
        end_date: str,
        timezone: str = "Asia/Tokyo",
        filter_type: Literal["all", "weekday", "weekend", "holydays"] = "holydays",
        website_url: Optional[str] = None,
        time_range_start: int = 21,
        time_range_end: int = 23,
        debug_vote: bool = False,
    ):
        """
        generate schedule
        :param interaction: interaction
        :param event_number: number of event
        :param start_date: start date
        :param end_date: end date
        :param timezone: timezone
        :param filter_type: filter type
        :param website_url: website url
        :param time_range_start: start time of event
        :param time_range_end: end time of event
        :param debug_vote: debug vote

        :return: None
        """
        # -------------------- checking  --------------------
        for c in self.collecting:
            if c.hooked_message.guild == interaction.guild:
                await self.send_error_response("現在集計中です。しばらくお待ちください。",interaction)
                return

        if interaction.guild is None:
            await self.send_error_response("サーバー以外ではこのコマンドは使用できません。",interaction)
            return

        if filter_type not in self.FILTER_TYPE:
            await self.send_error_response(f"filter_typeは以下のいずれかを指定してください。\n{', '.join(self.FILTER_TYPE.keys())}",interaction)
            return

        start_datetime: Optional[datetime] = None
        end_datetime: Optional[datetime] = None
        if start_date is not None:
            try:
                start_datetime = parse(timestr=start_date)
            except ParserError as e:
                await self.send_error_response("start_dateの形式・日付が不正です。",interaction)
                return

        if end_date is not None:
            try:
                end_datetime = parse(timestr=end_date)
            except ParserError as e:
                await self.send_error_response("end_dateの形式・日付が不正です。",interaction)
                return


        assert start_datetime is not None and end_datetime is not None

        if end_datetime <= start_datetime:
            await self.send_error_response("end_dateはstart_dateより後の日付を指定してください。",interaction)
            return
        elif start_datetime < datetime.now() or end_datetime < datetime.now():
            await self.send_error_response("start_dateとend_dateは現在時刻より後の日付を指定してください。",interaction)
            return
        elif start_datetime < datetime.now() + timedelta(
            days=self.DEFAULT_COLLECT_RANGE
        ):
            await self.send_error_response(f"投票期間とスケジュール期間が被っています。start_dateは現在時刻より{self.DEFAULT_COLLECT_RANGE}日以上後の日付を指定してください。",interaction)
            return

        if (
            time_range_start < 0
            or time_range_start > 23
            or time_range_end < 0
            or time_range_end > 23
        ):
            await self.send_error_response("time_range_startとtime_range_endは0-23の範囲で指定してください。",interaction)
            return
        elif time_range_start >= time_range_end:
            await self.send_error_response("time_range_startはtime_range_endより小さい値を指定してください。",interaction)
            return
        time_range = (time_range_start, time_range_end)

        # -------------------- initialize variables --------------------
        self.website_url = website_url
        self.collect_start_date = datetime.now()
        if debug_vote:
            self.collect_end_date = (
                self.collect_start_date + timedelta(minutes=3)
            ).replace(second=0)
        else:
            self.collect_end_date = (
                self.collect_start_date + timedelta(days=self.DEFAULT_COLLECT_RANGE)
            ).replace(hour=0, minute=0, second=0)
        author_text = f"{self.bot.ASG_NAME} 第{event_number}回"  # type: ignore

        # -------------------- generate schedule message and reactions for voting --------------------
        dates = self.generate_schedule_dates(
            start_date=start_datetime,
            end_date=end_datetime,
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
            await self.send_error_response(f"条件に一致する日程が、つけることができる絵文字数より多く存在します。{len(self.reaction_emojis)}個以下にしてください。",interaction)

        res_embed = self.generate_embed(
            title="以下のリアクションからスケジュールを選択してください。", author_text=author_text
        )
        res_embed.add_field(
            name="日時の候補",
            value="\n".join(
                [
                    f"{self.reaction_emojis[i]} `{d.strftime(self.dateFormat)}`"
                    for i, d in enumerate(dates)
                ]
            ),
        )
        res_embed.add_field(
            name="時間",
            value=f"{time_range[0]}:00 - {time_range[1]}:00",
        )
        res_embed.add_field(
            name="投票期間",
            value=f"{self.collect_start_date.strftime('%m/%d %H:%M')} 〜 {self.collect_end_date.strftime('%m/%d %H:%M')}",
        )
        await interaction.response.send_message(embed=res_embed, ephemeral=False)
        reaction_message = await interaction.original_response()
        for i in range(len(dates)):
            await reaction_message.add_reaction(self.reaction_emojis[i])

        # -------------------- start collecting votes --------------------
        status = CollectingStatus(
            interaction=interaction,
            hooked_message=reaction_message,
            event_number=event_number,
            start_date=start_datetime,
            end_date=end_datetime,
            collect_start_date=self.collect_start_date,
            collect_end_date=self.collect_end_date,
            author_text=author_text,
            website_url=website_url or "",
            dates=dates,
            time_range=time_range,
            timezone=timezone,
        )
        self.collecting.append(status)

        # -------------------- wait until voting end --------------------
        await self.change_presence(
            collect_end_date=self.collect_end_date,
        )

    @tasks.loop(minutes=1)
    async def update_collecting(self):
        for c in self.collecting:
            if c.collect_end_date < datetime.now():
                await self.addUpVotes(c)
            else:
                await self.change_presence(
                    collect_end_date=c.collect_end_date,
                )

    async def addUpVotes(self, status: CollectingStatus):
        # -------------------- voting end --------------------
        await self.change_presence()
        reaction_message = await status.hooked_message.fetch()

        # -------------------- collecting result --------------------
        reaction_result: List[int] = []
        for r in reaction_message.reactions:  # type: ignore
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
            await status.interaction.response.send_message(
                embed=ans_embed, ephemeral=False
            )
            raise e

        max_reaction_date: datetime = status.dates[max_reaction_date_index]
        ans_embed = self.generate_embed(
            title="以下の通りスケジュールが集計されました。",
            author_text=status.author_text,
        )
        ans_embed.add_field(
            name="スケジュールの集計結果",
            value="\n".join(
                [
                    f"{self.reaction_emojis[i]} `{d.strftime(self.dateFormat)}`: {'**' if i == max_reaction_date_index else ''}{reaction_result[i]}人{'**' if i == max_reaction_date_index else ''} {'   :eyes:' if i == max_reaction_date_index else ''}"
                    for i, d in enumerate(status.dates)
                ]
            ),
        )
        await reaction_message.edit(embed=ans_embed)

        # -------------------- create event --------------------
        if status.interaction.guild is None:
            raise ValueError("interaction.guild is None")

        guild = status.interaction.guild
        event = await self.create_event(
            guild=status.interaction.guild,
            name=f"{self.bot.ASG_NAME} 第{status.event_number}回",  # type: ignore
            channel=guild.voice_channels[0],
            date=max_reaction_date,
            description=f"website: {self.website_url}" if self.website_url else None,
            time_range=status.time_range,
        )
        ans_embed2 = self.generate_embed(
            title=f"次のイベントの日時は **{max_reaction_date.strftime('%m/%d')} {event.start_time.astimezone(gettz(status.timezone)).strftime('%H:%M')}{'-'+event.end_time.astimezone(gettz(status.timezone)).strftime('%H:%M') if event.end_time is not None else ''}** です。",
        )
        await status.interaction.followup.send(
            event.url, embed=ans_embed2, ephemeral=False
        )

        # -------------------- delete CollectingStatus --------------------
        self.collecting.remove(status)

    @app_commands.command(
        name="addup",
        description="直近の投票を手動で集計します。",
    )
    async def addup(
        self,
        interaction: discord.Interaction,
        message_id: Optional[int] = None,
    ):
        """
        add up schedule
        :param interaction: interaction
        :param message_id: message id
        :return: None
        """
        # -------------------- checking  --------------------
        if not isinstance(interaction.channel, discord.TextChannel):
            res_embed = self.generate_embed(
                title="エラー",
                description="このコマンドはテキストチャンネルでのみ使用できます。",
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

        if message_id is not None:
            msg = await interaction.channel.fetch_message(message_id)
            if msg is None:
                res_embed = self.generate_embed(
                    title="エラー",
                    description="指定されたメッセージが見つかりませんでした。",
                    color=discord.Color.red(),
                )
                await interaction.response.send_message(embed=res_embed, ephemeral=True)
                return

            if msg.author != self.bot.user or msg.embeds == []:
                res_embed = self.generate_embed(
                    title="エラー",
                    description="指定されたメッセージはスケジュールの集計が可能なメッセージではありません。",
                    color=discord.Color.red(),
                )
                await interaction.response.send_message(embed=res_embed, ephemeral=True)
                return

            # create CollectingStatus
            embed: discord.Embed = msg.embeds[0]
            dates: List[datetime] = []
            time_range = (21, 23)
            for field in embed.fields:
                if field.name == "日時の候補":
                    if field.value:
                        for line in field.value.split("\n"):
                            dates.append(parse(line.split("`")[1]))
                elif field.name == "時間":
                    if field.value:
                        time_range = (
                            int(field.value.split(":")[0].split("-")[0]),
                            int(field.value.split(":")[1].split("-")[1]),
                        )
            
            author_text = embed.author.name or ""
            event_number = int(author_text.split(" ")[1].split("回")[0])

            if len(dates) == 0:
                res_embed = self.generate_embed(
                    title="エラー",
                    description="指定されたメッセージには「日時の候補」の項目がありませんでした（恐らくこのメッセージは投票用のメッセージではありません）。",
                    color=discord.Color.red(),
                )
                await interaction.response.send_message(embed=res_embed, ephemeral=True)
                return

            website_url = ""
            if embed.description and "website: " in embed.description:
                website_url = embed.description.split("website: ")[1]

            status = CollectingStatus(
                interaction=interaction,
                hooked_message=msg,
                event_number=event_number,
                start_date=dates[0],
                end_date=dates[-1],
                collect_start_date=datetime.now(),
                collect_end_date=datetime.now(),
                author_text=embed.author.name or "",
                website_url=website_url,
                dates=dates,
                time_range=time_range,
            )
            await self.addUpVotes(status)
        elif len(self.collecting) > 0:
            await self.addUpVotes(self.collecting[0])
        else:
            res_embed = self.generate_embed(
                title="エラー",
                description="直近で行われている投票を見つけられませんでした。",
                color=discord.Color.red(),
            )
            await interaction.response.send_message(embed=res_embed, ephemeral=True)
            return

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ScheduleCog(bot))
