# asg-discordbot

Discord bot for autonomous study group

# installaion

```
$ git clone <repo's git url> && cd asg-discordbot
$ pipenv sync # requires pipenv (and pyenv)
$ cp .env.example .env
$ vi .env # edit .env, overwrite DISCORD_TOKEN= with your bot's token
$ pipenv run python bot.py
```

# development

I usually use vscode for developing discord bot in Python. so I also created .vscode directory for vscode's settings in the repository.
Activate extensions in .vscode/extensions.json if you want to use them.
