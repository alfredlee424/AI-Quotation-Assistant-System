' ============================================================
' launcher.vbs - AI 報價助理系統入口點
' 使用者雙擊此檔案即可啟動系統
' 特性：隱藏執行 launcher.bat，不出現黑色 CMD 視窗
' ============================================================

Dim oShell, scriptDir, batPath

Set oShell = CreateObject("WScript.Shell")

' 取得本 VBS 檔案所在目錄，確保相對路徑正確
scriptDir = Left(WScript.ScriptFullName, InStrRev(WScript.ScriptFullName, "\"))
batPath = scriptDir & "launcher.bat"

' Run 參數說明：
'   第二個參數 0 = 隱藏視窗（不顯示黑色 CMD）
'   第三個參數 False = 非同步執行（不等待 bat 完成）
oShell.Run """" & batPath & """", 0, False

Set oShell = Nothing
