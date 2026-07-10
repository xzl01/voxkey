// SPDX-FileCopyrightText: 2026 HarryLoong
// SPDX-License-Identifier: MIT
//
// capture_helper.swift — real-time microphone capture for VoxKey on macOS.
//
// Uses AVAudioEngine to tap the default input device and stream 16 kHz mono
// 16-bit little-endian PCM to stdout (after a 44-byte WAV header). Python's
// MicCapture reads these frames incrementally for low-latency streaming ASR.
//
// Build:
//   swiftc -O capture_helper.swift -o capture_helper
// (requires Xcode command line tools: xcode-select --install)
//
import AVFoundation
import Foundation

func parseArgs() -> (rate: Double, channels: UInt32) {
    var rate: Double = 16000
    var channels: UInt32 = 1
    let args = CommandLine.arguments
    for i in 0..<args.count {
        if args[i] == "--rate", i + 1 < args.count, let r = Double(args[i + 1]) {
            rate = r
        }
        if args[i] == "--channels", i + 1 < args.count, let c = UInt32(args[i + 1]) {
            channels = c
        }
    }
    return (rate, channels)
}

private func appendUInt32(_ v: UInt32, to data: inout Data) {
    var x = v.littleEndian
    withUnsafeBytes(of: &x) { data.append(contentsOf: $0) }
}

private func appendUInt16(_ v: UInt16, to data: inout Data) {
    var x = v.littleEndian
    withUnsafeBytes(of: &x) { data.append(contentsOf: $0) }
}

func wavHeader(rate: Double, channels: UInt32) -> Data {
    var data = Data()
    let sampleRate = UInt32(rate)
    let byteRate = UInt32(sampleRate) * channels * UInt32(2)
    let blockAlign = UInt16(channels * 2)
    data.append(contentsOf: "RIFF".utf8)
    appendUInt32(0xFFFFFFFF, to: &data)   // stream: unknown size
    data.append(contentsOf: "WAVE".utf8)
    data.append(contentsOf: "fmt ".utf8)
    appendUInt32(16, to: &data)
    appendUInt16(1, to: &data)             // PCM
    appendUInt16(UInt16(channels), to: &data)
    appendUInt32(sampleRate, to: &data)
    appendUInt32(byteRate, to: &data)
    appendUInt16(blockAlign, to: &data)
    appendUInt16(16, to: &data)            // bits per sample
    data.append(contentsOf: "data".utf8)
    appendUInt32(0xFFFFFFFF, to: &data)
    return data
}

let (rate, channels) = parseArgs()
let engine = AVAudioEngine()
let input = engine.inputNode

guard let format = AVAudioFormat(standardFormatWithSampleRate: rate, channels: channels) else {
    fputs("Failed to create AVAudioFormat\n", stderr)
    exit(1)
}

input.installTap(onBus: 0, bufferSize: 4096, format: format) { buffer, _ in
    guard let floatData = buffer.floatChannelData else { return }
    let frameCount = Int(buffer.frameLength)
    var ints = [Int16](repeating: 0, count: frameCount)
    for i in 0..<frameCount {
        let s = max(-1.0, min(1.0, floatData[0][i]))
        ints[i] = Int16(s * 32767.0)
    }
    let pcm = ints.withUnsafeBytes { Data($0) }
    FileHandle.standardOutput.write(pcm)
}

do {
    try engine.start()
} catch {
    fputs("AVAudioEngine start failed: \(error)\n", stderr)
    exit(1)
}

// Readiness handshake for the Python parent. Emit the stream header only after
// AVAudioEngine actually starts, so permission/device failures surface as
// startup errors instead of looking like a successful empty recording.
FileHandle.standardOutput.write(wavHeader(rate: rate, channels: channels))

// Keep the process alive until terminated (SIGTERM) by the parent.
RunLoop.main.run()
