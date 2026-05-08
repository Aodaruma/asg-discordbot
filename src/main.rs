use std::{
    collections::HashMap,
    env,
    fs::{self, File},
    io::BufWriter,
    path::{Path, PathBuf},
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc, Mutex,
    },
    time::Duration,
};

use anyhow::{anyhow, Context as AnyhowContext, Result};
use async_trait::async_trait;
use chrono::{DateTime, Datelike, Local, NaiveDate, NaiveTime, TimeZone, Utc};
use chrono_tz::Tz;
use dotenv::dotenv;
use hound::{SampleFormat, WavSpec, WavWriter};
use reqwest::multipart;
use serde::Deserialize;
use serenity::{
    all::{
        ChannelId, Colour, Command, CommandDataOptionValue, CommandInteraction, CommandOptionType,
        Context, CreateCommand, CreateCommandOption, CreateEmbed, CreateInteractionResponse,
        CreateInteractionResponseFollowup, CreateInteractionResponseMessage, EventHandler,
        GatewayIntents, GuildId, Interaction, ReactionType, Ready, ScheduledEvent,
        ScheduledEventType, Timestamp,
    },
    builder::CreateScheduledEvent,
    Client,
};
use songbird::{
    driver::{Channels, DecodeConfig, DecodeMode, SampleRate},
    events::{CoreEvent, Event, EventContext, EventHandler as VoiceEventHandler},
    SerenityInit,
};
use tokio::time::sleep;
use tokio::{io::AsyncWriteExt, task::JoinHandle};

const DEFAULT_COLLECT_DAYS: i64 = 7;
const DEFAULT_TIMEZONE: &str = "Asia/Tokyo";
const MAX_REACTION_COUNT: usize = 20;
const SAMPLE_RATE: u32 = 16_000;
const MAX_TRANSCRIPTION_BYTES: u64 = 24 * 1024 * 1024;
const TRANSCRIPTION_CHUNK_SECONDS: u32 = 10 * 60;

const REACTION_EMOJIS: [&str; MAX_REACTION_COUNT] = [
    "1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🇦", "🇧", "🇨", "🇩", "🇪", "🇫", "🇬", "🇭",
    "🇮", "🇯", "🇰",
];

#[derive(Clone)]
struct BotConfig {
    asg_name: String,
    owner_id: Option<u64>,
    openai_api_key: Option<String>,
    transcription_model: String,
    recordings_dir: PathBuf,
}

struct Handler {
    config: BotConfig,
    collecting: Arc<tokio::sync::Mutex<Vec<CollectingStatus>>>,
    scheduled_recordings: Arc<tokio::sync::Mutex<HashMap<u64, JoinHandle<()>>>>,
}

#[derive(Clone)]
struct CollectingStatus {
    guild_id: GuildId,
    channel_id: ChannelId,
    message_id: u64,
    event_number: i64,
    author_text: String,
    website_url: Option<String>,
    dates: Vec<DateTime<Tz>>,
    time_range: (u32, u32),
    timezone: Tz,
    voice_channel_id: ChannelId,
}

#[derive(Deserialize)]
struct OpenAiTextResponse {
    text: String,
}

#[tokio::main]
async fn main() -> Result<()> {
    dotenv().ok();
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "asg_discordbot=info,serenity=warn,songbird=warn".into()),
        )
        .init();

    let token = env::var("DISCORD_TOKEN").context("DISCORD_TOKEN is not set")?;
    let asg_name = env::var("ASG_NAME").context("ASG_NAME is not set")?;
    let owner_id = env::var("BOT_OWNER_ID").ok().and_then(|v| v.parse().ok());
    let openai_api_key = env::var("OPENAI_API_KEY").ok();
    let transcription_model =
        env::var("OPENAI_TRANSCRIPTION_MODEL").unwrap_or_else(|_| "whisper-1".to_string());
    let recordings_dir =
        PathBuf::from(env::var("RECORDINGS_DIR").unwrap_or_else(|_| "recordings".to_string()));
    fs::create_dir_all(&recordings_dir)?;

    let config = BotConfig {
        asg_name,
        owner_id,
        openai_api_key,
        transcription_model,
        recordings_dir,
    };

    let intents = GatewayIntents::GUILDS | GatewayIntents::GUILD_VOICE_STATES;
    let mut client = Client::builder(token, intents)
        .event_handler(Handler {
            config,
            collecting: Arc::new(tokio::sync::Mutex::new(Vec::new())),
            scheduled_recordings: Arc::new(tokio::sync::Mutex::new(HashMap::new())),
        })
        .register_songbird_from_config(songbird::Config::default().decode_mode(DecodeMode::Decode(
            DecodeConfig::new(Channels::Mono, SampleRate::Hz16000),
        )))
        .await?;

    client.start().await?;
    Ok(())
}

