Set objFSO = CreateObject("Scripting.FileSystemObject")
strRoot = objFSO.GetParentFolderName(WScript.ScriptFullName)

If Not objFSO.FileExists(strRoot & "\execution\main.py") Then
    WScript.Quit 1
End If

Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = strRoot

If objFSO.FileExists(strRoot & "\.venv\Scripts\pythonw.exe") Then
    WshShell.Run Chr(34) & strRoot & "\.venv\Scripts\pythonw.exe" & Chr(34) & " execution\main.py", 0, False
Else
    WshShell.Run "pythonw.exe execution\main.py", 0, False
End If

Set WshShell = Nothing
Set objFSO = Nothing
