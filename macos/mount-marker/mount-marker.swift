import AppKit
import Foundation

let env = ProcessInfo.processInfo.environment

let markerName: String = {
    if let override = env["RIVERHOG_MOUNT_MARKER_NAME"], !override.isEmpty {
        return override
    }
    return ".riverhog.mount-marker"
}()

let triggersDir: String = {
    if let override = env["RIVERHOG_MOUNT_MARKER_TRIGGERS_DIR"], !override.isEmpty {
        return override
    }

    let home = FileManager.default.homeDirectoryForCurrentUser
    let url = home
        .appendingPathComponent("Library")
        .appendingPathComponent("Application Support")
        .appendingPathComponent("riverhog")
        .appendingPathComponent("mount-triggers")
    try? FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
    return url.path
}()

@inline(__always) func log(_ s: String) {
    FileHandle.standardOutput.write((s + "\n").data(using: .utf8)!)
}

@inline(__always) func warn(_ s: String) {
    FileHandle.standardError.write(("[WARN] " + s + "\n").data(using: .utf8)!)
}

@inline(__always) func shQuote(_ s: String) -> String {
    "'" + s.replacingOccurrences(of: "'", with: "'\\''") + "'"
}

func maybeRun(for volumeURL: URL) {
    let markerURL = volumeURL.appendingPathComponent(markerName)
    guard let data = try? Data(contentsOf: markerURL),
          let name = String(data: data, encoding: .utf8)?
            .split(separator: "\n").first.map(String.init)?
            .trimmingCharacters(in: .whitespacesAndNewlines),
          !name.isEmpty
    else { return }

    let scriptURL = URL(fileURLWithPath: triggersDir).appendingPathComponent(name)
    let prog = scriptURL.path
    guard FileManager.default.isExecutableFile(atPath: prog) else {
        warn("Executable not found: \(prog)")
        return
    }

    log("Mount: \(volumeURL.path) -> \(prog)")

    let script = """
    tell application \"Terminal\"
      activate
      do script \"exec \(shQuote(prog)) \(shQuote(volumeURL.path))\"
    end tell
    """

    if let asObj = NSAppleScript(source: script) {
        var err: NSDictionary?
        _ = asObj.executeAndReturnError(&err)
        if let e = err { warn("AppleScript error: \(e)") }
    } else {
        warn("Failed to construct AppleScript.")
    }
}

let nc = NSWorkspace.shared.notificationCenter
var token: NSObjectProtocol?
token = nc.addObserver(forName: NSWorkspace.didMountNotification,
                       object: nil, queue: .main) { note in
    let uAny = note.userInfo?[NSWorkspace.volumeURLUserInfoKey] ??
               note.userInfo?["NSWorkspaceVolumeURLKey"]
    if let url = uAny as? URL {
        log("DidMount: \(url.path)")
        maybeRun(for: url)
    } else {
        warn("DidMount without URL in userInfo")
    }
}

log("mount marker listener started (pid \(getpid())) marker: \(markerName) triggers: \(triggersDir)")
RunLoop.main.run()