#[async_trait]
impl EventHandler for Handler {
    async fn ready(&self, ctx: Context, ready: Ready) {
        if let Err(err) = register_commands(&ctx).await {
            tracing::error!("failed to register commands: {err:?}");
        }
        if let Err(err) = self.restore_event_recordings(&ctx).await {
            tracing::error!("failed to restore event recordings: {err:?}");
        }
        tracing::info!("logged in as {} - {}", ready.user.name, ready.user.id);
    }

    async fn guild_scheduled_event_create(&self, ctx: Context, event: ScheduledEvent) {
        self.sync_event_recording(ctx, event).await;
    }

    async fn guild_scheduled_event_update(&self, ctx: Context, event: ScheduledEvent) {
        self.sync_event_recording(ctx, event).await;
    }

    async fn guild_scheduled_event_delete(&self, _ctx: Context, event: ScheduledEvent) {
        if let Some(handle) = self
            .scheduled_recordings
            .lock()
            .await
            .remove(&event.id.get())
        {
            handle.abort();
        }
    }

    async fn interaction_create(&self, ctx: Context, interaction: Interaction) {
        let Interaction::Command(command) = interaction else {
            return;
        };

        let result = match command.data.name.as_str() {
            "schedule" => self.schedule(&ctx, &command).await,
            "addup" => self.addup(&ctx, &command).await,
            "stop" => self.stop(&ctx, &command).await,
            "record" => self.record_now(&ctx, &command).await,
            _ => Ok(()),
        };

        if let Err(err) = result {
            tracing::error!("command failed: {err:?}");
            let _ =
                respond_error(&ctx, &command, &format!("エラーが発生しました。\n`{err}`")).await;
        }
    }
}

async fn register_commands(ctx: &Context) -> Result<()> {
    let commands = vec![
        CreateCommand::new("schedule")
            .description("投票を開始し、集計後にイベントを作成します。")
            .add_option(int_option("event_number", "イベントの回数", true))
            .add_option(str_option(
                "start_date",
                "候補期間の開始日。例: 2026-06-01",
                true,
            ))
            .add_option(str_option(
                "end_date",
                "候補期間の終了日。例: 2026-06-30",
                true,
            ))
            .add_option(channel_option(
                "voice_channel",
                "イベントと録音に使うボイスチャンネル",
                true,
            ))
            .add_option(str_option(
                "timezone",
                "タイムゾーン。既定: Asia/Tokyo",
                false,
            ))
            .add_option(str_option(
                "filter_type",
                "all / weekday / weekend / holydays。既定: holydays",
                false,
            ))
            .add_option(str_option("website_url", "イベントのWebサイトURL", false))
            .add_option(int_option("time_range_start", "開始時刻。既定: 21", false))
            .add_option(int_option("time_range_end", "終了時刻。既定: 23", false))
            .add_option(bool_option(
                "debug_vote",
                "投票期間を3分に短縮します。",
                false,
            )),
        CreateCommand::new("addup")
            .description("直近または指定メッセージの投票を手動で集計します。")
            .add_option(str_option("message_id", "集計する投票メッセージID", false)),
        CreateCommand::new("record")
            .description("指定ボイスチャンネルを今すぐ録音し、文字起こしをtxt保存します。")
            .add_option(channel_option(
                "voice_channel",
                "録音するボイスチャンネル",
                true,
            ))
            .add_option(int_option("minutes", "録音分数", true)),
        CreateCommand::new("stop").description("Botを終了します。Bot ownerのみ。"),
    ];

    Command::set_global_commands(&ctx.http, commands).await?;
    Ok(())
}

fn str_option(
    name: &'static str,
    description: &'static str,
    required: bool,
) -> CreateCommandOption {
    CreateCommandOption::new(CommandOptionType::String, name, description).required(required)
}

fn int_option(
    name: &'static str,
    description: &'static str,
    required: bool,
) -> CreateCommandOption {
    CreateCommandOption::new(CommandOptionType::Integer, name, description).required(required)
}

fn bool_option(
    name: &'static str,
    description: &'static str,
    required: bool,
) -> CreateCommandOption {
    CreateCommandOption::new(CommandOptionType::Boolean, name, description).required(required)
}

fn channel_option(
    name: &'static str,
    description: &'static str,
    required: bool,
) -> CreateCommandOption {
    CreateCommandOption::new(CommandOptionType::Channel, name, description).required(required)
}

