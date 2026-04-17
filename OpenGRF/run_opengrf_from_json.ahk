#Requires AutoHotkey v2.0
#SingleInstance Off

SetTitleMatchMode 2
DetectHiddenWindows false
CoordMode "Mouse", "Screen"

global LOG_FILE := A_ScriptDir "\opengrf_automation.log"
global CLAIM_ROOT := A_ScriptDir "\window_claims"
global METADATA_BASE_DIR := A_ScriptDir
global KNOWN_TITLES := Map(
    "Choose MSK model", true,
    "Choose IK Result file", true,
    "Input data", true
)
global RUNTIME := ParseRuntimeArgs()

Main()

Main() {
    global LOG_FILE
    global CLAIM_ROOT
    global METADATA_BASE_DIR
    global RUNTIME

    runtime := RUNTIME
    metaPath := runtime["metadata_path"] != "" ? NormalizeCliPath(runtime["metadata_path"]) : A_ScriptDir "\opengrf_metadata.json"
    if runtime["log_path"] != ""
        LOG_FILE := NormalizeCliPath(runtime["log_path"])
    if runtime["claim_root"] != ""
        CLAIM_ROOT := NormalizeCliPath(runtime["claim_root"])

    EnsureParentDir(LOG_FILE)
    if !DirExist(CLAIM_ROOT)
        DirCreate(CLAIM_ROOT)

    try {
        if !FileExist(metaPath)
            throw Error("Metadata file not found: " metaPath)

        metaName := ""
        SplitPath(metaPath, &metaName, &metaDir)
        if (metaDir != "")
            METADATA_BASE_DIR := metaDir

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
        Log("Worker ID: " runtime["worker_id"])
        Log("MATLAB PID: " runtime["matlab_pid"])
        Log("OSIM: " meta["osim"])
        Log("MOT : " meta["mot"])

        RunWatcher(meta, runtime["matlab_pid"] + 0)

        Log("Automation sequence completed.")
        if !runtime["silent"]
            MsgBox("OpenGRF automation steps completed.", "OpenGRF Automation")
        ExitApp(0)
    } catch as err {
        Log("ERROR: " err.Message)
        if !runtime["silent"]
            MsgBox(err.Message, "OpenGRF Automation Error", "Iconx")
        ExitApp(1)
    }
}

RunWatcher(meta, matlabPid) {
    timeoutMs := meta["watch_timeout_sec"] * 1000

    modelDialog := WaitForWindowByTitle("Choose MSK model", timeoutMs, matlabPid)
    Log("Detected Choose MSK model dialog.")
    SelectFileInDialog(modelDialog, meta["osim"])

    motDialog := WaitForWindowByTitle("Choose IK Result file", timeoutMs, matlabPid)
    Log("Detected Choose IK Result file dialog.")
    SelectFileInDialog(motDialog, meta["mot"])

    inputDialog := WaitForWindowByTitle("Input data", timeoutMs, matlabPid)
    Log("Detected Input data dialog.")
    FillInputDialog(inputDialog, meta["start_time"], meta["end_time"], meta["penetration"])

    HandleFrequencyPopup(meta["estimate_frequency_content"])

    Sleep(meta["post_yes_wait_ms"])
}

HandleFrequencyPopup(useYes) {
    ; This popup appears immediately after the input dialog closes.
    ; Startup is serialized, so sending the confirmation key directly is reliable.
    Sleep(2000)

    activeHwnd := WinExist("A")
    if activeHwnd {
        try WinActivate("ahk_id " activeHwnd)
        catch {
        }
    }
    Sleep(200)

    if useYes {
        Send("{Enter}")
        Log("Frequency popup handled with fixed wait + Enter.")
    } else {
        Send("{Tab}{Enter}")
        Log("Frequency popup handled with fixed wait + Tab+Enter.")
    }
}

