@echo off
setlocal

set "VENV_PYTHON=%~dp0.venv\Scripts\python.exe"
if not exist "%VENV_PYTHON%" (
	echo.
	echo Project virtual environment not found at %VENV_PYTHON%
	echo Create it first with: py -m venv .venv
	exit /b 1
)

"%VENV_PYTHON%" -m pip install -r "%~dp0requirements.txt"
echo.
echo Available local IPv4 addresses:
powershell -NoProfile -Command "Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -notlike '169.254*' -and $_.IPAddress -ne '127.0.0.1' } | Select-Object -ExpandProperty IPAddress"
echo.
echo Starting Streamlit on all network interfaces...
echo Use: http://YOUR_IP:8501 from another device on the same network.
"%VENV_PYTHON%" -m streamlit run "%~dp0app.py" --server.address 0.0.0.0 --server.port 8501 --server.headless true