impl Handler {
    async fn restore_event_recordings(&self, ctx: &Context) -> Result<()> {
        for guild_id in ctx.cache.guilds() {
            match guild_id.scheduled_events(&ctx.http, false).await {
                Ok(events) => {
                    for event in events {
                        self.sync_event_recording(ctx.clone(), event).await;
                    }
                }
                Err(err) => {
                    tracing::warn!(
                        "failed to fetch scheduled events for guild {}: {err:?}",
                        guild_id
                    );
                }
            }
        }
        Ok(())
    }

    async fn sync_event_recording(&self, ctx: Context, event: ScheduledEvent) {
        let event_id = event.id.get();
        if let Some(handle) = self.scheduled_recordings.lock().await.remove(&event_id) {
            handle.abort();
        }

        if let Some(handle) = build_event_recording_task(ctx, event, self.config.clone()) {
            self.scheduled_recordings
                .lock()
                .await
                .insert(event_id, handle);
        }
    }

    async fn schedule(&self, ctx: &Context, command: &CommandInteraction) -> Result<()> {
        let guild_id = command
            .guild_id
            .ok_or_else(|| anyhow!("サーバー以外では使用できません。"))?;
        let event_number = get_int(command, "event_number")?;
        let start_date = parse_date_arg(&get_string(command, "start_date")?)?;
        let end_date = parse_date_arg(&get_string(command, "end_date")?)?;
        let timezone = get_string_opt(command, "timezone")
            .unwrap_or_else(|| DEFAULT_TIMEZONE.to_string())
            .parse::<Tz>()
            .map_err(|_| anyhow!("timezoneが不正です。"))?;
        let filter_type =
            get_string_opt(command, "filter_type").unwrap_or_else(|| "holydays".to_string());
        let website_url = get_string_opt(command, "website_url");
        let time_range_start = get_int_opt(command, "time_range_start").unwrap_or(21) as u32;
        let time_range_end = get_int_opt(command, "time_range_end").unwrap_or(23) as u32;
        let debug_vote = get_bool_opt(command, "debug_vote").unwrap_or(false);
        let voice_channel_id = get_channel(command, "voice_channel")?;

        if time_range_start >= time_range_end || time_range_end > 23 {
            return respond_error(ctx, command, "開催時間の範囲が不正です。").await;
        }

        {
            let collecting = self.collecting.lock().await;
            if collecting.iter().any(|c| c.guild_id == guild_id) {
                return respond_error(ctx, command, "現在集計中です。しばらくお待ちください。")
                    .await;
            }
        }

        let now = Utc::now();
        let collect_end_at = if debug_vote {
            now + chrono::Duration::minutes(3)
        } else {
            now + chrono::Duration::days(DEFAULT_COLLECT_DAYS)
        };
        let dates =
            generate_schedule_dates(start_date, end_date, collect_end_at, &filter_type, timezone)?;

        if dates.is_empty() {
            return respond_error(
                ctx,
                command,
                "指定された条件に該当する日程が存在しませんでした。",
            )
            .await;
        }
        if dates.len() > MAX_REACTION_COUNT {
            return respond_error(
                ctx,
                command,
                "候補日が多すぎます。20個以下になるよう条件を絞ってください。",
            )
            .await;
        }

        let author_text = format!("{} 第{}回", self.config.asg_name, event_number);
        let mut embed = CreateEmbed::new()
            .title("以下のリアクションからスケジュールを選択してください。")
            .author(serenity::builder::CreateEmbedAuthor::new(
                author_text.clone(),
            ))
            .field(
                "日時の候補",
                dates
                    .iter()
                    .enumerate()
                    .map(|(i, d)| format!("{} `{}`", REACTION_EMOJIS[i], format_jp_date(*d)))
                    .collect::<Vec<_>>()
                    .join("\n"),
                false,
            )
            .field(
                "時間",
                format!("{time_range_start}:00 - {time_range_end}:00"),
                true,
            )
            .field(
                "投票期間",
                format!(
                    "{} 〜 {}",
                    now.with_timezone(&timezone).format("%m/%d %H:%M"),
                    collect_end_at
                        .with_timezone(&timezone)
                        .format("%m/%d %H:%M")
                ),
                true,
            )
            .colour(Colour::BLUE);
        if let Some(url) = &website_url {
            embed = embed.field("website", url, false);
        }

        command
            .create_response(
                &ctx.http,
                CreateInteractionResponse::Message(
                    CreateInteractionResponseMessage::new().embed(embed),
                ),
            )
            .await?;

        let message = command.get_response(&ctx.http).await?;
        for emoji in REACTION_EMOJIS.iter().take(dates.len()) {
            message
                .react(&ctx.http, ReactionType::Unicode((*emoji).to_string()))
                .await?;
        }

        self.collecting.lock().await.push(CollectingStatus {
            guild_id,
            channel_id: command.channel_id,
            message_id: message.id.get(),
            event_number,
            author_text,
            website_url,
            dates,
            time_range: (time_range_start, time_range_end),
            timezone,
            voice_channel_id,
        });

        let ctx = ctx.clone();
        let collecting = self.collecting.clone();
        let config = self.config.clone();
        let scheduled_recordings = self.scheduled_recordings.clone();
        tokio::spawn(async move {
            sleep_until(collect_end_at).await;
            if let Err(err) = add_up_oldest(
                &ctx,
                collecting,
                config,
                Some(scheduled_recordings),
                Some(guild_id),
            )
            .await
            {
                tracing::error!("auto add-up failed: {err:?}");
            }
        });

        Ok(())
    }

