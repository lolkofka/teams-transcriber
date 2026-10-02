# TeamsTranscriber для Mac с Apple Silicon

Сборка находится в архиве `TeamsTranscriber-macos-arm64.zip` из GitHub Actions. Распакуйте архив на Mac и перенесите `TeamsTranscriber.app` в «Программы». Приложение собрано для `arm64` и подписано временной подписью; для первого запуска может понадобиться открыть его через контекстное меню «Открыть» и подтвердить запуск в настройках конфиденциальности macOS.

## Запись звука Teams

macOS не даёт приложению напрямую захватывать звук другого приложения через CoreAudio. Установите виртуальное аудиоустройство [BlackHole 2ch](https://existential.audio/blackhole/), выберите BlackHole как **динамик в настройках Teams**, а в TeamsTranscriber выберите BlackHole как **вход**. В настройках macOS разрешите TeamsTranscriber доступ к микрофону. Для прослушивания выберите в программе свои наушники как **выход**. Не выбирайте BlackHole выходом программы: это создаст звуковую петлю.

Если нужны и запись, и системное воспроизведение звука Teams, оставьте выход Teams на BlackHole, а прослушивание включите через выход TeamsTranscriber. При первом запуске модели Whisper и ECAPA скачиваются из интернета; Vosk для быстрого черновика включён в архив.

Настройки, модели и записи лежат в `~/Library/Application Support/TeamsTranscriber/`. Можно открыть папку записей кнопкой в приложении.

## Ограничения версии для macOS

Автоматическое сохранение слайдов Teams, отслеживание кнопки микрофона Teams, установка VB-Cable и UnityCapture требуют Windows. Для записи собственного голоса выберите режим «всегда». Виртуальную камеру можно использовать через OBS Virtual Camera, если она доступна на Mac.

## Сборка из исходников

На Mac с Apple Silicon и Python 3.11:

```sh
brew install portaudio
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-macos.txt
python -m PyInstaller --noconfirm --clean TeamsTranscriber-macos.spec
```

Для включения Vosk поместите `models/vosk-model-small-ru-0.22` в репозиторий до сборки. Готовое приложение: `dist/TeamsTranscriber.app`.
