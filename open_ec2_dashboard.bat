@echo off
rem Opens the EC2 (Mumbai) QuantLag HFT dashboard at http://localhost:8092 through an SSH tunnel.
rem Keep this window open while you use it; closing it only closes the tunnel - the bot on
rem the server keeps running.
title QuantLag HFT EC2 tunnel - close to disconnect
start "" cmd /c "timeout /t 3 >nul & start http://localhost:8092"
ssh -i "%USERPROFILE%\.ssh\lagforge_ec2" -o ServerAliveInterval=30 -o ExitOnForwardFailure=yes -N -L 8092:localhost:8001 ubuntu@13.204.65.248
echo.
echo Tunnel closed. If it failed straight away: your home IP may have changed -
echo update the SSH inbound rule (Source: My IP) on security group sg-09a7efde8075f0c4e.
pause