    async fn addup(&self, ctx: &Context, command: &CommandInteraction) -> Result<()> {
        command
            .create_response(
                &ctx.http,
                CreateInteractionResponse::Message(
                    CreateInteractionResponseMessage::new()
                        .content("集計を開始します。")
                        .ephemeral(true),
                ),
            )
            .await?;

        if get_string_opt(command, "message_id").is_some() {
            return Err(anyhow!(
                "Rust版ではmessage_id指定の復元集計は未対応です。直近の集計中投票を対象にしてください。"
            ));
        }
        add_up_oldest(
            ctx,
            self.collecting.clone(),
            self.config.clone(),
            Some(self.scheduled_recordings.clone()),
            command.guild_id,
        )
        .await
    }

    async fn stop(&self, ctx: &Context, command: &CommandInteraction) -> Result<()> {
        if let Some(owner_id) = self.config.owner_id {
            if command.user.id.get() != owner_id {
                return respond_error(ctx, command, "Bot ownerのみ実行できます。").await;
            }
        }

        command
            .create_response(
                &ctx.http,
                CreateInteractionResponse::Message(
                    CreateInteractionResponseMessage::new()
                        .content("Botを終了します。")
                        .ephemeral(true),
                ),
            )
            .await?;
        ctx.shard.shutdown_clean();
        Ok(())
    }

    async fn record_now(&self, ctx: &Context, command: &CommandInteraction) -> Result<()> {
        let guild_id = command
            .guild_id
            .ok_or_else(|| anyhow!("サーバー以外では使用できません。"))?;
        let voice_channel_id = get_channel(command, "voice_channel")?;
        let minutes = get_int(command, "minutes")?;
        if minutes <= 0 {
            return respond_error(ctx, command, "minutesは1以上を指定してください。").await;
        }

        command
            .create_response(
                &ctx.http,
                CreateInteractionResponse::Message(
                    CreateInteractionResponseMessage::new()
                        .content("録音を開始します。")
                        .ephemeral(true),
                ),
            )
            .await?;

        let config = self.config.clone();
        let ctx = ctx.clone();
        let command = command.clone();
        tokio::spawn(async move {
            match record_and_transcribe(
                &ctx,
                guild_id,
                voice_channel_id,
                Duration::from_secs((minutes as u64) * 60),
                &config,
                format!("manual-{}", Local::now().format("%Y%m%d-%H%M%S")),
            )
            .await
            {
                Ok(path) => {
                    let _ = command
                        .create_followup(
                            &ctx.http,
                            CreateInteractionResponseFollowup::new()
                                .content(format!("文字起こしを保存しました: `{}`", path.display())),
                        )
                        .await;
                }
                Err(err) => {
                    let _ = command
                        .create_followup(
                            &ctx.http,
                            CreateInteractionResponseFollowup::new()
                                .content(format!("録音または文字起こしに失敗しました: `{err}`")),
                        )
                        .await;
                }
            }
        });
        Ok(())
    }
}

