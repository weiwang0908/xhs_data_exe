@echo off
rem ===========================================================================
rem  红薯雷达 —— 一键打包脚本
rem  真正的打包逻辑在 build.py（Python），这里只做环境检查与调用，
rem  以避免批处理在中文路径/编码下出现乱码问题。
rem ===========================================================================
setlocal
chcp 65001 >nul
cd /d "%~dp0"

set "PY=%CD%\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

echo.
echo [红薯雷达] 使用解释器: %PY%
echo.

"%PY%" -c "import sys; print('Python', sys.version.split()[0])"
if errorlevel 1 (
    echo.
    echo [错误] 未找到可用的 Python，请先安装 Python 3.11 或 3.12。
    pause
    exit /b 1
)

"%PY%" "%~dp0build.py" %*
set "RC=%ERRORLEVEL%"

echo.
if "%RC%"=="0" (
    echo [红薯雷达] 打包成功。
) else (
    echo [红薯雷达] 打包失败，退出码 %RC%。
)
echo.
pause
exit /b %RC%
