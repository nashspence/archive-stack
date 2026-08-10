import AppKit
import Foundation

let env = ProcessInfo.processInfo.environment

let markerName = env["GOGURT_MARKER_NAME"]?.isEmpty == false
    ? env["GOGURT_MARKER_NAME"]!
    : ".gogurt"

guard let gogurtExecutable = env["GOGURT_EXECUTABLE"], !gogurtExecutable.isEmpty else {
    fatalError("GOGURT_EXECUTABLE is required")
}
guard let routesFile = env["GOGURT_CONFIG"], !routesFile.isEmpty else {
    fatalError("GOGURT_CONFIG is required")
}
guard let actionsDir = env["GOGURT_ACTIONS_DIR"], !actionsDir.isEmpty else {
    fatalError("GOGURT_ACTIONS_DIR is required")
}

@inline(__always) func log(_ message: String) {
    FileHandle.standardOutput.write((message + "\n").data(using: .utf8)!)
}

@inline(__always) func warn(_ message: String) {
    FileHandle.standardError.write(("[WARN] " + message + "\n").data(using: .utf8)!)
}

@inline(__always) func shQuote(_ value: String) -> String {
    "'" + value.replacingOccurrences(of: "'", with: "'\\''") + "'"
}

func maybeRun(for volumeURL: URL) {
    let markerURL = volumeURL.appendingPathComponent(markerName)
    guard FileManager.default.fileExists(atPath: markerURL.path) else { return }

    let command = [
        gogurtExecutable,
        "run",
        volumeURL.path,
        "--config",
        routesFile,
        "--actions-dir",
        actionsDir,
        "--marker-name",
        markerName,
    ]
    let shellCommand = "exec " + command.map(shQuote).joined(separator: " ")
    let appleScriptCommand = shellCommand
        .replacingOccurrences(of: "\\", with: "\\\\")
        .replacingOccurrences(of: "\"", with: "\\\"")
    log("🛹 gogurt mount: \(volumeURL.path)")

    let script = """
    tell application \"Terminal\"
      activate
      do script \"\(appleScriptCommand)\"
    end tell
    """

    if let appleScript = NSAppleScript(source: script) {
        var error: NSDictionary?
        _ = appleScript.executeAndReturnError(&error)
        if let error { warn("AppleScript error: \(error)") }
    } else {
        warn("Failed to construct AppleScript.")
    }
}

let notificationCenter = NSWorkspace.shared.notificationCenter
let token = notificationCenter.addObserver(
    forName: NSWorkspace.didMountNotification,
    object: nil,
    queue: .main
) { notification in
    let value = notification.userInfo?[NSWorkspace.volumeURLUserInfoKey]
        ?? notification.userInfo?["NSWorkspaceVolumeURLKey"]
    if let url = value as? URL {
        log("🛹 gogurt observed mount: \(url.path)")
        maybeRun(for: url)
    } else {
        warn("DidMount without URL in userInfo")
    }
}

withExtendedLifetime(token) {
    log("🛹 gogurt listener started (pid \(getpid())) marker: \(markerName)")
    RunLoop.main.run()
}