async fn add_up_oldest(
    ctx: &Context,
    collecting: Arc<tokio::sync::Mutex<Vec<CollectingStatus>>>,
    config: BotConfig,
    scheduled_recordings: Option<Arc<tokio::sync::Mutex<HashMap<u64, JoinHandle<()>>>>>,
    guild_id: Option<GuildId>,
) -> Result<()> {
    let status = {
        let mut guard = collecting.lock().await;
        let index = guard
            .iter()
            .position(|c| guild_id.map_or(true, |g| c.guild_id == g))
            .ok_or_else(|| anyhow!("直近で行われている投票を見つけられませんでした。"))?;
        guard.remove(index)
    };

    let channel = status.channel_id;
    let mut message = channel
        .message(&ctx.http, status.message_id)
        .await
        .context("投票メッセージの取得に失敗しました")?;

    let mut counts = vec![0_u64; status.dates.len()];
    for reaction in &message.reactions {
        if let ReactionType::Unicode(emoji) = &reaction.reaction_type {
            if let Some(index) = REACTION_EMOJIS.iter().position(|e| e == emoji) {
                if index < counts.len() {
                    counts[index] = reaction.count.saturating_sub(1);
                }
            }
        }
    }

    let winner = counts
        .iter()
        .enumerate()
        .max_by_key(|(_, count)| *count)
        .map(|(index, _)| index)
        .ok_or_else(|| anyhow!("集計対象のリアクションがありません。"))?;
    let event_date = status.dates[winner];
    let start_time = make_datetime(
        status.timezone,
        event_date.date_naive(),
        status.time_range.0,
    )?;
    let end_time = make_datetime(
        status.timezone,
        event_date.date_naive(),
        status.time_range.1,
    )?;

    let result_lines = status
        .dates
        .iter()
        .enumerate()
        .map(|(i, d)| {
            if i == winner {
                format!(
                    "{} `{}`: **{}人**",
                    REACTION_EMOJIS[i],
                    format_jp_date(*d),
                    counts[i]
                )
            } else {
                format!(
                    "{} `{}`: {}人",
                    REACTION_EMOJIS[i],
                    format_jp_date(*d),
                    counts[i]
                )
            }
        })
        .collect::<Vec<_>>()
        .join("\n");

    let embed = CreateEmbed::new()
        .title("以下の通りスケジュールが集計されました。")
        .author(serenity::builder::CreateEmbedAuthor::new(
            status.author_text.clone(),
        ))
        .field("スケジュールの集計結果", result_lines, false)
        .colour(Colour::BLUE);
    message
        .edit(
            &ctx.http,
            serenity::builder::EditMessage::new().embed(embed),
        )
        .await?;

    let mut event_builder = CreateScheduledEvent::new(
        ScheduledEventType::Voice,
        format!("{} 第{}回", config.asg_name, status.event_number),
        Timestamp::from(start_time.with_timezone(&Utc)),
    )
    .channel_id(status.voice_channel_id)
    .end_time(Timestamp::from(end_time.with_timezone(&Utc)));
    if let Some(url) = &status.website_url {
        event_builder = event_builder.description(format!("website: {url}"));
    }

    let event = status
        .guild_id
        .create_scheduled_event(&ctx.http, event_builder)
        .await
        .context("Discordイベントの作成に失敗しました")?;

    channel
        .send_message(
            &ctx.http,
            serenity::builder::CreateMessage::new()
                .content(format!(
                    "https://discord.com/events/{}/{}",
                    status.guild_id.get(),
                    event.id.get()
                ))
                .embed(CreateEmbed::new().title(format!(
                    "次のイベントの日時は {} {}:00-{}:00 です。",
                    event_date.format("%m/%d"),
                    status.time_range.0,
                    status.time_range.1
                ))),
        )
        .await?;

    if let Some(handle) = build_event_recording_task(ctx.clone(), event.clone(), config) {
        if let Some(scheduled_recordings) = scheduled_recordings {
            if let Some(old_handle) = scheduled_recordings
                .lock()
                .await
                .insert(event.id.get(), handle)
            {
                old_handle.abort();
            }
        }
    }
    Ok(())
}

fn is_recordable_event(event: &ScheduledEvent, config: &BotConfig) -> bool {
    let expected_prefix = format!("{} 第", config.asg_name);
    matches!(event.kind, ScheduledEventType::Voice)
        && event.name.starts_with(&expected_prefix)
        && matches!(
            event.status,
            serenity::all::ScheduledEventStatus::Scheduled
                | serenity::all::ScheduledEventStatus::Active
        )
        && event.channel_id.is_some()
        && event
            .end_time
            .map(|end| end.with_timezone(&Utc) > Utc::now())
            .unwrap_or(true)
}

