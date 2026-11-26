Set objFSO = CreateObject("Scripting.FileSystemObject")
strDefaultRoot = "E:\Bots_and_Tools\Windows_Remote_Screen_Mirror_Bot"
strScriptDir = objFSO.GetParentFolderName(WScript.ScriptFullName)

If objFSO.FileExists(strScriptDir & "\execution\main.py") Then
    strRoot = strScriptDir
Else
    strRoot = strDefaultRoot
End If

Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = strRoot

If objFSO.FileExists(strRoot & "\.venv\Scripts\pythonw.exe") Then
    WshShell.Run Chr(34) & strRoot & "\.venv\Scripts\pythonw.exe" & Chr(34) & " execution\main.py", 0, False
Else
    WshShell.Run "cmd.exe /c " & Chr(34) & strRoot & "\.venv\Scripts\python.exe" & Chr(34) & " execution\main.py", 0, False
End If

Set WshShell = Nothing
Set objFSO = Nothing
