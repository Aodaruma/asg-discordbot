# asg-discordbot

Discord bot for autonomous study group.

This branch contains the Rust bin migration using serenity, songbird, and dotenv.

## setup

```powershell
cp .env.example .env
# edit DISCORD_TOKEN, ASG_NAME, OPENAI_API_KEY
cargo run
```

## commands

- `/schedule`: starts schedule voting, creates a Discord scheduled event after voting, then joins the selected voice channel at event time and records/transcribes it.
- `/addup`: manually adds up the latest active vote.
- `/record`: immediately records a selected voice channel for the given minutes and writes a transcript txt.
- `/stop`: stops the bot. `BOT_OWNER_ID` is required for owner-only enforcement.

## transcription

Recordings and transcripts are written under `RECORDINGS_DIR` (`recordings` by default).
The transcription API uses `OPENAI_API_KEY` and `OPENAI_TRANSCRIPTION_MODEL`.
The default model is `whisper-1`; `gpt-4o-transcribe` and `gpt-4o-mini-transcribe` can also be used.

Scheduled recordings are keyed by Discord scheduled event ID. On startup, the bot scans existing
voice events whose names start with `ASG_NAME 第`, restores future recordings, and resumes active
events by recording the remaining time. Segment transcripts are also appended to
`event-<event_id>.txt`, so restarting during an event continues into the same event transcript.