fn build_event_recording_task(
    ctx: Context,
    event: ScheduledEvent,
    config: BotConfig,
) -> Option<JoinHandle<()>> {
    if !is_recordable_event(&event, &config) {
        return None;
    }

    let start = event.start_time.with_timezone(&Utc);
    let end = event
        .end_time
        .map(|ts| ts.with_timezone(&Utc))
        .unwrap_or_else(|| start + chrono::Duration::hours(2));
    let now = Utc::now();
    let record_start = if start > now { start } else { now };
    if end <= record_start {
        return None;
    }
    let duration = (end - record_start)
        .to_std()
        .unwrap_or_else(|_| Duration::from_secs(1));
    let label = format!(
        "event-{}-{}",
        event.id.get(),
        record_start.format("%Y%m%d-%H%M%S")
    );
    let event_id = event.id.get();
    let guild_id = event.guild_id;
    let voice_channel_id = event.channel_id?;

    Some(tokio::spawn(async move {
        sleep_until(record_start).await;
        if let Err(err) =
            record_and_transcribe(&ctx, guild_id, voice_channel_id, duration, &config, label).await
        {
            tracing::error!("scheduled recording failed: {err:?}");
        }
        tracing::info!("event recording task finished: {event_id}");
    }))
}

async fn record_and_transcribe(
    ctx: &Context,
    guild_id: GuildId,
    voice_channel_id: ChannelId,
    duration: Duration,
    config: &BotConfig,
    label: String,
) -> Result<PathBuf> {
    let manager = songbird::get(ctx)
        .await
        .ok_or_else(|| anyhow!("Songbird voice manager is not initialized"))?
        .clone();
    let call = manager.join(guild_id, voice_channel_id).await?;

    let wav_path = config.recordings_dir.join(format!("{label}.wav"));
    let txt_path = config.recordings_dir.join(format!("{label}.txt"));
    let writer = create_wav_writer(&wav_path)?;
    let recorder = Arc::new(PcmRecorder {
        active: AtomicBool::new(true),
        writer: Mutex::new(Some(writer)),
    });

    {
        let mut call = call.lock().await;
        call.add_global_event(
            Event::Core(CoreEvent::VoiceTick),
            VoiceRecorder(recorder.clone()),
        );
    }

    sleep(duration).await;
    recorder.active.store(false, Ordering::SeqCst);
    recorder.finish()?;
    manager.remove(guild_id).await?;

    let text = transcribe_file(config, &wav_path).await?;
    tokio::fs::write(&txt_path, text).await?;
    append_event_transcript(config, &label, &txt_path).await?;
    Ok(txt_path)
}

async fn append_event_transcript(config: &BotConfig, label: &str, txt_path: &Path) -> Result<()> {
    let Some(rest) = label.strip_prefix("event-") else {
        return Ok(());
    };
    let Some((event_id, _segment)) = rest.split_once('-') else {
        return Ok(());
    };

    let text = tokio::fs::read_to_string(txt_path).await?;
    let aggregate_path = config.recordings_dir.join(format!("event-{event_id}.txt"));
    let mut file = tokio::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(aggregate_path)
        .await?;
    file.write_all(format!("\n\n===== {label} =====\n{text}\n").as_bytes())
        .await?;
    Ok(())
}

fn create_wav_writer(path: &Path) -> Result<WavWriter<BufWriter<File>>> {
    let spec = WavSpec {
        channels: 1,
        sample_rate: SAMPLE_RATE,
        bits_per_sample: 16,
        sample_format: SampleFormat::Int,
    };
    Ok(WavWriter::create(path, spec)?)
}

struct PcmRecorder {
    active: AtomicBool,
    writer: Mutex<Option<WavWriter<BufWriter<File>>>>,
}

impl PcmRecorder {
    fn write_mixed_frame(&self, voices: &HashMap<u32, songbird::events::context_data::VoiceData>) {
        if !self.active.load(Ordering::SeqCst) {
            return;
        }
        let max_len = voices
            .values()
            .filter_map(|v| v.decoded_voice.as_ref().map(Vec::len))
            .max()
            .unwrap_or(0);
        if max_len == 0 {
            return;
        }

        if let Ok(mut guard) = self.writer.lock() {
            if let Some(writer) = guard.as_mut() {
                for i in 0..max_len {
                    let mixed = voices.values().fold(0_i32, |acc, voice| {
                        acc + voice
                            .decoded_voice
                            .as_ref()
                            .and_then(|pcm| pcm.get(i))
                            .copied()
                            .unwrap_or(0) as i32
                    });
                    let sample = mixed.clamp(i16::MIN as i32, i16::MAX as i32) as i16;
                    let _ = writer.write_sample(sample);
                }
            }
        }
    }

    fn finish(&self) -> Result<()> {
        let mut guard = self
            .writer
            .lock()
            .map_err(|_| anyhow!("録音ファイルのロックに失敗しました"))?;
        if let Some(writer) = guard.take() {
            writer.finalize()?;
        }
        Ok(())
    }
}

