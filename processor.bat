@echo off
cd /d "%~dp0"
rem Внешний обработчик звука. Токен лежит в proc_token.txt рядом.
rem На машине с видеокартой: замените --device cpu на --device cuda
set /p TOK=<proc_token.txt
start "Teams transcriber processor" /min .venv\Scripts\python.exe -X utf8 processor_server.py --model large-v3-turbo --device cpu --threads 6 --port 8756 --token %TOK%
