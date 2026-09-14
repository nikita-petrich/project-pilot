# media

project-pilot's visual identity as code: the logo, the README banner and
animation, and the Telegram bot's profile photo are
[Remotion](https://www.remotion.dev) compositions, so changing one is an edit
and a re-render, not a design-tool round trip. Nothing here ships in the image.

| Composition | Output | Used by |
|---|---|---|
| `Logo` | `docs/assets/logo.png` | README header |
| `Banner` | `docs/assets/banner.png` | README hero, GitHub social preview |
| `Pipeline` | `docs/assets/pipeline.gif` | README "How it works" |
| `Avatar` | `docs/assets/telegram-avatar.jpg` | Telegram profile photo |
| `AvatarLoop` | `docs/assets/telegram-avatar.mp4` | animated profile photo (optional) |

```sh
cd media
npm install
npm run studio     # live preview of every composition
npm run render     # re-render everything into docs/assets/
```

The mark (`src/Mark.tsx`) is a navigation arrow in a radar that has just caught one
blip, the match. Colors and fonts live in `src/theme.ts`.

## Telegram bot profile

`telegram-profile.sh` sets the bot's name, short description, description
(German default, English for English clients) and profile photo through the Bot
API. No BotFather clicking, safe to re-run:

```sh
TELEGRAM_BOT_TOKEN=... ./media/telegram-profile.sh             # static photo
TELEGRAM_BOT_TOKEN=... ./media/telegram-profile.sh --animated  # looping video
```
