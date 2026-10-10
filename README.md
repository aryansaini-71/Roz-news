# RozNews

A swipeable news reel. One story per screen with a picture and a short AI summary, across 7 channels:
**Tech, AI & Cloud, Tech Job Market, Canada & Alberta, Business & Markets, Games, Movies.**

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
        GitHub Actions at 7 AM and 6 PM ──► docs/data.js ──► GitHub Pages site
```

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

`--reel` fetches the news and writes `docs/data.js`. `docs/index.html` is the site and opens straight from the folder.

### Google AI Studio key
1. Go to https://aistudio.google.com/apikey and create a key.
2. Put it in `.env`: `GEMINI_API_KEY=AIza...`
3. Without a key you still get headlines and pictures, just no summaries.

The model is set in `config.yaml` (`ai: model:`). It defaults to `gemini-3.5-flash-lite` and falls back to the next model in `fallback_models` if one is unavailable. Check https://aistudio.google.com/rate-limit for what your key's free tier allows. The bot makes 7 calls per run, spaced 5 seconds apart.

## Put it online (free, GitHub Pages)

1. Create a new GitHub repo and push this folder to the `main` branch.
2. Repo **Settings → Secrets and variables → Actions → New repository secret**: add `GEMINI_API_KEY`.
3. Repo **Settings → Pages → Build and deployment → Source: GitHub Actions**.
4. **Actions** tab → *Update news reel* → **Run workflow**.

Your site appears at `https://<your-username>.github.io/<repo-name>/` and refreshes at 7 AM and 6 PM Calgary time (an hour earlier in winter). On your phone use **Add to Home Screen** to make it feel like an app.

## Customize

| Want to... | Edit |
|---|---|
| Add or remove a channel, change its color or emoji | `categories:` in `config.yaml` |
| Follow a specific topic | add a line under `queries:` |
| More or fewer stories per channel | `reel_top_n` |
| What counts as "important" | the channel's `focus:` line |
| Send times | the `cron:` lines in `.github/workflows/digest.yml` (UTC) |
| Streak goal, XP values, how strongly hiding works | top of the script in `docs/index.html` |

## Notes
- Pictures come from the story's feed when it has one, otherwise from the article page's preview image. Stories with no picture show a large emoji instead. Images load straight from the publisher's site, so a few may be blocked and fall back to the emoji.
- Google News results are redirect links. `googlenewsdecoder` (in `requirements.txt`) finds the real article so its picture and link work. If it can't, that story keeps the redirect link and no picture.
- Summaries come from each story's headline and feed snippet, so they are short on purpose.
- GitHub pauses scheduled workflows after 60 days with no repo activity. It emails you first; one click turns it back on.
- The old email digest still works: `python news_bot.py` (needs the Gmail settings in `.env`).
- Fonts (Bricolage Grotesque, Newsreader) are bundled in `docs/fonts` under the SIL Open Font License.
