# outreach-agent

An AI agent that does cold outreach for a small business, with you approving every email.

It finds businesses on the public web that fit who you sell to, reads their websites, and finds a published address. It then drafts a short, personal three-email sequence and posts it to a private Discord channel. **Nothing is sent until you press Approve.** After that it:

- sends from your own mailbox, in the recipient's working hours;
- follows up twice, and stops the moment they reply;
- brings you their reply with a suggested answer.

It runs on your Mac in the background. Its brain is Claude, through either your Claude subscription (Claude Code) or Anthropic API credits, and you can switch between them from Discord.

```
every 10 minutes
  read new replies ──► reply?  stop the sequence, suggest an answer, ping you
                       bounce? stop, never email that address again
                       "unsubscribe"? stop, never email that company again
  keep ~12 drafts waiting for you:
     discover (web search) → research their site → find a published address → draft 3 emails → Discord card

all the time
  send approved emails: Mon–Fri, 9am–4pm their time, 2–7 min apart, under today's cap
```

## What makes it different

- **You approve everything.** Each draft arrives as a Discord card showing who it goes to, why them, what was noticed on their site, and all three emails. You can approve it, edit it, ask for a rewrite ("shorter", "lead with the pricing page"), reject it, or never contact that company.
- **Emails read like a person wrote them.** They're short and specific, with no brochure language. A fixed pre-send check rejects drafts with stock phrases, dashes, links to other sites, placeholders or the wrong length, and a draft that fails twice is dropped.
- **Addresses are never guessed.** They come only from the prospect's own website (or [Hunter](https://hunter.io), if you add a key). Free-mail addresses and inboxes like support@ and jobs@ are skipped, so bounces stay low and your domain keeps its reputation.
- **Safe by default.** The daily cap ramps up slowly and sending pauses itself if bounces climb. Every email has an opt-out line, and an "unsubscribe" reply is honoured permanently. Your inbox is opened read-only.
- **One business per folder.** Your business lives in a *home folder*: settings, secrets, a profile describing what you sell and to whom, and the database. One copy of this code can run several businesses side by side.

## Requirements