struct VoiceRecorder(Arc<PcmRecorder>);

#[async_trait]
impl VoiceEventHandler for VoiceRecorder {
    async fn act(&self, ctx: &EventContext<'_>) -> Option<Event> {
        if let EventContext::VoiceTick(tick) = ctx {
            self.0.write_mixed_frame(&tick.speaking);
        }
        None
    }
}

async fn transcribe_file(config: &BotConfig, wav_path: &Path) -> Result<String> {
    let size = tokio::fs::metadata(wav_path).await?.len();
    if size <= MAX_TRANSCRIPTION_BYTES {
        return transcribe_single_file(config, wav_path).await;
    }

    let chunk_paths = split_wav_for_transcription(wav_path)?;
    let mut transcripts = Vec::new();
    for (index, chunk_path) in chunk_paths.iter().enumerate() {
        let text = transcribe_single_file(config, chunk_path)
            .await
            .with_context(|| {
                format!("{}番目の音声チャンクの文字起こしに失敗しました", index + 1)
            })?;
        transcripts.push(format!("[chunk {}]\n{}", index + 1, text));
        let _ = tokio::fs::remove_file(chunk_path).await;
    }
    Ok(transcripts.join("\n\n"))
}

async fn transcribe_single_file(config: &BotConfig, wav_path: &Path) -> Result<String> {
    let api_key = config
        .openai_api_key
        .as_ref()
        .ok_or_else(|| anyhow!("OPENAI_API_KEY is not set"))?;

    let bytes = tokio::fs::read(wav_path).await?;
    let file_name = wav_path
        .file_name()
        .and_then(|v| v.to_str())
        .unwrap_or("recording.wav")
        .to_string();
    let part = multipart::Part::bytes(bytes)
        .file_name(file_name)
        .mime_str("audio/wav")?;
    let form = multipart::Form::new()
        .text("model", config.transcription_model.clone())
        .text("language", "ja")
        .part("file", part);

    let response = reqwest::Client::new()
        .post("https://api.openai.com/v1/audio/transcriptions")
        .bearer_auth(api_key)
        .multipart(form)
        .send()
        .await?
        .error_for_status()?
        .json::<OpenAiTextResponse>()
        .await?;
    Ok(response.text)
}

fn split_wav_for_transcription(wav_path: &Path) -> Result<Vec<PathBuf>> {
    let mut reader = hound::WavReader::open(wav_path)?;
    let spec = reader.spec();
    let samples_per_chunk =
        spec.sample_rate as usize * spec.channels as usize * TRANSCRIPTION_CHUNK_SECONDS as usize;
    let parent = wav_path.parent().unwrap_or_else(|| Path::new("."));
    let stem = wav_path
        .file_stem()
        .and_then(|v| v.to_str())
        .unwrap_or("recording");

    let mut paths = Vec::new();
    let mut writer: Option<WavWriter<BufWriter<File>>> = None;
    let mut written = 0_usize;
    let mut chunk_index = 0_usize;

    for sample in reader.samples::<i16>() {
        if writer.is_none() || written >= samples_per_chunk {
            if let Some(w) = writer.take() {
                w.finalize()?;
            }
            chunk_index += 1;
            written = 0;
            let path = parent.join(format!("{stem}.part{chunk_index:03}.wav"));
            writer = Some(WavWriter::create(&path, spec)?);
            paths.push(path);
        }

        if let Some(w) = writer.as_mut() {
            w.write_sample(sample?)?;
            written += 1;
        }
    }

    if let Some(w) = writer.take() {
        w.finalize()?;
    }
    Ok(paths)
}

fn generate_schedule_dates(
    start: NaiveDate,
    end: NaiveDate,
    collect_end_at: DateTime<Utc>,
    filter_type: &str,
    timezone: Tz,
) -> Result<Vec<DateTime<Tz>>> {
    if end <= start {
        return Err(anyhow!(
            "end_dateはstart_dateより後の日付を指定してください。"
        ));
    }

    let now = Utc::now().with_timezone(&timezone);
    let start_dt = make_datetime(timezone, start, 0)?;
    if start_dt < now + chrono::Duration::days(DEFAULT_COLLECT_DAYS) {
        return Err(anyhow!(
            "投票期間とスケジュール期間が被っています。start_dateは現在時刻より{DEFAULT_COLLECT_DAYS}日以上後の日付を指定してください。"
        ));
    }

    let mut dates = Vec::new();
    let mut current = start;
    while current <= end {
        let dt = make_datetime(timezone, current, 0)?;
        if dt.with_timezone(&Utc) > collect_end_at && matches_filter(current, filter_type)? {
            dates.push(dt);
        }
        current = current
            .succ_opt()
            .ok_or_else(|| anyhow!("日付の生成に失敗しました。"))?;
    }
    Ok(dates)
}

