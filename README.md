# RozNews

A swipeable news reel with two tabs.

- **News**: one story per screen with a picture and a short AI summary, across 7 channels (**Tech** is aimed at software developers and AI, and mixes news with *findings*: new GitHub repos and Show HN projects worth trying, shown as "Check it out" cards):
  Tech, AI & Cloud, Tech Job Market, Canada & Alberta, Business & Markets, Games, Movies.
- **Trending**: what everyone is talking about, in one mixed reel of **memes**, **topics** (viral news, celebrity and pop culture, sports, Bollywood/OTT/music) and **trending videos**. About half Indian ("Desi" badge, plus a Desi filter chip) and half worldwide. Topic and video cards carry a **"say this to a friend"** line: tap it to copy it (+5 XP).

- Swipe up for the next story. Channels are interleaved so you never get five of the same kind in a row.
- Each story animates in: the headline slides up word by word and the picture drifts into place.
- **Teach it what you don't want.** Tap the three dots on a story, then *Not interested*, then pick a reason. It learns (see below) and offers Undo.
- Earn XP for reading (+10), opening a story (+20) and saving (+5). Level up every 100 XP. Read 5 new stories a day to keep your streak.
- Double-tap a story, or tap the heart, to save it. Saved stories stay even after the feed refreshes.
- Keys on desktop: arrows or J/K to move, S to save, O to open, X to hide.

```
RSS + Google News ──► dedupe, last 13h ──► Gemini picks the top 6 per channel,
                                           writes headline, 2-sentence summary, topic tags
                                                      │
        article picture (feed image or the page's og:image)
                                                      │
        GitHub Actions every 4 hours ──► docs/data.js ──► GitHub Pages site
```

## Stories you've already seen

