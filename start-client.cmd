@echo off
setlocal
set "REPO=%~dp0"
set "PY=%REPO%.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=C:/Users/LAMNING/WorkBuddy/2026-11-10-14-55-02/.venv-tf/Scripts/python.exe"
if not exist "%PY%" set "PY=python"

netstat -ano | findstr /R /C:":8000 .*LISTENING" >nul 2>&1
if errorlevel 1 (
  echo [1/2] starting server on port 8000 ...
  pushd "%REPO%server"
  start "TeamFlow Server" /min "%PY%" main.py --port 8000
  popd
  timeout /t 5 /nobreak >nul
) else (
  echo [1/2] server already listening on 8000
)

echo [2/2] opening client window ...
rem 必须显式传 --root：只靠「当前目录」的话，从桌面/资源管理器启动时
rem 工作目录可能是 Documents 之类的别处，客户端就会去监听一个空目录，
rem 表现是「改了代码但永远没有洞察」。
pushd "%REPO%"
"%PY%" -m client.main ui --root "%REPO%."
popd
endlocal
