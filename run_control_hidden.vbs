' Zapusk lokalnogo obrabotchika komand BEZ okna.
' Vse literaly ASCII: put s kirillicey beretsya iz ScriptFullName vo vremya raboty.
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
here = fso.GetParentFolderName(WScript.ScriptFullName)
py = "C:\Users\o2385\AppData\Local\Microsoft\WindowsApps\PythonSoftwareFoundation.Python.3.12_qbz5n2kfra8p0\python.exe"
sh.CurrentDirectory = here
sh.Run """" & py & """ -u """ & here & "\control_daemon.py""", 0, False