- macOS for the double-click setup and background service. The program itself is plain Python and runs anywhere; on Linux, run `run.sh` under systemd or tmux.
- Python 3.11 or newer.
- One of:
  - a Claude subscription with [Claude Code](https://claude.com/claude-code) installed (`claude` on your PATH), or
  - an Anthropic API key.
- A mailbox with SMTP and IMAP access. Gmail or Google Workspace with an app password works out of the box.
- A Discord account. You'll create a private server and a bot; both are free.

## Quick start

```bash
git clone https://github.com/hammad1201/outreach-agent.git
cd outreach-agent
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# create a home folder for your business, with a filled-in example profile to edit
.venv/bin/python -m outreach_agent init ~/Outreach/MyBusiness
```

Then:

1. **Describe your business** in `~/Outreach/MyBusiness/profile/` (see [The profile](#the-profile)).
2. **Fill in** `config.toml` and `.env` in that folder (see [Setup](#setup)).
3. **Double-click `Setup.command`** in that folder. It logs Claude in if needed, checks every connection, and starts outreach in the background, now and at every login.

From then on, everything happens in Discord. **Double-click `Stop.command`** to turn it off.

## The profile

Everything that makes the emails about *your* business is in four files in `profile/`. The code has nothing hardcoded about any business. The example profile is for a fictional web studio, Kite Labs. Replace it with yours.

| File | What it is |
|---|---|
| `facts.md` | What you do, how you work, proof you can mention, who you don't work with. **The agent claims nothing that isn't here**, so keep it true. Plain prose or bullets. |
| `segments.md` | The kinds of business to look for. Each is one `## key` section describing who they are, the signs they need you, where to find them, who to skip, and what you offer them. Optional lines: `Label:` (display name), `Aliases:` (other words for `find`), and `Niches:`, a `;`-separated list from which each search picks one at random, so runs don't repeat themselves. |
| `examples.md` | One or two example emails per segment (or a `## default` section). They set the voice and length; the agent writes each email fresh. |
| `signature.html` | Optional HTML signature under every email. A plain-text signature is always included too. |

The files are re-read as they're used, so edits take effect without a restart. `<!-- comments -->` in them are notes for you and never reach the model.

**Tip:** the profile is the whole game. Specific signs of need ("still takes bookings by Instagram DM", "setup guide still uses a USB cable") find far better prospects than broad descriptions ("small businesses that need software").

## Setup

About 20 minutes, once per business.

**1. Claude.** With a Claude subscription, install Claude Code, run `claude` once, log in, and `/exit`. With API credits, set `provider = "api"` in `config.toml` and add `ANTHROPIC_API_KEY` to `.env`. Optionally, set `api_budget_usd` as a monthly spending limit.

**2. The mailbox (Gmail shown; any SMTP/IMAP provider works).** For iCloud Mail, including custom domains, create an app-specific password at <https://account.apple.com>, use your iCloud address as `SMTP_USER`, and set the hosts in `[mail]` to `smtp.mail.me.com` and `imap.mail.me.com`.
1. Turn on 2-Step Verification: <https://myaccount.google.com/security>.
2. Create an app password at <https://myaccount.google.com/apppasswords>. Put your Gmail address in `.env` as `SMTP_USER` and the app password as `SMTP_PASSWORD`.
3. If you send from an address on your own domain that forwards to Gmail (e.g. `hello@yourbusiness.com`), add it in Gmail under **Settings → Accounts and Import → Send mail as**, and set it as `email` under `[sender]` in `config.toml`.

Sent emails are filed in your mailbox's Sent folder, so you see the real conversations in your mail app (Gmail does this by itself; for other providers the agent saves the copy, see `save_to_sent`).

**3. The Discord bot.**
1. In Discord, create a private server for yourself (＋ → Create My Own) with a channel such as `#outreach`.
2. At <https://discord.com/developers/applications>, click **New Application**, then on the **Bot** tab click **Reset Token**. Put the token in `.env` as `DISCORD_BOT_TOKEN`.
3. On the same **Bot** tab, turn on **Message Content Intent** so you can type commands like `status` (otherwise only the `/` commands work).
4. Under **OAuth2 → URL Generator**, tick `bot` and `applications.commands`, and the permissions **Send Messages**, **Embed Links**, **Read Message History**, **Pin Messages** (to pin the command list) and **Manage Channels** (to set the topic). Open the generated link and add the bot to your server.
5. In Discord, turn on **Settings → Advanced → Developer Mode**. Right-click the channel → **Copy Channel ID** (`DISCORD_CHANNEL_ID`). Right-click your name → **Copy User ID** (`DISCORD_OWNER_ID`). Only that user can press the buttons.

**4. Postal address (optional).** US (CAN-SPAM) and Canadian (CASL) law require a postal address in commercial email. While `postal_address` is empty, US and Canadian companies are skipped and everyone else is contacted.

**5. Check and preview.** `Setup.command` runs these for you; you can also run them by hand:

```bash
OA=".venv/bin/python -m outreach_agent --home ~/Outreach/MyBusiness"
$OA doctor --live          # every connection and profile file, plus one real model call
$OA send-test              # emails you, then checks the From address and SPF/DKIM/DMARC
$OA pipeline --count 3     # find, research and draft 3 prospects, printed here; nothing sent
```

**6. Run.** `Setup.command` installs the background service. Otherwise:

```bash
./run.sh ~/Outreach/MyBusiness     # foreground; Ctrl-C to stop
$OA install-service                # background, starts at login, restarts if it crashes
$OA uninstall-service              # stop it for good
```

## In Discord

| Button | What it does |
|---|---|
| ✅ Approve | Queues the sequence. The first email goes in their working hours; follow-ups on day 4 and day 10. |
| ✏️ Edit | Edit the subject and all three emails yourself. |
| 🔄 Rewrite | Tell it what to change and it redrafts. |
| ✖️ Reject | Skip this one. |
| 🚫 Never contact | Skip, and never email anyone at this company. |

Approved cards then show each email's delivery status. Replies arrive as a card that pings you, with **Send reply**, **Edit** and **I'll handle it**.

Type a command in the channel or use the `/` menu. The list is pinned in the channel:

`status` · `queue` · `approve all` · `stop` / `start` (all work) · `pause` / `resume` (sending only) · `restart` · `cap 30` · `find <segment> 5` · `redraft` · `brain api` / `brain subscription` · `credits 85.80` · `help`

`status` shows today's sends and cap, the queue, what it's working on right now, your Claude plan usage, and API spend. A summary posts every morning at `digest_hour`.

## Volume and safety

- **The daily cap starts at 20.** It rises by 5 each day that yesterday's cap was mostly used and bounces stayed under 3%, up to `max_daily_cap`. Gmail itself stops a personal account at about 500 a day.
- **Sending pauses itself** if more than 5% of the last 50 emails bounce. Fix the cause, then type `resume`.
- **Countries** are set in `allowed_countries`. Check the rules where you send: Germany and Austria, for example, expect prior consent even for business email.
- **Model calls are capped** per day (`max_calls_per_day`), so outreach can't use up the Claude quota you need for your own work.
- **Prompt injection:** scraped pages and replies are passed to the model as untrusted data. Model output is checked by fixed rules, not trusted, and nothing is sent without your approval.

**Deliverability with a personal Gmail.** Mail sent through a personal Gmail as an address on your own domain is signed by gmail.com, so DMARC alignment fails (`send-test` shows this). It still delivers at these volumes. Keep your DMARC policy at `p=none`. For full alignment, use Google Workspace, or another provider that signs with your domain.

You're responsible for complying with the anti-spam and privacy laws where you and your recipients are. The defaults (approval for every email, opt-out in every email, honouring unsubscribes, published business addresses only) are a good start, not legal advice.

## Not just sales

The same loop works for anything where you'd write short, specific, personal emails to organisations: a job search, partnerships, fundraising, recruiting creators. For a job search, for example:

- set `commercial = false` under `[business]`, which drops the sales-style opt-out footer;
- attach your CV with `attachment` under `[sender]`;
- set `preferred_inboxes = ["jobs", "careers", "hiring"]` and `page_hints = ["careers", "jobs"]` under `[targets]`;
- list companies you've already applied to in `exclude`;
- set `form_only = "card"`, so roles that only take applications through a form still reach you, as a card with the link and a ready cover note;
- describe the roles you want as segments, and your experience in `facts.md`.

## Several businesses

Each business is its own home folder, with its own profile, mailbox, Discord channel (or server) and database:

```bash
.venv/bin/python -m outreach_agent init ~/Outreach/SecondBusiness
```

Each one runs as its own background service, named after the business (`dev.outreach-agent.<name>`). **Give each its own Discord channel.** They can share one Discord bot (same token, different `DISCORD_CHANNEL_ID`): each answers only in its own channel. They can share a mailbox too, since each only reads replies to its own emails. Daily caps are per business; the Claude plan and your mailbox's own sending limit are shared.

## Layout

```
outreach-agent/            the program (this repo)
  outreach_agent/          engine, pipeline, mailer, Discord bot, pre-send gate
  prompts/                 generic task prompts, filled in from your profile
  profiles/example/        the starter profile `init` copies
  config.example.toml      every setting, commented
  run.sh, scripts/         the service runner and the Setup/Stop scripts

~/Outreach/MyBusiness/     one business (a home folder; never commit it)
  config.toml  .env  profile/  data/outreach.db  data/outreach.log  Setup.command  Stop.command
```

## Development

```bash
.venv/bin/python -m pytest -q
```

The tests run the whole pipeline offline with a fake model, a fake website and a dry-run mailer.

## License

MIT. See [LICENSE](LICENSE).
