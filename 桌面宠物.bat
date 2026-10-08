@echo off
rem Launcher for the desktop pet. It runs main.py with a renamed copy of the
rem interpreter, so Task Manager shows the process as the pet's own name.
cd /d "%~dp0"
start "" "%~dp0.venv\Scripts\桌面宠物.exe" "%~dp0main.py"
rem 启动桌面宠物：用改名后的解释器副本 .venv\Scripts\桌面宠物.exe 跑 main.py。
rem 中文注释必须放在最后一行——cmd 在 UTF-8 代码页下解析多字节字符会错位，注释若在前面会把命令拆坏。
