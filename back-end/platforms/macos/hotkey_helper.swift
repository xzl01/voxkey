// SPDX-FileCopyrightText: 2026 HarryLoong
// SPDX-License-Identifier: MIT
//
// hotkey_helper.swift — global push-to-talk hotkey for VoxKey on macOS.
//
// Registers a Carbon global hotkey and prints "down" / "up" lines to stdout as
// the key is pressed / released. The Python daemon reads these to start/stop
// recording. Default key: Right Option (kVK_RightOption = 0x3D). Override with
// --key <hex> (macOS virtual key code).
//
// Build: swiftc -O hotkey_helper.swift -o hotkey_helper
//
import Carbon
import Foundation

/// Write a line to stdout and flush, so the parent process sees events promptly.
func printFlush(_ line: String) {
    if let data = line.data(using: .utf8) {
        FileHandle.standardOutput.write(data)
    }
    FileHandle.standardOutput.synchronizeFile()
}

let keyCode: UInt32 = {
    let args = CommandLine.arguments
    for i in 0..<args.count {
        if args[i] == "--key", i + 1 < args.count, let v = UInt32(args[i + 1], radix: 16) {
            return v
        }
    }
    return 0x3D // Right Option
}()

var hotKeyRef: EventHotKeyRef?

func install() {
    var hotKey = EventHotKeyID()
    hotKey.signature = OSType(0x564F58) // 'VOX'
    hotKey.id = 1
    let status = RegisterEventHotKey(keyCode, 0, hotKey, GetApplicationEventTarget(), 0, &hotKeyRef)
    if status != noErr {
        fputs("Failed to register hotkey (status \(status))\n", stderr)
        exit(1)
    }
    printFlush("hotkey_ready key=0x\(String(keyCode, radix: 16))\n")
}

func handler(_: EventHandlerCallRef?, _ event: EventRef?, _: UnsafeMutableRawPointer?) -> OSStatus {
    guard let event = event else { return noErr }
    var hk = EventHotKeyID()
    GetEventParameter(event, EventParamName(kEventParamDirectObject), EventParamType(typeEventHotKeyID),
                      nil, MemoryLayout<EventHotKeyID>.size, nil, &hk)
    let kind = GetEventKind(event)
    if kind == kEventHotKeyPressed {
        printFlush("down\n")
    } else if kind == kEventHotKeyReleased {
        printFlush("up\n")
    }
    return noErr
}

install()

var evt = EventTypeSpec()
evt.eventClass = OSType(kEventClassKeyboard)
evt.eventKind = UInt32(kEventHotKeyPressed)
var evt2 = EventTypeSpec()
evt2.eventClass = OSType(kEventClassKeyboard)
evt2.eventKind = UInt32(kEventHotKeyReleased)

var handlerRef: EventHandlerRef?
InstallEventHandler(GetApplicationEventTarget(), handler, 2,
                   [evt, evt2], nil, &handlerRef)

RunLoop.main.run()
