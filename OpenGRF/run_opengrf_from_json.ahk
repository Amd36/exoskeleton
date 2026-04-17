#Requires AutoHotkey v2.0
#SingleInstance Force

SetTitleMatchMode 2
DetectHiddenWindows false
CoordMode "Mouse", "Screen"

global LOG_FILE := A_ScriptDir "\opengrf_automation.log"
global KNOWN_TITLES := Map(
    "MATLAB R2025a", true,
    "Choose MSK model", true,
    "Choose IK Result file", true,
    "Input data", true
)
global RUNTIME := ParseRuntimeArgs()

Main()

Main() {
    global RUNTIME
    runtime := RUNTIME
    try {
        metaPath := A_ScriptDir "\opengrf_metadata.json"
        if !FileExist(metaPath)
            throw Error("Metadata file not found: " metaPath)

        meta := LoadMetadata(metaPath)
        NormalizeMetadata(meta)
        if runtime["mot_path"] != ""
            meta["mot"] := NormalizePathValue(runtime["mot_path"])
        if runtime["start_time"] != ""
            meta["start_time"] := runtime["start_time"] + 0
        if runtime["end_time"] != ""
            meta["end_time"] := runtime["end_time"] + 0
        ValidateMetadata(meta)

        Log("Loaded metadata from: " metaPath)
        Log("OSIM: " meta["osim"])
        Log("MOT : " meta["mot"])

        RunWatcher(meta)

        if !runtime["silent"]
            MsgBox("OpenGRF automation steps completed.", "OpenGRF Automation")
    } catch as err {
        Log("ERROR: " err.Message)
        if !runtime["silent"]
            MsgBox(err.Message, "OpenGRF Automation Error", "Iconx")
    }
}

RunWatcher(meta) {
    timeoutMs := meta["watch_timeout_sec"] * 1000

    WaitForWindowByTitle("Choose MSK model", timeoutMs)
    Log("Detected Choose MSK model dialog.")
    SelectFileInDialog("Choose MSK model", meta["osim"])

    WaitForWindowByTitle("Choose IK Result file", timeoutMs)
    Log("Detected Choose IK Result file dialog.")
    SelectFileInDialog("Choose IK Result file", meta["mot"])

    WaitForWindowByTitle("Input data", timeoutMs)
    Log("Detected Input data dialog.")
    FillInputDialog("Input data", meta["start_time"], meta["end_time"], meta["penetration"])

    ; Let the frequency popup fully appear, then confirm it directly.
    Sleep(2000)
    if meta["estimate_frequency_content"] {
        Send("{Enter}")
        Log("Frequency popup handled with fixed 2-second wait + Enter.")
    } else {
        Send("{Tab}{Enter}")
        Log("Frequency popup handled with fixed 2-second wait + Tab+Enter.")
    }
    Sleep(meta["post_yes_wait_ms"])

    Log("Automation sequence completed.")
}

SelectFileInDialog(title, fullPath) {
    if !FileExist(fullPath)
        throw Error("File does not exist: " fullPath)

    SplitPath(fullPath, &fileName, &dirPath)

    winSpec := title
    WinActivate(winSpec)
    WinWaitActive(winSpec, "", 5)
    Sleep(250)

    ; Primary method: write the full path into the filename box and press Enter.
    if TrySetFileName(winSpec, fullPath) {
        Send("{Enter}")
        Sleep(700)
        if !WinExist(winSpec)
            return
    }

    Log("Primary file-dialog method did not close the window. Trying address-bar fallback.")

    ; Fallback: go to folder via address bar, then enter only the file name.
    Send("!d")
    Sleep(200)
    SendText(dirPath)
    Sleep(150)
    Send("{Enter}")
    Sleep(900)

    Send("!n")
    Sleep(200)
    SendText(fileName)
    Sleep(150)
    Send("{Enter}")
    Sleep(700)

    if WinExist(winSpec)
        throw Error("Failed to close file dialog: " title)
}

TrySetFileName(winSpec, fullPath) {
    ; Standard Windows file dialog usually exposes the filename field as Edit1.
    for ctrl in ["Edit1", "Edit2", "RichEdit20WPT1"] {
        try {
            ControlFocus(ctrl, winSpec)
            Sleep(80)
            ControlSetText(fullPath, ctrl, winSpec)
            Sleep(120)
            return true
        } catch {
        }
    }

    ; Fallback: use Alt+N to focus "File name"
    try {
        Send("!n")
        Sleep(150)
        SendText(fullPath)
        Sleep(120)
        return true
    } catch {
    }

    return false
}

