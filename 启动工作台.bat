@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 正在启动公众号文章 → Word 工作台...
echo.
".venv\Scripts\w2d.exe" serve --port 8865
echo.
echo 工作台已停止。按任意键关闭窗口。
pause >nul
