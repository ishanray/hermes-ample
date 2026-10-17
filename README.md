# Ample for Hermes Agent

Put the websites and web apps Hermes builds online. Ask Hermes for "an RSVP page
for Maya's birthday" and it writes the app, deploys it with
[Ample](https://ample.computer) and gives you a public HTTPS link. Ask for a
change and it redeploys to the same link.

- **Four tools.**
  - `ample_deploy`: deploys a folder.
  - `ample_logs`: reads build and runtime logs.
  - `ample_apps`: lists what is deployed.
  - `ample_app_delete`: takes an app down.
- **Any small app.** A static site, or a Node, Python, Go, Ruby or PHP app.
  Ample works out how to build and run it.
- **Several services.** A folder with several services (say `web/` and `api/`)
  is planned into an `ample.toml` and gets one URL per public service.
- **Databases.** An app that reads `DATABASE_URL` gets a Postgres database
  automatically.
- **A skill, `ample:web-apps`.** It tells the agent which shape to pick, how to
  build for a first-time deploy, and to test the app before handing you the
  link.

## Install

```bash
hermes plugins install ample
```

That's all. On the first deploy the plugin signs up for a free Ample account
and returns a claim link. **Open it and confirm your email within two days,**
or Ample deletes the account and its sites. Set **Your email** in the plugin's
settings to have Ample email you the link too.

To use an account you already have, create an agent credential with
`ample auth agent create` and set `AMPLE_CLIENT_ID` and `AMPLE_CLIENT_SECRET`
in Hermes (Settings → Plugins → Ample, or `hermes config set`). `AMPLE_TOKEN`
also works.

## Settings

| Setting | Default | |
|---|---|---|
| `signup` | `true` | Sign up for a free account on the first deploy when no credential is set. |
| `owner_email` | empty | Ample emails you a claim link for an account the plugin signs up for. |
| `confirm_new_sites` | `true` | Ask before a folder is published for the first time. Deletes always ask. |
| `cli_path` | empty | Use this `ample` binary instead of the pinned release. |
| `api_url` | `https://api.ample.computer` | |

Settings are read when a profile first uses the plugin; restart Hermes after
changing them.

## What it does on your machine and network

Please read this before installing.

- **Network calls.** It talks to Ample's API at `api.ample.computer`:
  - signup, sign-in and claim emails;
  - every deploy, log, list and delete.
- **One download.** On first use it downloads the Ample CLI, version 0.1.33,
  from Ample's release server, `get.ample.computer/releases/0.1.33/`. The file is
  checked against the SHA-256 pinned in `hermes_ample/cli.py`, saved under
  `<hermes home>/plugin-data/ample/`, and never updated by the plugin; a new
  CLI comes only with a new plugin release. Set `cli_path` to use your own
  binary instead.
- **A subprocess.** Each tool call runs that CLI as a child process. Its
  environment is limited to:
  - a 15-minute access token and the API URL;
  - a config path inside the plugin's data directory;
  - `PATH`, `HOME`, locale, time zone and temp variables.

  It sees none of Hermes's other keys and none of your own `ample` login. The
  CLI's crash reports are turned off.
- **Uploads.** `ample_deploy` uploads the folder it is given.
  - It refuses to upload the filesystem root, your home directory, Hermes's home
    directory, or anything holding or inside `~/.ssh`, `~/.aws`, `~/.gnupg`,
    `~/.config`, `~/.kube`, `~/.docker` and `~/.azure`.
  - The first deploy of a folder asks for your approval.
  - Secrets passed in `env` go to the CLI in a temporary file readable only by
    you, which is deleted after the run, and are stored encrypted by Ample.
- **Files it writes.**
  - In the app folder: `ample.toml` (for a project of several services) and an
    `.ample/` folder of deploy records. If an earlier account was deleted,
    `.ample/` may be removed and written again.
  - In `<hermes home>/plugin-data/ample/`: the CLI and its state, plus the
    account ID, the claim link and the list of folders you have published.
- **Stored credentials.** After a signup, the new `AMPLE_CLIENT_ID` and
  `AMPLE_CLIENT_SECRET` are saved in Hermes's `.env`, the way Hermes stores
  other keys. Nothing secret is kept in plugin state.
- **No telemetry.** The plugin sends nothing beyond the calls above, and runs
  nothing in the background.

## Approvals

- **Deletes.** `ample_app_delete` always asks for your approval. Each prompt is
  for one app, so an "always" answer covers only that app.
- **First publish.** `ample_deploy` asks before a folder is published for the
  first time (turn this off with `confirm_new_sites`). The plugin remembers the
  folders you have published in its own state, per Hermes profile, and only
  those skip the question. Nothing inside a folder counts, so an `.ample/`
  folder or `ample.toml` the agent wrote itself still gets a prompt, and a
  symlink is judged by the folder it points to.
- **Hermes's rules apply.** Both prompts go through Hermes's approval gate, so
  Hermes's rules for unattended runs apply. Under cron and in one-shot `-q`
  runs, a prompt nobody can answer is denied unless you configured Hermes to
  approve.

## Accounts and limits

- **Free plan.** Accounts the plugin creates are on Ample's free plan, which has
  quotas. Ample's hosted service is in beta and free to use.
- **Claiming.** An unclaimed account is deleted two days after signup. The next
  deploy then signs up again and tells you your earlier sites are gone; their
  folders are still on disk.
- **Suspension.** If Ample suspends an account, the plugin stops and says so.
  It does not sign up again.

## Development

```bash
python -m pytest
hermes plugins validate .
```

The tests use a fake Ample API and a fake CLI.

## License

MIT