FillInputDialog(title, startTime, endTime, penetration) {
    winSpec := title
    WinActivate(winSpec)
    WinWaitActive(winSpec, "", 5)
    Sleep(250)

    okDirect := true
    okDirect := okDirect && TrySetEdit("Edit1", String(startTime), winSpec)
    okDirect := okDirect && TrySetEdit("Edit2", String(endTime), winSpec)
    okDirect := okDirect && TrySetEdit("Edit3", String(penetration), winSpec)

    if okDirect {
        if TryPressOk(winSpec) {
            WaitForWindowClose(title, 5000)
            return
        }
    }

    ; Keyboard fallback
    try {
        ControlFocus("Edit1", winSpec)
    } catch {
    }

    Sleep(100)
    Send("^a")
    Sleep(50)
    SendText(String(startTime))
    Sleep(100)
    Send("{Tab}")
    Sleep(100)

    Send("^a")
    Sleep(50)
    SendText(String(endTime))
    Sleep(100)
    Send("{Tab}")
    Sleep(100)

    Send("^a")
    Sleep(50)
    SendText(String(penetration))
    Sleep(150)

    Send("{Enter}")
    WaitForWindowClose(title, 5000)
}

TrySetEdit(ctrl, value, winSpec) {
    try {
        ControlFocus(ctrl, winSpec)
        Sleep(70)
        ControlSetText(value, ctrl, winSpec)
        Sleep(100)
        return true
    } catch {
        return false
    }
}

TryPressOk(winSpec) {
    for ctrl in ["Button1", "OK", "&OK"] {
        try {
            ControlFocus(ctrl, winSpec)
            Sleep(60)
            ControlClick(ctrl, winSpec)
            Sleep(200)
            return true
        } catch {
        }
    }

    try {
        Send("{Enter}")
        Sleep(200)
        return true
    } catch {
        return false
    }
}

TryHandleFrequencyPopup(useYes, timeoutMs) {
    deadline := A_TickCount + timeoutMs
    seenInputDisappear := false

    while (A_TickCount < deadline) {
        if !WinExist("Input data")
            seenInputDisappear := true

        if seenInputDisappear {
            hwnd := WinExist("A")
            if hwnd && IsLikelyDecisionDialog(hwnd) {
                HandlePopupByKeys(hwnd, useYes)
                return true
            }

            for hwnd in WinGetList() {
                if IsLikelyDecisionDialog(hwnd) {
                    HandlePopupByKeys(hwnd, useYes)
                    return true
                }
            }
        }

        Sleep(120)
    }

    Log("Frequency popup was not positively identified within timeout.")
    return false
}

HandlePopupByKeys(hwnd, useYes) {
    winSpec := "ahk_id " hwnd

    try WinActivate(winSpec)
    try WinWaitActive(winSpec, "", 2)
    Sleep(250)

    if useYes {
        Send("{Enter}")
        Log("Frequency popup handled with Enter. Title='" SafeWinTitle(hwnd) "' Class='" SafeWinClass(hwnd) "'")
    } else {
        Send("{Tab}{Enter}")
        Log("Frequency popup handled with Tab+Enter. Title='" SafeWinTitle(hwnd) "' Class='" SafeWinClass(hwnd) "'")
    }
}

IsLikelyDecisionDialog(hwnd) {
    global KNOWN_TITLES
    winSpec := "ahk_id " hwnd

    try mm := WinGetMinMax(winSpec)
    catch
        return false

    if (mm = -1)
        return false

    try title := Trim(WinGetTitle(winSpec))
    catch
        title := ""

    if KNOWN_TITLES.Has(title)
        return false

    try WinGetPos(&x, &y, &w, &h, winSpec)
    catch
        return false

    ; The popup in your screenshot is a small dialog, not a full app window.
    if !(w >= 250 && w <= 1000 && h >= 120 && h <= 500)
        return false

    ; Must have button-like controls
    if !HasButtonLikeControl(winSpec)
        return false

    return true
}

HasButtonLikeControl(winSpec) {
    try ctrls := WinGetControls(winSpec)
    catch
        return false

    for _, ctrl in ctrls {
        if InStr(ctrl, "Button")
            return true
    }

    return false
}

WaitForWindowByTitle(title, timeoutMs) {
    deadline := A_TickCount + timeoutMs
    while (A_TickCount < deadline) {
        hwnd := WinExist(title)
        if hwnd
            return hwnd
        Sleep(100)
    }
    throw Error("Timed out waiting for '" title "' dialog.")
}

