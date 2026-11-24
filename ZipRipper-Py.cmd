@ECHO OFF
REM ==========================================================================
REM  ZipRipper Python 版启动器（Windows 双击运行）
REM  作用：定位同目录的 zipripper.py，用本机 Python 启动网页界面。
REM  需要用户自备 Python 3.9+ 并已加入 PATH。
REM ==========================================================================
SETLOCAL
CD /D "%~dp0"

WHERE python.exe >nul 2>&1
IF ERRORLEVEL 1 (
	ECHO 未找到 python.exe，请先安装 Python 3.9 或更高版本，并勾选“Add Python to PATH”。
	ECHO 下载地址：https://www.python.org/downloads/windows/
	ECHO/
	PAUSE
	EXIT /b 1
)

python "%~dp0zipripper.py" %*
IF ERRORLEVEL 1 (
	ECHO/
	ECHO ZipRipper 异常退出，请查看上方的错误信息。
	PAUSE
)
ENDLOCAL
