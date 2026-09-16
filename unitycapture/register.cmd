@echo off
regsvr32 /s /u "C:\Users\root\PycharmProjects\teams-transcriber\unitycapture\UnityCapture-master\Install\UnityCaptureFilter32.dll"
regsvr32 /s "/i:UnityCaptureName=HD Web Camera" "C:\Users\root\PycharmProjects\teams-transcriber\unitycapture\UnityCapture-master\Install\UnityCaptureFilter32.dll"
regsvr32 /s /u "C:\Users\root\PycharmProjects\teams-transcriber\unitycapture\UnityCapture-master\Install\UnityCaptureFilter64.dll"
regsvr32 /s "/i:UnityCaptureName=HD Web Camera" "C:\Users\root\PycharmProjects\teams-transcriber\unitycapture\UnityCapture-master\Install\UnityCaptureFilter64.dll"
