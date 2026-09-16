@echo off
rem SSH-tunnel to the external processor on the owner server. Kept alive in a loop; started hidden by tunnel_hidden.vbs (autostart: copy of tunnel_hidden.vbs in shell:startup as TeamsProcessorTunnel.vbs).
:loop
"C:\Windows\System32\OpenSSH\ssh.exe" -N -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -o ExitOnForwardFailure=yes -o StrictHostKeyChecking=accept-new -L 8756:127.0.0.1:8756 lolkof-srv
timeout /t 10 /nobreak >nul
goto loop