A story counts as seen once per visit (30+ minutes apart, so reloading doesn't count twice). After it has been seen on **2 visits** it stops appearing in the feed. Saved stories always stay in **Saved**. Tap **Hidden** to see how many are being held back and bring them all back.

## Tech findings

The Tech channel pulls brand-new GitHub repos that are gaining stars fast (topics: llm, ai-agents, mcp, claude-code, vibe-coding, developer-tools) and recent Show HN projects, mixes them with developer and AI news, and Gemini picks about half findings. Edit the `finds:` block of Tech in `config.yaml` to change topics, how recent, or the minimum stars. On GitHub Actions the search uses the built-in token automatically. On your PC it works without one but is slower; add `GITHUB_TOKEN=` (a personal access token with no scopes) to `.env` to speed it up.

## The Trending tab

```
Reddit (Indian + worldwide meme subs) ──────────────► meme cards (shown as-is, no AI)
Google Trends (IN, US, GB, CA) ─┐
Google News searches ───────────┼─► Gemini picks + writes headline, summary, "say this" line ─► topic cards
Wikipedia most-read pages ──────┘
YouTube trending (IN, US, GB) ──► Gemini picks + writes the same ───────────────────────────► video cards
                                              │
                      interleaved, ~50% Indian / 50% worldwide ──► docs/trending.js
```

`python news_bot.py --trending` builds it. Settings (subreddits, countries, how many cards, the Indian share) are in the `trending:` block of `config.yaml`. Language mix: English and Hinglish come from the Indian meme subs and Indian news; Punjabi comes from the Punjabi searches and YouTube trending. Card text is always casual English.

**Check what works from your PC first:**

```powershell
python news_bot.py --check-sources
```

It prints OK or FAIL for every source. A few notes on sources:

- **YouTube trending needs a key.** In https://console.cloud.google.com, open the project your AI Studio key belongs to (or make a new one), search for **YouTube Data API v3** and click **Enable**. Then either put `YOUTUBE_API_KEY=...` in `.env` (and as a GitHub secret of the same name) or leave it blank to try the Gemini key. Without it the Videos part is simply left out.
- **Reddit sometimes blocks cloud servers like GitHub Actions** (it often works from your own PC). The bot tries `www.reddit.com`, then `old.reddit.com`, then the public `meme-api.com`, and fills worldwide memes from Bluesky if all of that fails. If your Indian meme subs come back empty on the live site, check the `--check-sources` output and tell me which ones fail.
- **Instagram and TikTok have no public feed**, so their reels can't be pulled directly. Reddit, YouTube and Google Trends are where the same memes and trends show up first.
- Meme cards are filtered to exclude adult, spoiler and video posts, but memes are other people's posts: the card links to the original.

## How "Not interested" works

Every story carries 1-3 topic tags. The reason you pick decides what changes:

| You pick | What happens |
|---|---|
| Not into this topic | That topic is muted. Any story with it disappears, now and in future updates. |
| Don't want stories from *source* | That source is muted. |
| Show less *channel* | The whole channel sinks lower in the order (never fully removed). |
| I've already seen this | Just this story goes away. |
| Just not for me | This story goes away and its topics lose a little weight (never fully muted). |

Saving and opening stories do the opposite: their topics float up. Tap the **Hidden** chip to see everything you've muted and bring any of it back.

What it learns is stored in your browser (no account, no server), so it is per device. Clearing site data resets it.

## Run it on your PC (Windows PowerShell)

```powershell
cd "D:\Other Projects\RozNews\news-bot\news-bot"
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python news_bot.py --reel
start docs\index.html
```

`--reel` fetches the news and writes `docs/data.js`. Add `--trending` (alone or together: `python news_bot.py --reel --trending`) to build the Trending tab's `docs/trending.js`. `docs/index.html` is the site and opens straight from the folder.

### Google AI Studio key
1. Go to https://aistudio.google.com/apikey and create a key.
2. Put it in `.env`: `GEMINI_API_KEY=AIza...`
3. Without a key you still get headlines and pictures, just no summaries.

The model is set in `config.yaml` (`ai: model:`). It defaults to `gemini-3.5-flash-lite` and falls back to the next model in `fallback_models` if one is unavailable. Check https://aistudio.google.com/rate-limit for what your key's free tier allows. News makes 7 calls per run and Trending about 4, spaced 5 seconds apart. At every 4 hours that is roughly 65 calls a day; if your free tier is lower, change the cron in `digest.yml` to every 6 or 8 hours.

## Put it online (free, GitHub Pages)

1. Create a new GitHub repo and push this folder to the `main` branch.
2. Repo **Settings → Secrets and variables → Actions → New repository secret**: add `GEMINI_API_KEY` (and optionally `YOUTUBE_API_KEY`).
3. Repo **Settings → Pages → Build and deployment → Source: GitHub Actions**.
4. **Actions** tab → *Update news reel* → **Run workflow**.

Your site appears at `https://<your-username>.github.io/<repo-name>/` and refreshes every 4 hours. On your phone use **Add to Home Screen** to make it feel like an app.

## Customize

| Want to... | Edit |
|---|---|
| Add or remove a channel, change its color or emoji | `categories:` in `config.yaml` |
| Follow a specific topic | add a line under `queries:` |
| More or fewer stories per channel | `reel_top_n` |
| What counts as "important" | the channel's `focus:` line |
| Refresh schedule | the `cron:` line in `.github/workflows/digest.yml` (UTC) |
| Trending: more Indian or more worldwide, other subreddits or countries | `trending:` in `config.yaml` |
| Streak goal, XP values, how strongly hiding works | top of the script in `docs/index.html` |

## Notes
- Pictures come from the story's feed when it has one, otherwise from the article page's preview image. Stories with no picture show a large emoji instead. Images load straight from the publisher's site, so a few may be blocked and fall back to the emoji.
- Google News results are redirect links. `googlenewsdecoder` (in `requirements.txt`) finds the real article so its picture and link work. If it can't, that story keeps the redirect link and no picture.
- Summaries come from each story's headline and feed snippet, so they are short on purpose.
- GitHub pauses scheduled workflows after 60 days with no repo activity. It emails you first; one click turns it back on.
- The old email digest still works: `python news_bot.py` (needs the Gmail settings in `.env`).
- Fonts (Bricolage Grotesque, Newsreader) are bundled in `docs/fonts` under the SIL Open Font License.
