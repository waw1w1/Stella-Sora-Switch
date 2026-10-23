@echo off
setlocal
chcp 65001 >nul
pushd "%~dp0"
set "art_python=%~dp0.python\python.exe"
set "art_flags=-I"
if exist "%art_python%" goto run
set "art_python=%~dp0.venv\Scripts\python.exe"
if not exist "%art_python%" (
  echo 未找到本地运行环境，请按 README 使用 uv 安装，或下载并完整解压免安装包。
  set "result=1"
  goto finish
)
:run
"%art_python%" %art_flags% -B -X utf8 "%~dp0replace_art.py" configure
set "result=%errorlevel%"
if not "%result%"=="0" echo 操作未完成，请查看上方提示。
:finish
echo.
pause
popd
endlocal & exit /b %result%
