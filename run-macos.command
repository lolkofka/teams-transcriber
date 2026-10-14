#!/bin/zsh
set -e
cd -- "${0:A:h}"
if [[ ! -x .venv/bin/python ]]; then
  print 'Сначала установите зависимости по README-macos.md'
  exit 1
fi
if [[ ! -x dist/TeamsTranscriber.app/Contents/MacOS/TeamsTranscriber ]]; then
  .venv/bin/python build_macos_launcher.py
fi
exec open dist/TeamsTranscriber.app