WaitForWindowClose(title, timeoutMs) {
    deadline := A_TickCount + timeoutMs
    while (A_TickCount < deadline) {
        if !WinExist(title)
            return true
        Sleep(100)
    }
    return false
}

NormalizeMetadata(meta) {
    for _, key in ["opengrf_folder", "osim", "mot"] {
        if meta.Has(key) {
            meta[key] := NormalizePathValue(meta[key])
        }
    }
}

ValidateMetadata(meta) {
    required := ["osim", "mot", "start_time", "end_time", "penetration", "estimate_frequency_content", "watch_timeout_sec", "post_yes_wait_ms"]

    for _, key in required {
        if !meta.Has(key)
            throw Error("Missing key in metadata: " key)
    }

    if !FileExist(meta["osim"])
        throw Error("OSIM file not found: " meta["osim"])

    if !FileExist(meta["mot"])
        throw Error("MOT file not found: " meta["mot"])

    if (meta["watch_timeout_sec"] < 10)
        meta["watch_timeout_sec"] := 120

    if (meta["post_yes_wait_ms"] < 500)
        meta["post_yes_wait_ms"] := 2000
}

NormalizePathValue(pathValue) {
    val := Trim(String(pathValue))
    if (val = "")
        return val

    val := StrReplace(val, "/", "\")

    if RegExMatch(val, "^[A-Za-z]:\\") || SubStr(val, 1, 2) = "\\"
        return val

    return A_ScriptDir "\" val
}

LoadMetadata(path) {
    text := FileRead(path, "UTF-8")

    meta := Map()
    meta["opengrf_folder"] := JsonGetString(text, "opengrf_folder", "")
    meta["osim"] := JsonGetString(text, "osim", "")
    meta["mot"] := JsonGetString(text, "mot", "")
    meta["start_time"] := JsonGetNumber(text, "start_time", 0)
    meta["end_time"] := JsonGetNumber(text, "end_time", 0)
    meta["penetration"] := JsonGetNumber(text, "penetration", 20)
    meta["estimate_frequency_content"] := JsonGetBool(text, "estimate_frequency_content", true)
    meta["watch_timeout_sec"] := JsonGetNumber(text, "watch_timeout_sec", 120)
    meta["post_yes_wait_ms"] := JsonGetNumber(text, "post_yes_wait_ms", 2000)

    return meta
}

JsonGetString(text, key, defaultValue := "") {
    pat := '"' key '"\s*:\s*"((?:\\.|[^"])*)"'
    if RegExMatch(text, pat, &m)
        return JsonUnescape(m[1])
    return defaultValue
}

JsonGetNumber(text, key, defaultValue := 0) {
    pat := '"' key '"\s*:\s*([-+]?\d+(?:\.\d+)?)'
    if RegExMatch(text, pat, &m)
        return m[1] + 0
    return defaultValue
}

JsonGetBool(text, key, defaultValue := true) {
    pat := '"' key '"\s*:\s*(true|false)'
    if RegExMatch(text, pat, &m)
        return (StrLower(m[1]) = "true")
    return defaultValue
}

JsonUnescape(s) {
    s := StrReplace(s, '\/', '/')
    s := StrReplace(s, '\"', '"')
    s := StrReplace(s, '\\', '\')
    s := StrReplace(s, '\r', "`r")
    s := StrReplace(s, '\n', "`n")
    s := StrReplace(s, '\t', "`t")
    return s
}

ParseRuntimeArgs() {
    runtime := Map(
        "silent", false,
        "mot_path", "",
        "start_time", "",
        "end_time", ""
    )

    positionalIndex := 0
    for _, arg in A_Args {
        if (arg = "--silent")
            runtime["silent"] := true
        else {
            positionalIndex += 1
            if (positionalIndex = 1)
                runtime["mot_path"] := arg
            else if (positionalIndex = 2)
                runtime["start_time"] := arg
            else if (positionalIndex = 3)
                runtime["end_time"] := arg
        }
    }

    return runtime
}

SafeWinTitle(hwnd) {
    try {
        return WinGetTitle("ahk_id " hwnd)
    } catch {
        return ""
    }
}

SafeWinClass(hwnd) {
    try {
        return WinGetClass("ahk_id " hwnd)
    } catch {
        return ""
    }
}

Log(msg) {
    global LOG_FILE
    ts := FormatTime(A_Now, "yyyy-MM-dd HH:mm:ss")
    FileAppend("[" ts "] " msg "`n", LOG_FILE, "UTF-8")
}
