#!/usr/bin/env bash
# Brands the Telegram bot through the Bot API: name, descriptions (German default,
# English localized) and the profile photo rendered by this Remotion project.
# Headless and idempotent — run it again after re-rendering the avatar.
#
#   TELEGRAM_BOT_TOKEN=... ./media/telegram-profile.sh              # static JPG
#   TELEGRAM_BOT_TOKEN=... ./media/telegram-profile.sh --animated   # looping MP4
set -euo pipefail

: "${TELEGRAM_BOT_TOKEN:?set TELEGRAM_BOT_TOKEN (the token from @BotFather)}"
API="https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}"
ASSETS="$(cd "$(dirname "$0")/../docs/assets" && pwd)"

NAME="Project Pilot"

SHORT_DE="Findet passende Freelance-Projekte in Minuten und schickt nur die echten Matches."
SHORT_EN="Spots matching freelance projects within minutes and sends only the real matches."

DESC_DE="🎯 Project Pilot beobachtet Freelance-Projektbörsen und prüft jedes neue Projekt gegen ein persönliches Profil: erst harte Regeln, dann ein LLM-Match.

Nur echte Treffer landen hier als Karte:
• Projektbeschreibung öffnen – das Original-Inserat
• Bewerben – neuer Claude-Chat mit der Karte, Entwurf mit einem Tipp
• Ablehnen – Karte weg

Privater Bot eines einzelnen Nutzers, keine Befehle."

DESC_EN="🎯 Project Pilot watches freelance project boards and checks every new listing against a personal profile: hard rules first, then an LLM match.

Only real matches arrive here as a card:
• Projektbeschreibung öffnen – the original listing
• Bewerben – a new Claude chat with the card, draft in one tap
• Ablehnen – the card is gone

Private single-user bot, no commands."

call() {
  local method=$1
  shift
  local response
  response=$(curl -sS "$API/$method" "$@")
  if [[ $response == *'"ok":true'* ]]; then
    echo "✓ $method"
  else
    echo "✗ $method: $response" >&2
    return 1
  fi
}

call setMyName --data-urlencode "name=$NAME"

call setMyShortDescription --data-urlencode "short_description=$SHORT_DE"
call setMyShortDescription --data-urlencode "short_description=$SHORT_EN" --data-urlencode "language_code=en"

call setMyDescription --data-urlencode "description=$DESC_DE"
call setMyDescription --data-urlencode "description=$DESC_EN" --data-urlencode "language_code=en"

if [[ ${1:-} == "--animated" ]]; then
  call setMyProfilePhoto \
    -F 'photo={"type":"animated","animation":"attach://avatar","main_frame_timestamp":0}' \
    -F "avatar=@$ASSETS/telegram-avatar.mp4;type=video/mp4"
else
  call setMyProfilePhoto \
    -F 'photo={"type":"static","photo":"attach://avatar"}' \
    -F "avatar=@$ASSETS/telegram-avatar.jpg;type=image/jpeg"
fi