SelectFileInDialog(hwnd, fullPath) {
    if !FileExist(fullPath)
        throw Error("File does not exist: " fullPath)

    SplitPath(fullPath, &fileName, &dirPath)
    winSpec := "ahk_id " hwnd

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
        throw Error("Failed to close file dialog: " SafeWinTitle(hwnd))
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

FillInputDialog(hwnd, startTime, endTime, penetration) {
    winSpec := "ahk_id " hwnd
    WinActivate(winSpec)
    WinWaitActive(winSpec, "", 5)
    Sleep(250)

    okDirect := true
    okDirect := okDirect && TrySetEdit("Edit1", String(startTime), winSpec)
    okDirect := okDirect && TrySetEdit("Edit2", String(endTime), winSpec)
    okDirect := okDirect && TrySetEdit("Edit3", String(penetration), winSpec)

    if okDirect {
        if TryPressOk(winSpec) {
            WaitForWindowClose(hwnd, 5000)
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
    WaitForWindowClose(hwnd, 5000)
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

TryHandleFrequencyPopup(useYes, timeoutMs, matlabPid) {
    deadline := A_TickCount + timeoutMs
    seenInputDisappear := false

    while (A_TickCount < deadline) {
        if !FindWindowForMatlab("Input data", matlabPid)
            seenInputDisappear := true

        if seenInputDisappear {
            for hwnd in WinGetList() {
                if !WindowBelongsToMatlab(hwnd, matlabPid)
                    continue
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

    if !(w >= 250 && w <= 1000 && h >= 120 && h <= 500)
        return false

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

WaitForWindowByTitle(title, timeoutMs, matlabPid) {
    deadline := A_TickCount + timeoutMs
    while (A_TickCount < deadline) {
        hwnd := FindWindowForMatlab(title, matlabPid)
        if hwnd
            return hwnd
        Sleep(100)
    }
    Log("Timed out waiting for '" title "'. " DescribeCandidates(title))
    throw Error("Timed out waiting for '" title "' dialog for MATLAB PID " matlabPid ".")
}

FindWindowForMatlab(title, matlabPid) {
    for hwnd in WinGetList(title) {
        if WindowBelongsToMatlab(hwnd, matlabPid) && TryClaimWindow(hwnd, title)
            return hwnd
    }

    activeHwnd := WinExist("A")
    if activeHwnd && TitleMatches(activeHwnd, title) && TryClaimWindow(activeHwnd, title)
        return activeHwnd

    windows := WinGetList(title)
    if (windows.Length = 1) {
        hwnd := windows[1]
        if TryClaimWindow(hwnd, title)
            return hwnd
    }

    for hwnd in windows {
        if TryClaimWindow(hwnd, title)
            return hwnd
    }
    return 0
}

TryClaimWindow(hwnd, title) {
    global CLAIM_ROOT
    global RUNTIME

    if !hwnd
        return false

    claimDir := CLAIM_ROOT "\" SanitizeClaimKey(title) "_" hwnd
    ownerFile := claimDir "\owner.txt"
    workerId := RUNTIME["worker_id"] != "" ? RUNTIME["worker_id"] : "worker"

    if DirExist(claimDir) {
        if FileExist(ownerFile) {
            try existing := Trim(FileRead(ownerFile, "UTF-8"))
            catch
                existing := ""
            if (existing = workerId)
                return true
        }
        return false
    }

    try {
        DirCreate(claimDir)
        FileAppend(workerId, ownerFile, "UTF-8")
        Log("Claimed '" title "' window hwnd=" hwnd " via fallback routing.")
        return true
    } catch {
        return false
    }
}

TitleMatches(hwnd, expectedTitle) {
    try title := WinGetTitle("ahk_id " hwnd)
    catch
        return false

    return InStr(title, expectedTitle) > 0
}

SanitizeClaimKey(text) {
    cleaned := RegExReplace(text, "[^A-Za-z0-9]+", "_")
    return cleaned != "" ? cleaned : "window"
}

DescribeCandidates(title) {
    windows := WinGetList(title)
    if (windows.Length = 0)
        return "No matching windows were visible."

    parts := []
    for hwnd in windows {
        pid := 0
        try pid := WinGetPID("ahk_id " hwnd)
        catch
            pid := 0
        parts.Push("hwnd=" hwnd " pid=" pid " title='" SafeWinTitle(hwnd) "'")
    }
    return "Candidates: " ArrayToJoinedString(parts)
}

ArrayToJoinedString(parts) {
    text := ""
    for index, part in parts {
        if (index > 1)
            text .= "; "
        text .= part
    }
    return text
}

WindowBelongsToMatlab(hwnd, matlabPid) {
    if (matlabPid <= 0)
        return true

    winSpec := "ahk_id " hwnd
    try {
        if (WinGetPID(winSpec) = matlabPid)
            return true
    } catch {
    }

    owner := GetOwnerHwnd(hwnd)
    while owner {
        ownerSpec := "ahk_id " owner
        try {
            if (WinGetPID(ownerSpec) = matlabPid)
                return true
        } catch {
        }
        owner := GetOwnerHwnd(owner)
    }

    return false
}

GetOwnerHwnd(hwnd) {
    return DllCall("GetWindow", "ptr", hwnd, "uint", 4, "ptr")
}

WaitForWindowClose(hwnd, timeoutMs) {
    winSpec := "ahk_id " hwnd
    deadline := A_TickCount + timeoutMs
    while (A_TickCount < deadline) {
        if !WinExist(winSpec)
            return true
        Sleep(100)
    }
    return false
}

NormalizeMetadata(meta) {
    for _, key in ["opengrf_folder", "osim", "mot"] {
        if meta.Has(key)
            meta[key] := NormalizePathValue(meta[key])
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

NormalizeCliPath(pathValue) {
    val := Trim(String(pathValue))
    if (val = "")
        return val

    val := StrReplace(val, "/", "\")
    if RegExMatch(val, "^[A-Za-z]:\\") || SubStr(val, 1, 2) = "\\"
        return val

    return A_ScriptDir "\" val
}

NormalizePathValue(pathValue) {
    global METADATA_BASE_DIR

    val := Trim(String(pathValue))
    if (val = "")
        return val

    val := StrReplace(val, "/", "\")

    if RegExMatch(val, "^[A-Za-z]:\\") || SubStr(val, 1, 2) = "\\"
        return val

    return METADATA_BASE_DIR "\" val
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
        "end_time", "",
        "metadata_path", "",
        "log_path", "",
        "claim_root", "",
        "matlab_pid", 0,
        "worker_id", ""
    )

    positionalIndex := 0
    index := 1
    while (index <= A_Args.Length) {
        arg := A_Args[index]
        if (arg = "--silent") {
            runtime["silent"] := true
        } else if (arg = "--metadata-path") {
            index += 1
            runtime["metadata_path"] := RequireOptionValue(arg, index)
        } else if (arg = "--log-path") {
            index += 1
            runtime["log_path"] := RequireOptionValue(arg, index)
        } else if (arg = "--claim-root") {
            index += 1
            runtime["claim_root"] := RequireOptionValue(arg, index)
        } else if (arg = "--matlab-pid") {
            index += 1
            runtime["matlab_pid"] := RequireOptionValue(arg, index) + 0
        } else if (arg = "--worker-id") {
            index += 1
            runtime["worker_id"] := RequireOptionValue(arg, index)
        } else {
            positionalIndex += 1
            if (positionalIndex = 1)
                runtime["mot_path"] := arg
            else if (positionalIndex = 2)
                runtime["start_time"] := arg
            else if (positionalIndex = 3)
                runtime["end_time"] := arg
        }
        index += 1
    }

    return runtime
}

RequireOptionValue(optionName, index) {
    if (index > A_Args.Length)
        throw Error("Missing value for " optionName)
    return A_Args[index]
}

EnsureParentDir(path) {
    fileName := ""
    SplitPath(path, &fileName, &dirPath)
    if (dirPath != "" && !DirExist(dirPath))
        DirCreate(dirPath)
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
