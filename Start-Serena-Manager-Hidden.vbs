Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
folder = fso.GetParentFolderName(WScript.ScriptFullName)
manager = fso.BuildPath(folder, "Serena-Manager.py")
shell.Run "pyw.exe -3 " & Chr(34) & manager & Chr(34), 0, False
