@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem Uruchamia aplikacje; brakujace pakiety Pythona doinstaluje ona sama (zapyta o zgode).
where pythonw >nul 2>nul && (start "" pythonw "%~dp0notatnik.pyw" & exit /b 0)
where pyw >nul 2>nul && (start "" pyw -3 "%~dp0notatnik.pyw" & exit /b 0)
echo Nie znaleziono Pythona na tym komputerze.
choice /c TN /m "Zainstalowac Python 3.12 (winget, tylko dla tego uzytkownika)"
if errorlevel 2 exit /b 1
winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
if not exist "%LOCALAPPDATA%\Programs\Python\Python312\pythonw.exe" (echo Instalacja Pythona nie powiodla sie. & pause & exit /b 1)
start "" "%LOCALAPPDATA%\Programs\Python\Python312\pythonw.exe" "%~dp0notatnik.pyw"
