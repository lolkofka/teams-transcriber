Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$s.SetOutputToWaveFile("$PSScriptRoot\test_meeting.wav")
$s.SelectVoice("Microsoft Irina Desktop")
$s.Speak("Добрый день, коллеги. Давайте начнём встречу с обсуждения статуса по проекту миграции. На прошлой неделе мы закончили перенос базы данных, осталось проверить репликацию и настроить мониторинг.")
$s.SelectVoice("Microsoft Zira Desktop")
$s.Speak("Thanks. From my side, the API gateway is deployed to staging and we are running load tests today. If everything is green, we can promote it to production on Thursday.")
$s.SelectVoice("Microsoft Irina Desktop")
$s.Speak("Отлично. Тогда во вторник проведём демонстрацию для заказчика и обсудим сроки релиза. Ещё нужно закрыть вопрос с лицензиями.")
$s.SelectVoice("Microsoft Zira Desktop")
$s.Speak("Sounds good. I will prepare the slides and send the invite. Anything else for today?")
$s.SelectVoice("Microsoft Irina Desktop")
$s.Speak("Нет, на этом всё. Спасибо, до встречи.")
$s.Dispose()