fn matches_filter(date: NaiveDate, filter_type: &str) -> Result<bool> {
    let weekday = date.weekday().number_from_monday();
    Ok(match filter_type {
        "all" => true,
        "weekday" => weekday <= 5,
        "weekend" => weekday >= 6,
        "holydays" => weekday >= 6 || is_japanese_holiday(date),
        _ => {
            return Err(anyhow!(
                "filter_typeは all / weekday / weekend / holydays のいずれかです。"
            ))
        }
    })
}

fn is_japanese_holiday(date: NaiveDate) -> bool {
    let jp_date =
        match jpholiday::chrono::NaiveDate::from_ymd_opt(date.year(), date.month(), date.day()) {
            Some(date) => date,
            None => return false,
        };
    jpholiday::jpholiday::JPHoliday::new().is_holiday(&jp_date)
}

fn make_datetime(timezone: Tz, date: NaiveDate, hour: u32) -> Result<DateTime<Tz>> {
    let naive = date
        .and_time(NaiveTime::from_hms_opt(hour, 0, 0).ok_or_else(|| anyhow!("時刻が不正です。"))?);
    timezone
        .from_local_datetime(&naive)
        .single()
        .ok_or_else(|| anyhow!("指定日時をタイムゾーンに変換できません。"))
}

fn parse_date_arg(input: &str) -> Result<NaiveDate> {
    NaiveDate::parse_from_str(input, "%Y-%m-%d")
        .or_else(|_| NaiveDate::parse_from_str(input, "%Y/%m/%d"))
        .map_err(|_| anyhow!("日付は YYYY-MM-DD または YYYY/MM/DD で指定してください。"))
}

fn format_jp_date(dt: DateTime<Tz>) -> String {
    let weekdays = ["月", "火", "水", "木", "金", "土", "日"];
    format!(
        "{:04}/{:02}/{:02}({})",
        dt.year(),
        dt.month(),
        dt.day(),
        weekdays[dt.weekday().num_days_from_monday() as usize]
    )
}

async fn sleep_until(target: DateTime<Utc>) {
    if let Ok(duration) = (target - Utc::now()).to_std() {
        sleep(duration).await;
    }
}

async fn respond_error(ctx: &Context, command: &CommandInteraction, message: &str) -> Result<()> {
    let embed = CreateEmbed::new()
        .title("エラー")
        .description(message)
        .colour(Colour::RED);
    command
        .create_response(
            &ctx.http,
            CreateInteractionResponse::Message(
                CreateInteractionResponseMessage::new()
                    .embed(embed)
                    .ephemeral(true),
            ),
        )
        .await?;
    Ok(())
}

fn get_option<'a>(
    command: &'a CommandInteraction,
    name: &str,
) -> Option<&'a CommandDataOptionValue> {
    command
        .data
        .options
        .iter()
        .find(|option| option.name == name)
        .map(|option| &option.value)
}

fn get_string(command: &CommandInteraction, name: &str) -> Result<String> {
    get_string_opt(command, name).ok_or_else(|| anyhow!("{name} is required"))
}

fn get_string_opt(command: &CommandInteraction, name: &str) -> Option<String> {
    match get_option(command, name) {
        Some(CommandDataOptionValue::String(value)) => Some(value.clone()),
        _ => None,
    }
}

fn get_int(command: &CommandInteraction, name: &str) -> Result<i64> {
    get_int_opt(command, name).ok_or_else(|| anyhow!("{name} is required"))
}

fn get_int_opt(command: &CommandInteraction, name: &str) -> Option<i64> {
    match get_option(command, name) {
        Some(CommandDataOptionValue::Integer(value)) => Some(*value),
        _ => None,
    }
}

fn get_bool_opt(command: &CommandInteraction, name: &str) -> Option<bool> {
    match get_option(command, name) {
        Some(CommandDataOptionValue::Boolean(value)) => Some(*value),
        _ => None,
    }
}

fn get_channel(command: &CommandInteraction, name: &str) -> Result<ChannelId> {
    match get_option(command, name) {
        Some(CommandDataOptionValue::Channel(value)) => Ok(*value),
        _ => Err(anyhow!("{name} is required")),
    }
}
